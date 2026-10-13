"""Worker: ek time par sirf ek ZIP, strict chronological order, har stage idempotent + resumable.

Error taxonomy:
  StageTransient -> backoff+jitter ke saath retry (limit ke baad pause)
  StagePause     -> ZIP PAUSED (insaan ko dekhna hai); queue is ZIP par ruki rehti hai, skip nahi
  StagePermanent -> ZIP FAILED (jaise corrupt ZIP); queue aage badhti hai, report me dikhta hai
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .channel_reader import DownloadError
from .db import Database, Lease
from .db_stores import ArticleStore, DbImageStore, DbUploadStore, PostStore
from .diskwala_uploader import flood_seconds, is_fatal, is_transient
from .image_store import collect_images
from .imgbb_uploader import ImgBBClient
from .jobs import (
    CREATING_TELEGRAPH, DOWNLOADING, EXTRACTING, GENERATING_POSTS, PUBLISHING_POSTS, UPLOADING_IMAGES,
    UPLOADING_VIDEOS, CANCELLED_STAGE, FAILED, JobRepo,
)
from .posts import (
    DEFAULT_IMAGES_ONLY_TEMPLATE, DEFAULT_TEMPLATE, DEFAULT_VIDEOS_ONLY_TEMPLATE, PostError, TemplateError,
    plan_posts, sort_videos,
)
from .publish import upload_images
from .strict_videos import StrictVideoUploader, VideoBlocked
from .telegraph_publisher import METHOD_EMBED, TelegraphClient
from .tg_publisher import PostPublisher, PublishBlocked
from .upload_store import COMPLETED, collect_videos
from .zip_processor import ArchiveCorrupted, ArchiveRejected, InsufficientSpace, Limits, extract_archive
from .zip_telegraph import ST_FAILURES, ST_UNVERIFIED, ZipArticleError, publish_zip_article

log = logging.getLogger("worker")


# Telegram errors jinhe retry karne se kuch nahi hoga: insaan ko Telegram me kuch karna hai.
HUMAN_HINTS = {
    "YouBlockedUserError": "Is Telegram account ne DiskWala bot ko BLOCK kiya hua hai. Wahi account (jiska TELEGRAM_SESSION_STRING hai) "
                           "Telegram app me kholo -> bot ki chat -> Unblock / Restart. Phir `resume`.",
    "UserIsBlockedError": "Us user/bot ne is account ko block kiya hai. Telegram me check karo.",
    "AuthKeyDuplicatedError": "Same Telegram session do jagah/IP se ek saath chali: Telegram ne key permanently kill kar di. "
                              "Naya login + export-session karo.",
    "AuthKeyUnregisteredError": "Telegram session invalid ho gaya. Naya `export-session` karke TELEGRAM_SESSION_STRING update karo.",
    "SessionRevokedError": "Telegram session revoke ho gaya. Naya `export-session` karke TELEGRAM_SESSION_STRING update karo.",
    "UserDeactivatedError": "Telegram account deactivated hai.",
    "UserDeactivatedBanError": "Telegram account banned/deactivated hai.",
    "InputUserDeactivatedError": "Target Telegram account deactivated hai.",
    "ChatWriteForbiddenError": "Destination me post karne ki permission nahi (admin banao / FINAL_POST_DESTINATION_ID check karo).",
    "PeerFloodError": "Telegram ne is account par spam-limit lagayi (PeerFlood). Kuch ghante baad resume karo; @SpamBot dekho.",
    "ChannelPrivateError": "Channel private/inaccessible hai: account ab us channel me nahi hai.",
    "PeerIdInvalidError": "Telegram ne chat/channel id reject ki: FILE_MANAGER_CHANNEL_ID / FINAL_POST_DESTINATION_ID check karo.",
}
NEEDS_HUMAN = frozenset(HUMAN_HINTS)

# Session hi mar gayi: retry/pause bekaar hai, job ka koi kasoor nahi. Worker ruk jaata hai, job jaisi thi waisi rehti hai.
SESSION_DEAD = frozenset({"AuthKeyDuplicatedError", "AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError"})
SESSION_HELP = (
    "Telegram session string dead hai (ek hi session do jagah/IP se ek saath chali, ya revoke ho gayi). "
    "FIX: 1) Jahan bhi purani session chal rahi hai wahan band karo (Termux/local, Replit, purana Koyeb/Render instance) "
    "aur local workspace/sessions/*.session file delete karo. "
    "2) Ek hi jagah naya login: `python -m src.main login`, phir `python -m src.app export-session`. "
    "3) Nayi string TELEGRAM_SESSION_STRING me daalo aur redeploy karo. "
    "4) Agar koi ZIP PAUSED dikhe: `python -m src.app resume <ID>`."
)


def is_session_dead(e: BaseException | None) -> bool:
    seen = 0
    while e is not None and seen < 5:  # cause/context chain bhi dekho
        if type(e).__name__ in SESSION_DEAD:
            return True
        e = e.__cause__ or e.__context__
        seen += 1
    return False


class SessionDead(Exception):
    """Telegram session unusable. Pause/FAIL nahi hota; worker ruk jaata hai."""


def needs_human(e: BaseException) -> bool:
    return type(e).__name__ in NEEDS_HUMAN or is_fatal(e)


def explain(text: str) -> str:
    hints = [h for name, h in HUMAN_HINTS.items() if name in text]
    return text + (" -> " + " ".join(hints) if hints else "")


class StageTransient(Exception):
    def __init__(self, msg: str, delay: float | None = None):
        super().__init__(msg)
        self.delay = delay


class StagePause(Exception):
    pass


class StagePermanent(Exception):
    pass


class StopRequested(Exception):
    pass


class ArchiveGone(Exception):
    """Source channel se ZIP message delete ho gaya: ZIP kabhi download nahi hoga => FAILED (queue nahi rokta)."""


@dataclass
class WorkerConfig:
    workspace: Path
    limits: Limits = field(default_factory=Limits)
    poll_interval: float = 30.0
    stage_max_retries: int = 5
    backoff_base: float = 5.0
    backoff_cap: float = 900.0
    imgbb_delay: float = 5.0
    image_chunk: int = 5                  # itni images ke baad stop-check
    image_failure_policy: str = "pause"   # pause | skip
    telegraph_title: str = "My Image Collection"
    telegraph_author: str = ""
    telegraph_method: str = METHOD_EMBED
    telegraph_require_verified: bool = False
    template: str = DEFAULT_TEMPLATE
    images_only_template: str = DEFAULT_IMAGES_ONLY_TEMPLATE
    videos_only_template: str = DEFAULT_VIDEOS_ONLY_TEMPLATE
    max_links_per_post: int = 8
    cleanup: bool = True
    lease_ttl: float = 120.0
    bot_username: str = "DiskWalaFileUploaderBot"


@dataclass
class Ports:
    """Bahar ki duniya (Telegram/ImgBB/Telegraph/DiskWala). Tests me fakes lagte hain."""
    scan: Callable[[], Awaitable[dict]]
    download: Callable[[dict], Awaitable[Path]]
    imgbb: ImgBBClient
    telegraph: TelegraphClient
    http: object
    make_uploader: Callable[[DbUploadStore], StrictVideoUploader]
    publisher: PostPublisher
    extract: Callable[[Path, Path, Limits], object] = extract_archive
    ensure_connected: Callable[[], Awaitable[None]] | None = None  # Telegram reconnect hook


def backoff_delay(attempt: int, base: float, cap: float, rng: Callable[[], float] = random.random) -> float:
    ceiling = min(cap, base * 2 ** (attempt - 1))
    return ceiling / 2 + rng() * ceiling / 2


class Worker:
    def __init__(self, db: Database, repo: JobRepo, cfg: WorkerConfig, ports: Ports, *, lease: Lease | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, rng: Callable[[], float] = random.random):
        self.db, self.repo, self.cfg, self.ports = db, repo, cfg, ports
        self.lease = lease or Lease(db, ttl=cfg.lease_ttl)
        self.sleep, self.rng = sleep, rng
        self.articles, self.posts = ArticleStore(db), PostStore(db)
        self.stop_event = asyncio.Event()
        self._prepared = False
        self.current: str | None = None
        self._crashes: dict[str, int] = {}
        self._job_cancelled = False
        self.max_crashes = 5
        self.fatal: BaseException | None = None  # session dead hui to yahan; run_worker exit code 3 deta hai
        self._task: asyncio.Task | None = None
        self.on_job: Callable[[str | None], None] | None = None  # bot ka live-progress hook (job shuru/khatam)

    # ------------------------------------------------------------------ loop
    def stop(self) -> None:
        self.stop_event.set()

    def cancel_current(self) -> str | None:
        """Chal rahi ZIP turant cancel (heartbeat ka ~40s wait nahi). Returns archive_id, ya None agar kuch chal hi nahi raha."""
        aid, task = self.current, self._task
        if not aid or task is None or task.done():
            return None
        self.repo.cancel(aid)  # FAILED + CANCELLED (requeue se wapas aa sakti hai)
        self._job_cancelled = True
        task.cancel()
        return aid

    def _fatal(self, e: BaseException) -> None:
        if self.fatal is None:  # sirf ek baar log (pehle har 30s par spam hota tha)
            self.fatal = e
            log.error("Telegram session dead: %s: %s", type(e).__name__, e)
            log.error(SESSION_HELP)
        self.stop_event.set()

    def _notify_job(self, aid: str | None) -> None:
        if self.on_job:
            try:
                self.on_job(aid)
            except Exception:  # noqa: BLE001 - UI hook kabhi pipeline na tode
                pass

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise StopRequested()

    async def run_forever(self) -> None:
        """Lease lo -> scan -> oldest unfinished ZIP process -> repeat. SIGTERM par graceful stop."""
        log.info("Worker shuru. Poll interval %ss", self.cfg.poll_interval)
        try:
            while not self.stop_event.is_set():
                worked = await self.run_once()
                if not worked:
                    await self._nap(self.cfg.poll_interval)
        finally:
            self.lease.release()
            log.info("Worker band.")

    async def _nap(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def run_once(self) -> bool:
        """Ek iteration. True = kisi ZIP par kaam hua."""
        if not self.lease.acquire():
            log.warning("Doosra worker active hai (lease %s). Main wait kar raha hoon.", self.lease.holder())
            return False
        try:
            info = await self.ports.scan()
            if info.get("new"):
                log.info("%s naye ZIP queue me.", info["new"])
        except Exception as e:  # noqa: BLE001 - scan fail ho to bhi purani queue chalti rahe
            if is_session_dead(e):
                self._fatal(e)
                return False
            log.warning("Channel scan fail (%s: %s). Agli baar dobara.", type(e).__name__, e)
            try:
                if self.ports.ensure_connected:
                    await self.ports.ensure_connected()
            except Exception as ce:  # noqa: BLE001
                if is_session_dead(ce):
                    self._fatal(ce)
                    return False
                log.warning("Reconnect fail: %s", ce)
        sel = self.repo.next_job()
        if sel.blocked_by:
            b = sel.blocked_by
            log.warning("Queue ruki hai: %s PAUSED - %s. 'resume %s' se aage badhao.", b["filename"], b["paused_reason"], b["archive_id"])
            return False
        if not sel.job:
            return False
        aid = sel.job["archive_id"]
        try:
            await self._run_job_with_heartbeat(sel.job)
            self._crashes.pop(aid, None)
        except asyncio.CancelledError:
            if self.stop_event.is_set():
                return True
            raise
        except SessionDead as e:
            self._fatal(e.__cause__ or e)
            return False
        except Exception as e:  # noqa: BLE001 - worker kabhi crash-loop me na jaye; state DB me safe hai
            n = self._crashes[aid] = self._crashes.get(aid, 0) + 1
            log.exception("[%s] Unexpected error (%s/%s): %s", aid, n, self.max_crashes, e)
            if n >= self.max_crashes:
                self.repo.pause(aid, f"Unexpected error {n} baar: {type(e).__name__}: {e}")
                self._crashes.pop(aid, None)
            else:
                try:
                    if self.ports.ensure_connected:
                        await self.ports.ensure_connected()
                except Exception as ce:  # noqa: BLE001
                    log.warning("Reconnect fail: %s", ce)
                await self._nap(backoff_delay(n, self.cfg.backoff_base, self.cfg.backoff_cap, self.rng))
        return True

    async def _run_job_with_heartbeat(self, job: dict) -> None:
        task = asyncio.create_task(self.process(job))
        self._task = task
        hb = asyncio.create_task(self._heartbeat(task))
        try:
            await task
        except asyncio.CancelledError:
            if self._job_cancelled:  # user ne `cancel` kiya: ye job band, worker chalta rahe
                self._job_cancelled = False
                log.warning("[%s] Cancelled by user: job roki gayi (done kaam DB me safe).", job["archive_id"])
                return
            task.cancel()
            raise
        finally:
            hb.cancel()

    async def _heartbeat(self, task: asyncio.Task) -> None:
        while True:
            await self.sleep(self.cfg.lease_ttl / 3)
            if not self.lease.renew():  # lease kho di => doosra worker aa sakta hai: turant ruko
                log.error("Lease kho di - job cancel.")
                self.stop_event.set()
                task.cancel()
                return
            cur = self.repo.get(self.current) if self.current else None
            if cur and cur["status"] == FAILED and cur.get("stage") == CANCELLED_STAGE:
                self._job_cancelled = True
                task.cancel()
                return

    # ------------------------------------------------------------------ one ZIP
    async def process(self, job: dict) -> str:
        aid = job["archive_id"]
        self.current = aid
        self._notify_job(aid)
        log.info("[%s] %s: process shuru (status %s)", aid, job["filename"], job["status"])
        try:
            await self._stage_extract(job)
            self._check_stop()
            await self._stage_images(aid)
            self._check_stop()
            await self._stage_article(aid)
            self._check_stop()
            await self._stage_videos(aid)
            self._check_stop()
            await self._stage_posts(aid)
            self._check_stop()
            await self._stage_publish(aid)
        except StopRequested:
            log.info("[%s] Stop request: state DB me safe hai, restart par yahin se resume.", aid)
            return "stopped"
        except SessionDead:
            log.warning("[%s] Session dead: job pause nahi ki, naya session lagane ke baad wahin se chalegi.", aid)
            raise
        except asyncio.CancelledError:
            log.info("[%s] Cancel: state DB me safe hai.", aid)
            raise
        except StagePause as e:
            reason = explain(str(e))
            self.repo.pause(aid, reason)
            log.warning("[%s] PAUSED: %s", aid, reason)
            return "paused"
        except StagePermanent as e:
            self.repo.fail(aid, str(e))
            log.error("[%s] FAILED: %s", aid, e)
            return "failed"
        except Exception as e:  # noqa: BLE001
            if is_session_dead(e):
                raise SessionDead(str(e)) from e
            if not needs_human(e):
                raise  # asli bug/unknown: run_once ka crash guard sambhalega
            reason = explain(f"{type(e).__name__}: {e}")  # retry bekaar hai: turant pause + saaf instruction
            self.repo.pause(aid, reason)
            log.warning("[%s] PAUSED (action chahiye): %s", aid, reason)
            return "paused"
        finally:
            self.current = None
            self._notify_job(None)
        self.repo.complete(aid)
        log.info("[%s] COMPLETED", aid)
        if self.cfg.cleanup:
            self._cleanup(aid)
        return "completed"

    async def _retry(self, aid: str, label: str, fn: Callable[[], Awaitable]):
        attempt = 0
        while True:
            self._check_stop()
            try:
                return await fn()
            except StageTransient as e:
                attempt += 1
                self.repo.bump_retry(aid)
                if attempt > self.cfg.stage_max_retries:
                    raise StagePause(f"{label}: {e} ({attempt - 1} retries ke baad bhi)") from e
                delay = e.delay if e.delay is not None else backoff_delay(attempt, self.cfg.backoff_base, self.cfg.backoff_cap, self.rng)
                log.warning("[%s] %s temporary error: %s. %.0fs baad retry %s/%s", aid, label, e, delay, attempt, self.cfg.stage_max_retries)
                await self._nap(delay)

    # ------------------------------------------------------------------ helpers
    def _job_dir(self, aid: str) -> Path:
        return self.cfg.workspace / "jobs" / aid.replace(":", "_").replace("-", "n")

    def _rel(self, p: Path) -> str:
        return str(Path(p).resolve().relative_to(self.cfg.workspace.resolve()))

    def _media_needed(self, aid: str) -> bool:
        imgs = DbImageStore(self.db, aid).files
        vids = DbUploadStore(self.db, aid, self.cfg.bot_username, self.cfg.workspace).files
        return any(r["imgbb"]["status"] != "uploaded" for r in imgs) or any(v["status"] != COMPLETED for v in vids)

    def _media_present(self, aid: str) -> bool:
        ws = self.cfg.workspace
        for r in DbImageStore(self.db, aid).files:
            if r["imgbb"]["status"] != "uploaded" and not (ws / r["path"]).is_file():
                return False
        for v in DbUploadStore(self.db, aid, self.cfg.bot_username, ws).files:
            if v["status"] != COMPLETED and not Path(v["path"]).is_file():
                return False
        return True

    def _cleanup(self, aid: str) -> None:
        job = self.repo.get(aid) or {}
        info = job.get("extraction") or {}
        for rel in (info.get("archive_path"), info.get("dir")):
            if not rel:
                continue
            p = self.cfg.workspace / rel
            try:
                shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)
            except OSError as e:
                log.warning("[%s] cleanup fail %s: %s", aid, rel, e)

    # ------------------------------------------------------------------ stages
    async def _stage_extract(self, job: dict) -> None:
        aid = job["archive_id"]
        fresh = self.repo.get(aid)  # latest
        info = fresh.get("extraction")
        if info is not None and (not self._media_needed(aid) or self._media_present(aid)):
            return  # kaam ho chuka ya media disk par maujood hai
        self.repo.transition(aid, DOWNLOADING)
        path = await self._retry(aid, "download", lambda: self._download(job))
        self._check_stop()
        self.repo.transition(aid, EXTRACTING)
        dest = self._job_dir(aid) / "extracted"
        shutil.rmtree(dest, ignore_errors=True)  # adhoori purani extraction hatao
        try:
            res = await asyncio.to_thread(self.ports.extract, path, dest, self.cfg.limits)
        except (ArchiveRejected, ArchiveCorrupted) as e:
            raise StagePermanent(f"{type(e).__name__}: {e}") from e
        except InsufficientSpace as e:
            raise StagePause(f"Disk space kam: {e}") from e
        files = [{**f, "path": f"{self._rel(dest)}/{f['relative_path']}"} for f in res.files]
        if info is None:
            if not any(f["type"] in ("image", "video") for f in files):
                raise StagePermanent("ZIP me koi supported image/video nahi mila.")
            self._register_media(aid, job, files)
            self.repo.save_extraction(aid, {
                "archive_path": self._rel(path), "dir": self._rel(dest),
                "images": sum(f["type"] == "image" for f in files), "videos": sum(f["type"] == "video" for f in files),
                "skipped": res.skipped_count})
        else:  # redeploy ke baad re-extract: sizes DB se match hone chahiye
            sizes = {f["file_id"]: f["size"] for f in files}
            for rec in DbImageStore(self.db, aid).files + DbUploadStore(self.db, aid, self.cfg.bot_username, self.cfg.workspace).files:
                if rec["file_id"] in sizes and sizes[rec["file_id"]] != rec["size"]:
                    raise StagePermanent(f"Re-extract me {rec['filename']} ka size badal gaya.")
            self.repo.save_extraction(aid, {**info, "archive_path": self._rel(path), "dir": self._rel(dest)})

    async def _download(self, job: dict) -> Path:
        try:
            return await self.ports.download(job)
        except ArchiveGone as e:
            raise StagePermanent(str(e)) from e
        except DownloadError as e:
            if is_session_dead(e):
                raise SessionDead(str(e)) from e
            msg = str(e)
            if "bohot bada" in msg:
                raise StagePermanent(msg) from e
            if "Disk space" in msg:
                raise StagePause(msg) from e
            raise StageTransient(msg) from e
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            if is_session_dead(e):
                raise SessionDead(str(e)) from e
            secs = flood_seconds(e)
            if secs is not None:
                raise StageTransient(f"Telegram FloodWait {secs}s", delay=secs + 1) from e
            if is_transient(e):
                raise StageTransient(f"{type(e).__name__}: {e}") from e
            raise StagePause(f"Download fail: {type(e).__name__}: {e}") from e

    def _register_media(self, aid: str, job: dict, files: list[dict]) -> None:
        manifest = {"archives": [{"archive_id": aid, "archive_filename": job["filename"], "message_id": job["message_id"],
                                  "status": "completed", "files": files, "extraction_dir": None}]}
        DbImageStore(self.db, aid).sync(collect_images(manifest))
        vids = sort_videos(collect_videos(manifest))  # asli disk size ke hisaab se: bada pehle
        ws = self.cfg.workspace
        for v in vids:
            v["size"] = (ws / v["path"]).stat().st_size
        DbUploadStore(self.db, aid, self.cfg.bot_username, ws).sync(sort_videos(vids))

    async def _stage_images(self, aid: str) -> None:
        if not DbImageStore(self.db, aid).select_for_upload(retry_failed=True):
            return
        self.repo.transition(aid, UPLOADING_IMAGES)
        await self._retry(aid, "imgbb", lambda: self._images_pass(aid))
        failed = [r for r in DbImageStore(self.db, aid).ordered() if r["imgbb"]["status"] == "failed"]
        if failed and self.cfg.image_failure_policy != "skip":
            names = ", ".join(r["filename"] for r in failed[:5])
            raise StagePause(f"{len(failed)} image(s) ImgBB par upload nahi hui ({names}...). Theek karke resume karo "
                             "ya IMAGE_FAILURE_POLICY=skip lagao.")

    async def _images_pass(self, aid: str) -> None:
        """Pending images ek-ek (delay ke saath), chhote chunks me taaki stop/heartbeat chalte rahein."""
        store = DbImageStore(self.db, aid)
        work = store.select_for_upload(retry_failed=True)
        total = len(store.files)
        for i in range(0, len(work), self.cfg.image_chunk):
            self._check_stop()
            def say(m: str, base: int = i, n: int = len(work)) -> None:  # chunk ka [1/5] -> poore ZIP ka [k/n]
                log.info("[%s] %s", aid, re.sub(r"^\[(\d+)/\d+\]", lambda x: f"[{base + int(x.group(1))}/{n}]", m))
            run = await asyncio.to_thread(upload_images, store, self.ports.imgbb, work[i:i + self.cfg.image_chunk],
                                          self.cfg.workspace, delay=self.cfg.imgbb_delay, out=say)
            self.repo.set_progress(aid, {"images_uploaded": len(store.uploaded()), "images_total": total})
            if run.stopped_reason:
                if "rate limit" in run.stopped_reason.lower():  # service ne roka: wait, bypass nahi
                    raise StageTransient(run.stopped_reason, delay=max(60.0, self.cfg.backoff_base * 6))
                raise StagePause(f"ImgBB: {run.stopped_reason}")

    async def _stage_article(self, aid: str) -> None:
        store = DbImageStore(self.db, aid)
        if not store.uploaded():
            return
        art = self.articles.get(aid)
        if art and art["status"] in ("verified", "created_unverified") and art.get("urls"):
            return self._article_gate(art)
        self.repo.transition(aid, CREATING_TELEGRAPH)

        async def go():
            try:
                return await asyncio.to_thread(
                    publish_zip_article, articles=self.articles, store=store, client=self.ports.telegraph,
                    http=self.ports.http, archive_id=aid, title=self.cfg.telegraph_title,
                    author=self.cfg.telegraph_author, method=self.cfg.telegraph_method)
            except ZipArticleError as e:
                raise StageTransient(str(e)) from e
            except Exception as e:  # noqa: BLE001 - definitive Telegraph rejection etc.
                if type(e).__name__ == "TelegraphError":
                    raise StagePause(f"Telegraph: {e}") from e
                raise

        art = await self._retry(aid, "telegraph", go)
        log.info("[%s] Telegraph article (%s): %s", aid, art["status"], ", ".join(art["urls"]))
        self._article_gate(art)

    def _article_gate(self, art: dict) -> None:
        if art["status"] == ST_FAILURES:
            raise StagePause(f"Telegraph article me {art['counts']['failed']} image(s) verify nahi hui (failed). "
                             "Dekh ke resume karo (verification dobara chalegi).")
        if art["status"] == ST_UNVERIFIED and self.cfg.telegraph_require_verified:
            raise StagePause(f"{art['counts']['embedded_unverified']} image(s) unverified aur TELEGRAPH_REQUIRE_VERIFIED=true.")

    async def _stage_videos(self, aid: str) -> None:
        store = DbUploadStore(self.db, aid, self.cfg.bot_username, self.cfg.workspace)
        if not store.files:
            return
        if all(v["status"] == COMPLETED and v.get("url") for v in store.files):
            return
        self.repo.transition(aid, UPLOADING_VIDEOS)
        order = sort_videos(store.files)
        log.info("[%s] %s video(s), bada pehle: %s", aid, len(order),
                 ", ".join(f"{v['filename']} ({v['size'] / 1048576:.0f} MB)" for v in order[:10]) + (" ..." if len(order) > 10 else ""))
        uploader = self.ports.make_uploader(store)
        if not self._prepared:
            await uploader.prepare()
            self._prepared = True
        total = len(store.files)
        try:
            await uploader.process_all(store.files)
        except VideoBlocked as e:
            raise StagePause(f"DiskWala: {e}") from e
        finally:
            self.repo.set_progress(aid, {"videos_done": sum(v["status"] == COMPLETED for v in store.files), "videos_total": total})

    async def _stage_posts(self, aid: str) -> None:
        if self.posts.list(aid):
            return  # posts pehle se saved: kabhi regenerate nahi (content stable)
        self.repo.transition(aid, GENERATING_POSTS)
        vids = DbUploadStore(self.db, aid, self.cfg.bot_username, self.cfg.workspace).files
        review = [v["filename"] for v in vids if v.get("needs_review")]
        if review:
            raise StagePause(f"Ye video links needs_review hain ({', '.join(review[:3])}). Check karke "
                             f"`approve {aid}` phir resume.")
        bad = [v["filename"] for v in vids if v["status"] != COMPLETED or not v.get("url")]
        if bad:
            raise StagePause(f"Videos complete nahi: {', '.join(bad[:3])}")
        links = [v["url"] for v in sort_videos(vids)]
        art = self.articles.get(aid)
        urls = (art or {}).get("urls", [])
        try:
            plans = plan_posts(video_links=links, telegraph_urls=urls, template=self.cfg.template,
                               images_only_template=self.cfg.images_only_template,
                               videos_only_template=self.cfg.videos_only_template, max_links=self.cfg.max_links_per_post)
        except (PostError, TemplateError) as e:
            raise StagePause(f"Post banana fail: {e}") from e
        self.posts.save_plan(aid, plans)
        log.info("[%s] %s post(s) tayyar (publish se pehle DB me saved).", aid, len(plans))

    async def _stage_publish(self, aid: str) -> None:
        if all(p["status"] == "published" for p in self.posts.list(aid)):
            return
        self.repo.transition(aid, PUBLISHING_POSTS)
        try:
            n = await self.ports.publisher.publish_all(aid)
            log.info("[%s] %s post(s) publish ho gaye.", aid, n)
        except PublishBlocked as e:
            raise StagePause(f"Publish: {e}") from e
