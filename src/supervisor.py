"""Supervisor: login state machine + worker lifecycle.

Session ka source (priority): database me saved (bot ke /login se) > TELEGRAM_SESSION_STRING env.
Session dead ho ya /logout ho to worker ruk jaata hai aur process ZINDA rehta hai (health pass hota rahe);
admin bot se /login karta hai aur worker apne aap wapas chal padta hai.
Process lease HAMESHA pakde rehta hai (Telegram se judne se pehle se), taaki ek hi session do jagah na chale.
"""
from __future__ import annotations

import asyncio
import dataclasses
import html
import logging
from typing import Awaitable, Callable

from .config import ConfigError
from .db import Database, Lease
from .jobs import JobRepo
from .status import LiveStatus
from .worker import SESSION_HELP, is_session_dead

log = logging.getLogger("supervisor")
KV_SESSION = "telegram_session"


class SessionUnusable(ConfigError):
    """Session authorized nahi / dead / revoke: dobara /login chahiye."""


async def acquire_lease(lease: Lease, stop: asyncio.Event, poll: float = 5.0) -> bool:
    """Telegram se connect hone SE PEHLE lease lo. Rolling deploy me purana instance jab tak chal raha hai, naya
    wait karta hai: warna ek hi session do IP se judti hai aur Telegram AuthKeyDuplicatedError de kar key maar deta hai.
    (Ye tabhi kaam karta hai jab DATABASE_URL PostgreSQL ho: SQLite har instance ka apna hota hai.)"""
    announced = False
    while not stop.is_set():
        if lease.acquire():
            return True
        if not announced:
            log.warning("Purana worker abhi active hai (%s). Same Telegram session do jagah nahi chal sakti, isliye "
                        "uske band hone ka wait kar raha hoon (max ~%.0fs).", lease.holder(), lease.ttl)
            announced = True
        try:
            await asyncio.wait_for(stop.wait(), poll)
        except asyncio.TimeoutError:
            pass
    return False


