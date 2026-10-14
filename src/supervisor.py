"""Supervisor: login state machine + worker lifecycle.

Session source (priority): saved in the database (via the bot's /login) > TELEGRAM_SESSION_STRING env.
If the session dies or /logout is used, the worker stops but the process stays ALIVE (so health checks keep passing);
the admin sends /login to the bot and the worker restarts by itself.
The process ALWAYS holds the lease (even before connecting to Telegram) so the same session never runs in two places.
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
    """Session is not authorized / dead / revoked: /login is needed again."""


async def acquire_lease(lease: Lease, stop: asyncio.Event, poll: float = 5.0) -> bool:
    """Acquire the lease BEFORE connecting to Telegram. During a rolling deploy the new instance waits while the old one
    is still running: otherwise one session connects from two IPs and Telegram raises AuthKeyDuplicatedError and kills the key.
    (This only works when DATABASE_URL is PostgreSQL: SQLite is separate for every instance.)"""
    announced = False
    while not stop.is_set():
        if lease.acquire():
            return True
        if not announced:
            log.warning("The previous worker is still active (%s). The same Telegram session cannot run in two places, so "
                        "I am waiting for it to stop (max ~%.0fs).", lease.holder(), lease.ttl)
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
        self.bad: set[str] = set()       # known-dead strings: never retried
        self.login_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.logged_out = asyncio.Event()
        self.logout_req = False
        self.lease_lost = False
        self.notify: Callable[[str], Awaitable[None]] | None = None
        self._last_note = ""

    # ---- for health checks (same interface as HealthServer.worker) ----
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
        """Called after the bot's /login. This is a new key: leave old dead strings in the ignore list."""
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
                log.warning("Bot notify failed: %s", type(e).__name__)

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
            if not self.lease.renew():  # another instance took it: stop immediately, do not touch the session
                log.error("Lease lost (another instance is active). Stopping.")
                self.lease_lost = True
                self.stop()
                return

    # ---- logout ----
    async def logout(self) -> str:
        s, _ = self.session_string()
        if not s:
            return "🚪 Already logged out."
        if not (self.worker and self.wtask):
            self._mark_bad(s)  # worker is not running: just remove the local session
            return "🧹 Local session removed. (To also remove it on the Telegram side: Settings → Devices.)"
        self.logout_req = True
        self.logged_out.clear()
        self.worker.stop()
        self.wtask.cancel()
        try:
            await asyncio.wait_for(self.logged_out.wait(), 90)
        except asyncio.TimeoutError:
            return "⚠️ Logout is taking a while. Check /status in a moment."
        return "ok"

    async def _finish_logout(self, client, s: str) -> None:
        try:
            await client.log_out()  # also terminate the session on the Telegram side
        except Exception as e:  # noqa: BLE001
            log.warning("log_out failed (%s): removing the local session.", type(e).__name__)
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
                        log.error("No Telegram session available. %s", SESSION_HELP)
                        code = 3
                        break
                    self.state = "needs_login"
                    await self._notify("🔐 <b>You are not logged in.</b>\n👉 Send /login and scan the QR code. The worker will start automatically.")
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
                    await self._notify("⚠️ <b>The Telegram session is invalid/dead.</b>\n🔐 Please send /login again.")
                    continue
                except Exception as e:  # noqa: BLE001 - network errors etc: retry
                    log.warning("Telegram connect failed (%s: %s). Retrying in 30s.", type(e).__name__, e)
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
                        await self._notify("⚠️ <b>The Telegram session is dead.</b>\n🔐 Please send /login again.")
                        if not self.bot_enabled:
                            code = 3
                            break
                        continue
                    log.exception("Worker setup failed")
                    await self._notify(f"❌ <b>Setup error:</b> <code>{html.escape(type(e).__name__ + ': ' + str(e)[:300], quote=False)}</code>\n🔁 Retrying in 60s.")
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
                await self._notify("✅ <b>Worker is running</b>" + (f" ({self.me_name})" if self.me_name else "") + "\n📊 Send /status to follow progress.")
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
                    await self._notify("👋 <b>Logged out.</b>\n🔐 Send /login to sign in again.", once=False)
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
                    await self._notify("🔴 <b>The Telegram session is dead</b> (it was used in two places at once, or it was revoked).\n"
                                       "🛑 Stop all other instances, then send /login.", once=False)
                    continue
                if self.stop_event.is_set():
                    break
                # the worker released the lease (finally of run_forever): take it back, otherwise another instance is running
                if not await acquire_lease(self.lease, self.stop_event):
                    break
        finally:
            keeper.cancel()
            self.state = "stopped"
        return 2 if self.lease_lost and code == 0 else code
