"""Agla ZIP pehle se download (jab current ZIP ke videos bot ke link ka intezaar kar rahe hon).

SAFETY (kya ye KABHI nahi karta):
  - DB ko nahi chhuta: job ki status/stage/files/links/posts sab waise hi. Sirf ZIP file disk par aati hai.
  - Video/link ka koi kaam nahi: videos ek-ek karke, apni ZIP ke liye, pehle jaise strict order me. Isliye link kisi
    dusri video se mix nahi ho sakta.
  - Wahi download_zip use hota hai (.part -> size check -> rename), to adhoori file kabhi final naam nahi leti.
  - Jab worker is ZIP ki baari par aata hai to pehle chal rahe prefetch ka INTEZAAR karta hai (dono ek hi .part par kabhi
    nahi likhte). Prefetch fail ho to normal download pehle jaisa retry ke saath chalta hai.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

log = logging.getLogger("prefetch")


class Prefetcher:
    def __init__(self, peek: Callable[[str], dict | None], download: Callable[[dict], Awaitable] | None, enabled: bool = True):
        self.peek, self.download = peek, download
        self.enabled = bool(enabled and download)
        self._task: asyncio.Task | None = None
        self._aid: str | None = None
        self._done: set[str] = set()     # prefetch ho chuke (dobara get_messages/API call nahi)
        self._failed: set[str] = set()   # prefetch fail: normal download sambhalega, hum dobara hathoda nahi marenge

    # ------------------------------------------------------------------ start
    def kick(self, current_aid: str) -> None:
        """Current job ke baad ka job (agar download layak ho) background me download shuru. Ek samay par ek hi."""
        if not self.enabled or (self._task is not None and not self._task.done()):
            return
        try:
            job = self.peek(current_aid)
        except Exception as e:  # noqa: BLE001 - peek me koi dikkat pipeline na roke
            log.warning("Prefetch peek fail: %s: %s", type(e).__name__, e)
            return
        if not job or (job.get("kind") or "zip") == "media":
            return  # media batch me ZIP hoti hi nahi
        aid = job["archive_id"]
        if aid == current_aid or aid in self._done or aid in self._failed:
            return
        self._aid = aid
        log.info("[%s] Prefetch shuru: %s (current ZIP ke videos ka intezaar chal raha hai).", aid, job.get("filename"))
        self._task = asyncio.ensure_future(self._run(job))

    async def _run(self, job: dict) -> None:
        aid = job["archive_id"]
        try:
            await self.download(job)
            self._done.add(aid)
            log.info("[%s] Prefetch poora: %s tayyar.", aid, job.get("filename"))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - normal download retry/pause sambhalega
            self._failed.add(aid)
            log.warning("[%s] Prefetch fail (%s: %s). Apni baari par normal download hoga.", aid, type(e).__name__, e)

    # ------------------------------------------------------------------ worker ki baari
    async def wait_for(self, aid: str) -> None:
        """Is job ka prefetch chal raha ho to poora hone do (same .part par do download kabhi nahi)."""
        t = self._task
        if t is None or t.done() or self._aid != aid:
            return
        log.info("[%s] Prefetch chal raha hai: poora hone ka intezaar.", aid)
        try:
            await asyncio.shield(t)
        except asyncio.CancelledError:
            if t.cancelled():
                return  # prefetch cancel hua (hum nahi): normal download chalega
            raise  # asli cancel (stop/cancel job): prefetch ko chhod do, upar jaane do

    def discard_other(self, aid: str) -> None:
        """Ab jo job chal raha hai wo prefetch wala nahi hai (priority badli/cancel): prefetch rok do, bandwidth bachao."""
        t = self._task
        if t is not None and not t.done() and self._aid != aid:
            log.info("Prefetch %s rok diya (ab %s chal raha hai).", self._aid, aid)
            t.cancel()

    async def aclose(self) -> None:
        t = self._task
        if t is not None and not t.done():
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