class Supervisor:
    def __init__(self, cfg, db: Database, repo: JobRepo, lease: Lease, *, connect: Callable, build: Callable,
                 client_factory=None, bot_enabled: bool = False):
        self.cfg, self.db, self.repo, self.lease = cfg, db, repo, lease
        self.connect, self.build, self.client_factory = connect, build, client_factory
        self.bot_enabled = bot_enabled
        self.live = LiveStatus()
        self.state = "starting"          # starting | needs_login | connecting | running | stopped
        self.me_name = ""
        self.client = None
        self.worker = None
        self.wtask: asyncio.Task | None = None
        self.bad: set[str] = set()       # known-dead strings: dobara try nahi
        self.login_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.logged_out = asyncio.Event()
        self.logout_req = False
        self.lease_lost = False
        self.notify: Callable[[str], Awaitable[None]] | None = None
        self._last_note = ""

    # ---- health ke liye (HealthServer.worker jaisa interface) ----
    @property
    def current(self) -> str | None:
        return self.worker.current if self.worker else None

    def stop(self) -> None:
        self.stop_event.set()
        if self.worker:
            self.worker.stop()

    # ---- session ----
    def stored_session(self) -> str:
        return (self.repo.get_kv(KV_SESSION) or "").strip()

    def session_string(self) -> tuple[str, str]:
        for src, val in (("saved", self.stored_session()), ("env", (self.cfg.session_string or "").strip())):
            if val and val not in self.bad:
                return val, src
        return "", ""

    def save_session(self, s: str) -> None:
        """Bot ke /login ke baad. Ye naya key hai: purani dead strings ko ignore list me hi rehne do."""
        self.repo.set_kv(KV_SESSION, s)
        self.bad.discard(s)
        self.login_event.set()

    def _mark_bad(self, s: str) -> None:
        self.bad.add(s)
        if self.stored_session() == s:
            self.repo.set_kv(KV_SESSION, "")

    async def _notify(self, text: str, once: bool = True) -> None:
        if once and text == self._last_note:
            return
        self._last_note = text
        if self.notify:
            try:
                await self.notify(text)
            except Exception as e:  # noqa: BLE001
                log.warning("Bot notify fail: %s", type(e).__name__)

    async def _wait(self, ev: asyncio.Event, timeout: float | None = None) -> None:
        t1, t2 = asyncio.create_task(ev.wait()), asyncio.create_task(self.stop_event.wait())
        try:
            await asyncio.wait({t1, t2}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (t1, t2):
                t.cancel()

    async def _sleep(self, sec: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), sec)
        except asyncio.TimeoutError:
            pass

    async def _keep_lease(self) -> None:
        while not self.stop_event.is_set():
            await self._sleep(max(5.0, self.lease.ttl / 4))
            if self.stop_event.is_set():
                return
            if not self.lease.renew():  # kisi aur instance ne le li: turant ruko, session mat chhuo
                log.error("Lease kho di (doosra instance active hai). Ruk raha hoon.")
                self.lease_lost = True
                self.stop()
                return

    # ---- logout ----
    async def logout(self) -> str:
        s, _ = self.session_string()
        if not s:
            return "Pehle se logged out hai."
        if not (self.worker and self.wtask):
            self._mark_bad(s)  # worker nahi chal raha: sirf local session hatao
            return "Local session hata di. (Telegram side se bhi hatana ho to Settings → Devices.)"
        self.logout_req = True
        self.logged_out.clear()
        self.worker.stop()
        self.wtask.cancel()
        try:
            await asyncio.wait_for(self.logged_out.wait(), 90)
        except asyncio.TimeoutError:
            return "⚠️ Logout me der lag rahi hai; thodi der baad /status dekho."
        return "ok"

    async def _finish_logout(self, client, s: str) -> None:
        try:
            await client.log_out()  # Telegram side par bhi session terminate
        except Exception as e:  # noqa: BLE001
            log.warning("log_out fail (%s): local session hata raha hoon.", type(e).__name__)
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._mark_bad(s)
        self.logout_req = False
        self.logged_out.set()

    # ---- main loop ----
    async def run(self) -> int:
        keeper = asyncio.create_task(self._keep_lease())
        code = 0
        try:
            while not self.stop_event.is_set():
                s, src = self.session_string()
                if not s:
                    if not self.bot_enabled:
                        log.error("Telegram session nahi hai. %s", SESSION_HELP)
                        code = 3
                        break
                    self.state = "needs_login"
                    await self._notify("🔐 <b>Login nahi hai.</b> /login bhejo, QR scan karo; worker apne aap shuru ho jayega.")
                    await self._wait(self.login_event)
                    self.login_event.clear()
                    continue
                self.state = "connecting"
                c = dataclasses.replace(self.cfg, session_string=s)
                try:
                    client = await self.connect(c, self.client_factory)
                except SessionUnusable as e:
                    log.error("Session (%s) unusable: %s", src, e)
                    self._mark_bad(s)
                    if not self.bot_enabled:
                        code = 3
                        break
                    await self._notify("⚠️ <b>Telegram session invalid/dead hai.</b> Dobara /login karo.")
                    continue
                except Exception as e:  # noqa: BLE001 - network etc: retry
                    log.warning("Telegram connect fail (%s: %s). 30s baad retry.", type(e).__name__, e)
                    await self._sleep(30)
                    continue
                try:
                    worker = await self.build(c, self.db, self.repo, client, lease=self.lease, live=self.live)
                except Exception as e:  # noqa: BLE001
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    if is_session_dead(e):
                        self._mark_bad(s)
                        await self._notify("⚠️ <b>Telegram session dead hai.</b> Dobara /login karo.")
                        if not self.bot_enabled:
                            code = 3
                            break
                        continue
                    log.exception("Worker setup fail")
                    await self._notify(f"❌ <b>Setup error:</b> <code>{html.escape(type(e).__name__ + ': ' + str(e)[:300], quote=False)}</code>\n60s baad dobara try karunga.")
                    await self._sleep(60)
                    continue
                worker.on_job = self.live.reset
                self.client, self.worker = client, worker
                try:
                    me = await client.get_me()
                    self.me_name = " ".join(x for x in (getattr(me, "first_name", ""), getattr(me, "last_name", "")) if x) or str(getattr(me, "id", ""))
                except Exception:  # noqa: BLE001
                    self.me_name = ""
                self.state = "running"
                self._last_note = ""
                await self._notify(f"✅ <b>Worker chal raha hai</b>" + (f" ({self.me_name})" if self.me_name else "") + ". /status se progress dekho.")
                self.wtask = asyncio.create_task(worker.run_forever())
                try:
                    await self.wtask
                except asyncio.CancelledError:
                    if not (self.logout_req or self.stop_event.is_set()):
                        raise
                except Exception:  # noqa: BLE001
                    log.exception("Worker crash")
                finally:
                    self.worker = self.wtask = self.client = None
                    self.live.reset(None)
                if self.logout_req:
                    await self._finish_logout(client, s)
                    await self._notify("👋 <b>Logout ho gaya.</b> Wapas login ke liye /login.", once=False)
                else:
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                if worker.fatal is not None:
                    self._mark_bad(s)
                    log.error("Session dead (%s).", type(worker.fatal).__name__)
                    if not self.bot_enabled:
                        code = 3
                        break
                    await self._notify("🔴 <b>Telegram session dead</b> (same session do jagah chali ya revoke hui). "
                                       "Baaki sab instances band karo, phir /login.", once=False)
                    continue
                if self.stop_event.is_set():
                    break
                # worker ne lease chhodi (run_forever ka finally): wapas lo, warna doosra instance chal raha hai
                if not await acquire_lease(self.lease, self.stop_event):
                    break
        finally:
            keeper.cancel()
            self.state = "stopped"
        return 2 if self.lease_lost and code == 0 else code
