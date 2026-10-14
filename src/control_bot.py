"""Telegram control bot (Telethon bot account): /login (QR) /logout /status (live card) /cancel /queue /resume /requeue.

Only users listed in ADMIN_IDS can run commands. The 2FA password comes from the env (TELEGRAM_2FA_PASSWORD); it is never
asked for in chat and never written to the logs. After login the session string is saved in the DATABASE (kept until logout).
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from dataclasses import dataclass

from .config import ConfigError
from .jobs import PAUSED, TERMINAL
from .status import DIV, esc, render_idle, render_job, render_paused

log = logging.getLogger("control_bot")

QR_WAIT = 25          # sec: lifetime of one QR (Telegram ~30s)
QR_REFRESHES = 12     # ~5 min
CARD_TICK = 4.0       # sec: live card edit interval (protects the bot from flood limits)
CARD_MAX_TICKS = 600  # auto-refresh stops after ~40 min; the Refresh button restarts it
PW_NAMES = ("TELEGRAM_2FA_PASSWORD", "TWO_FA_PASSWORD", "TG_2FA_PASSWORD", "TELEGRAM_PASSWORD", "PASSWORD_2FA")

COMMANDS = [("login", "🔐 Log in to Telegram via QR"), ("status", "📊 Live progress card"), ("cancel", "⛔ Cancel the running task"),
            ("queue", "📋 All ZIP jobs"), ("resume", "▶️ Put a PAUSED job back in the queue"),
            ("requeue", "♻️ Re-queue a cancelled/failed job"),
            ("dwstart", "🚀 Send /start to the DiskWala bot"), ("dwrestart", "🔁 Restart the DiskWala bot (stop + start)"),
            ("logout", "🚪 Remove the Telegram session"), ("help", "❓ Help")]


@dataclass
class BotSettings:
    token: str = ""
    admins: frozenset = frozenset()
    password: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.token)


def load_bot_settings(env=os.environ) -> BotSettings:
    token = (env.get("BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    raw = f"{env.get('ADMIN_IDS') or ''} {env.get('ADMIN_ID') or ''}"
    admins = frozenset(int(x) for x in re.findall(r"-?\d+", raw))
    pw = next((env[n] for n in PW_NAMES if (env.get(n) or "")), "")
    return BotSettings(token=token, admins=admins, password=pw)


def _png_from_matrix(matrix, scale: int = 10) -> bytes:
    """Grayscale PNG without Pillow (zlib + struct). matrix: list of rows, True = black module."""
    import struct
    import zlib
    n = len(matrix)
    raw = bytearray()
    for r in matrix:
        line = b"".join((b"\x00" if v else b"\xff") * scale for v in r)
        raw += (b"\x00" + line) * scale  # filter byte 0 + pixels, repeated `scale` times
    w = n * scale

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, w, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def make_qr_png(url: str) -> bytes | None:
    try:
        import qrcode
        q = qrcode.QRCode(border=4, box_size=1)
        q.add_data(url)
        q.make(fit=True)
        return _png_from_matrix(q.get_matrix(), 10)
    except Exception as e:  # noqa: BLE001 - qrcode is not installed: only the link will be sent
        log.warning("Could not build QR image (%s: %s): sending the link only.", type(e).__name__, e)
        return None


class LoginError(Exception):
    pass


class ControlBot:
    def __init__(self, cfg, settings: BotSettings, sup):
        self.cfg, self.bs, self.sup = cfg, settings, sup
        self.bot = None
        self._login_task: asyncio.Task | None = None
        self._cards: dict[int, asyncio.Task] = {}

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession
        s = self.cfg.settings
        self.bot = TelegramClient(StringSession(), s.api_id, s.api_hash, connection_retries=10**6, retry_delay=3,
                                  auto_reconnect=True)
        try:
            await asyncio.wait_for(self.bot.start(bot_token=self.bs.token), 90)
        except Exception as e:  # noqa: BLE001
            raise ConfigError(f"Control bot failed to start ({type(e).__name__}). Check BOT_TOKEN.") from e
        self.bot.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        self.bot.add_event_handler(self._on_callback, events.CallbackQuery())
        self.sup.notify = self.notify
        try:
            from telethon.tl.functions.bots import SetBotCommandsRequest
            from telethon.tl.types import BotCommand, BotCommandScopeDefault
            await self.bot(SetBotCommandsRequest(scope=BotCommandScopeDefault(), lang_code="",
                                                 commands=[BotCommand(command=c, description=d) for c, d in COMMANDS]))
        except Exception as e:  # noqa: BLE001
            log.warning("Could not set the bot commands menu: %s", type(e).__name__)
        me = await self.bot.get_me()
        log.info("Control bot ready: @%s (admins: %s)", me.username, len(self.bs.admins))
        if not self.bs.admins:
            log.warning("ADMIN_IDS is not set: no command will work. Send /start to the bot and it will show your ID.")

    async def stop(self) -> None:
        for t in [self._login_task, *self._cards.values()]:
            if t and not t.done():
                t.cancel()
        if self.bot:
            try:
                await self.bot.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def notify(self, text: str) -> None:
        for uid in self.bs.admins:
            try:
                await self.bot.send_message(uid, text, parse_mode="html")
            except Exception as e:  # noqa: BLE001 - the admin may not have started the bot yet
                log.debug("notify %s fail: %s", uid, type(e).__name__)

    # ------------------------------------------------------------------ messages
    async def _on_message(self, event) -> None:
        if not event.is_private:
            return
        text = (event.raw_text or "").strip()
        if not text.startswith("/"):
            return
        cmd = text.split()[0][1:].split("@")[0].lower()
        uid = event.sender_id
        if uid not in self.bs.admins:
            await event.respond(f"⛔ <b>Access denied</b>\n{DIV}\n🆔 Your ID: <code>{uid}</code>\n\n"
                                f"👉 To become an admin, set <code>ADMIN_IDS={uid}</code> in Koyeb and redeploy.",
                                parse_mode="html")
            return
        fn = {"start": self.cmd_help, "help": self.cmd_help, "login": self.cmd_login, "logout": self.cmd_logout,
              "status": self.cmd_status, "cancel": self.cmd_cancel, "queue": self.cmd_queue,
              "resume": self.cmd_resume, "requeue": self.cmd_requeue,
              "dwstart": self.cmd_dwstart, "dwrestart": self.cmd_dwrestart}.get(cmd)
        if not fn:
            return await event.respond("🤔 Unknown command. Send /help to see what I can do.")
        try:
            await fn(event)
        except Exception as e:  # noqa: BLE001
            log.exception("command /%s fail", cmd)
            await event.respond(f"❌ Error: <code>{esc(type(e).__name__)}: {esc(str(e)[:200])}</code>", parse_mode="html")

    def _acct_line(self) -> str:
        st = self.sup.state
        if st == "running":
            return f"🟢 Logged in{' · ' + esc(self.sup.me_name) if self.sup.me_name else ''} · worker is running"
        return {"needs_login": "🔐 Logged out · send /login", "connecting": "⏳ Connecting to Telegram…",
                "starting": "⏳ Starting up…"}.get(st, f"⚪ {esc(st)}")

    async def cmd_help(self, event) -> None:
        warn = ""
        if self.sup.db.kind == "sqlite":
            warn = ("\n\n⚠️ <b>The database is SQLite</b> (it is wiped on every redeploy): your login, queue and Telegraph "
                    "account will reset on each deploy. Use a Koyeb/Neon PostgreSQL <code>DATABASE_URL</code> instead.")
        await event.respond(
            f"🤖 <b>ZIP Worker Control</b>\n{self._acct_line()}\n{DIV}\n\n"
            "🔑 <b>Account</b>\n"
            "/login — 🔐 QR login (2FA password comes from env)\n"
            "/logout — 🚪 remove the session\n\n"
            "📦 <b>Jobs</b>\n"
            "/status — 📊 live progress card\n"
            "/queue — 📋 all jobs\n"
            "/cancel — ⛔ cancel the running task\n"
            "/resume — ▶️ resume a PAUSED job\n"
            "/requeue — ♻️ re-queue a cancelled/failed job\n\n"
            "💿 <b>DiskWala bot</b>\n"
            "/dwstart — 🚀 send /start to the bot\n"
            "/dwrestart — 🔁 restart (stop → unblock → /start)" + warn, parse_mode="html")

    # ------------------------------------------------------------------ login / logout
    async def cmd_login(self, event) -> None:
        if self.sup.session_string()[0]:
            return await event.respond(f"{self._acct_line()}\n✅ Already logged in. To log in with another account, send /logout first.",
                                       parse_mode="html")
        s = self.cfg.settings
        if not (s.api_id and s.api_hash):
            return await event.respond("❌ API_ID / API_HASH are not set.")
        if self._login_task and not self._login_task.done():
            self._login_task.cancel()
        self._login_task = asyncio.create_task(self._login_flow(event.chat_id))

    async def _login_flow(self, chat_id: int) -> None:
        from telethon import TelegramClient
        from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
        from telethon.sessions import StringSession
        s = self.cfg.settings
        uc = TelegramClient(StringSession(), s.api_id, s.api_hash)
        msg = None
        try:
            await uc.connect()
            qr = await uc.qr_login()
            for _ in range(QR_REFRESHES):
                msg = await self._show_qr(chat_id, msg, qr.url)
                try:
                    await qr.wait(timeout=QR_WAIT)
                    break
                except asyncio.TimeoutError:
                    await qr.recreate()
                except SessionPasswordNeededError:
                    if not self.bs.password:
                        raise LoginError("🔒 2-step verification is on, but no 2FA password is set in the env. Add "
                                         "<code>TELEGRAM_2FA_PASSWORD</code> in Koyeb, redeploy, then send /login again.")
                    try:
                        await uc.sign_in(password=self.bs.password)
                    except PasswordHashInvalidError:
                        raise LoginError("🔒 The 2FA password is wrong. Check the Koyeb variable.") from None
                    break
            else:
                raise LoginError("⌛ The QR expired (5 min). Send /login again.")
            if not await uc.is_user_authorized():
                raise LoginError("⚠️ Login was not completed. Send /login again.")
            me = await uc.get_me()
            session = StringSession.save(uc.session)
            await self._drop(msg)
            self.sup.save_session(session)  # saved in the database (kept until logout) + signals the worker to start
            name = " ".join(x for x in (me.first_name, me.last_name) if x) or str(me.id)
            await self.bot.send_message(chat_id, f"✅ <b>Logged in:</b> {esc(name)}\n{DIV}\n💾 Session saved\n🚀 Worker is starting…\n\n"
                                                 "📊 Send /status to follow progress.", parse_mode="html")
            if self.sup.db.kind == "sqlite":
                await self.bot.send_message(chat_id, "⚠️ The database is SQLite: this login will be lost on redeploy. Use a PostgreSQL <code>DATABASE_URL</code>.", parse_mode="html")
        except asyncio.CancelledError:
            await self._drop(msg)
            await self.bot.send_message(chat_id, "✖️ Login cancelled.")
        except LoginError as e:
            await self._drop(msg)
            await self.bot.send_message(chat_id, f"❌ {e}", parse_mode="html")
        except Exception as e:  # noqa: BLE001
            log.warning("Login failed: %s", type(e).__name__)
            await self._drop(msg)
            await self.bot.send_message(chat_id, f"❌ Login failed: <code>{esc(type(e).__name__)}</code>\n🔁 Send /login to try again.", parse_mode="html")
        finally:
            try:
                await uc.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _drop(self, msg) -> None:
        if msg:
            try:
                await msg.delete()
            except Exception:  # noqa: BLE001
                pass

    async def _show_qr(self, chat_id: int, msg, url: str):
        from telethon import Button
        caption = (f"📲 <b>Telegram Login</b>\n{DIV}\n"
                   "1️⃣ Open Telegram → <b>Settings → Devices → Link Desktop Device</b>\n"
                   "2️⃣ Scan this QR code — or tap <b>Open in Telegram</b> below\n\n"
                   "⏳ The QR refreshes automatically every ~25s.")
        png = make_qr_png(url)
        if png is None:
            caption += f"\n\n🔗 <code>{esc(url)}</code>"
        cancel = [Button.inline("✖️ Cancel", b"login_cancel")]
        variants = ([[Button.url("📲 Open in Telegram", url)], cancel], [cancel])  # fallback without the URL button in case Telegram rejects the tg:// link

        def f():
            bio = io.BytesIO(png)
            bio.name = "login_qr.png"
            return bio
        if msg is not None:
            for btns in variants:
                try:
                    kw = {"file": f()} if png else {}
                    await msg.edit(text=caption, buttons=btns, parse_mode="html", **kw)
                    return msg
                except Exception as e:  # noqa: BLE001
                    if type(e).__name__ == "MessageNotModifiedError":
                        return msg
            await self._drop(msg)
        last = None
        for btns in variants:
            try:
                if png:
                    return await self.bot.send_file(chat_id, f(), caption=caption, buttons=btns, parse_mode="html")
                return await self.bot.send_message(chat_id, caption, buttons=btns, parse_mode="html", link_preview=False)
            except Exception as e:  # noqa: BLE001
                last = e
        raise last  # type: ignore[misc]

    async def cmd_logout(self, event) -> None:
        from telethon import Button
        if not self.sup.session_string()[0]:
            return await event.respond("🚪 Already logged out. Send /login to sign in.")
        await event.respond(f"🚪 <b>Log out for real?</b>\n{DIV}\n🔌 The Telegram session will be terminated\n"
                            "⏹ The running task will stop (your progress stays safe in the DB)\n"
                            "🔐 You will need /login again.",
                            buttons=[[Button.inline("✅ Yes, log out", b"logout_yes"), Button.inline("↩️ No", b"logout_no")]],
                            parse_mode="html")

    # ------------------------------------------------------------------ status card
    def _buttons(self, rows):
        from telethon import Button
        return [[Button.inline(t, d) for t, d in row] for row in rows]

    def _render(self):
        """(text, buttons, active). active=True => the card keeps live-refreshing."""
        sup, repo = self.sup, self.sup.repo
        refresh = ("🔄 Refresh", b"refresh")
        if sup.state == "needs_login":
            return "🔐 <b>You are not logged in.</b>\n👉 Send /login to sign in with a QR code.", self._buttons([[refresh]]), False
        if sup.state in ("starting", "connecting"):
            return f"{self._acct_line()}", self._buttons([[refresh]]), True
        w = sup.worker
        sel = repo.next_job()
        unfinished = [r for r in repo.list() if r["status"] not in TERMINAL]
        if w and w.current:
            job = repo.get(w.current)
            if job:
                text = render_job(job, sup.live, max(0, len(unfinished) - 1))
                return text, self._buttons([[refresh, ("⛔ Cancel task", b"cancel_ask")]]), True
        if sel.blocked_by:
            b = sel.blocked_by
            return render_paused(b), self._buttons([[("▶️ Resume", f"resume:{b['archive_id']}".encode()), refresh]]), False
        if sel.job:
            return (f"⏳ <b>Up next:</b> 📦 {esc(sel.job['filename'])}\n🤖 The worker is about to pick it up…", self._buttons([[refresh]]), True)
        counts: dict = {}
        for r in repo.list():
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return render_idle(counts), self._buttons([[refresh]]), False

    async def cmd_status(self, event) -> None:
        self._stop_card(event.chat_id)
        text, btns, active = self._render()
        msg = await event.respond(text, buttons=btns, parse_mode="html")
        if active:
            self._cards[event.chat_id] = asyncio.create_task(self._card_loop(event.chat_id, msg))

    def _stop_card(self, chat_id: int) -> None:
        t = self._cards.pop(chat_id, None)
        if t and not t.done():
            t.cancel()

    async def _card_loop(self, chat_id: int, msg) -> None:
        from telethon.errors import FloodWaitError, MessageNotModifiedError
        last = None
        try:
            for _ in range(CARD_MAX_TICKS):
                await asyncio.sleep(CARD_TICK)
                text, btns, active = self._render()
                if text != last:
                    try:
                        await self.bot.edit_message(chat_id, msg.id, text, buttons=btns, parse_mode="html")
                    except MessageNotModifiedError:
                        pass
                    except FloodWaitError as e:
                        await asyncio.sleep(e.seconds + 1)
                    last = text
                if not active:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - e.g. the message was deleted
            log.debug("card loop stopped: %s", type(e).__name__)

    # ------------------------------------------------------------------ cancel / queue / resume
    def _cancel_prompt(self):
        w = self.sup.worker
        if not (w and w.current):
            return "😴 No task is running right now.", None
        job = self.sup.repo.get(w.current) or {}
        return (f"⛔ <b>Cancel this task?</b>\n{DIV}\n📦 {esc(job.get('filename', w.current))}\n\n"
                "ℹ️ Work already done (ImgBB uploads, Telegraph article, published posts) is <b>not</b> deleted.\n"
                "⏭ The queue will move on to the next job.\n"
                "♻️ You can bring it back with /requeue.",
                self._buttons([[("✅ Yes, cancel", b"cancel_yes"), ("↩️ No", b"cancel_no")]]))

    async def cmd_cancel(self, event) -> None:
        text, btns = self._cancel_prompt()
        await event.respond(text, buttons=btns, parse_mode="html")

    async def cmd_queue(self, event) -> None:
        rows = self.sup.repo.list()
        if not rows:
            return await event.respond("📭 The queue is empty.")
        icon = {"COMPLETED": "✅", "FAILED": "❌", "PAUSED": "⏸", "QUEUED": "⏳", "DISCOVERED": "⏳"}
        lines = [f"{icon.get(r['status'], '🔄')} <code>{esc(r['archive_id'])}</code> {esc(r['filename'][:34])} · {r['status'].lower()}"
                 for r in rows[-12:]]
        await event.respond(f"📋 <b>Queue</b> (latest 12)\n{DIV}\n" + "\n".join(lines), parse_mode="html")

    async def cmd_resume(self, event) -> None:
        b = self.sup.repo.next_job().blocked_by
        if not b:
            return await event.respond("👌 There is no PAUSED job.")
        self.sup.repo.resume(b["archive_id"])
        await event.respond(f"▶️ <b>Back in the queue:</b> <code>{esc(b['archive_id'])}</code>\n📊 Send /status to follow progress.", parse_mode="html")

    async def cmd_requeue(self, event) -> None:
        failed = [r for r in self.sup.repo.list() if r["status"] == "FAILED"][-5:]
        if not failed:
            return await event.respond("👌 There is no cancelled/failed job.")
        btns = self._buttons([[(f"♻️ {r['filename'][:30]}", f"requeue:{r['archive_id']}".encode())] for r in failed])
        await event.respond("♻️ <b>Which job should go back in the queue?</b>", buttons=btns, parse_mode="html")

    # ------------------------------------------------------------------ DiskWala bot start / restart
    def _videos_busy(self) -> bool:
        w = self.sup.worker
        job = self.sup.repo.get(w.current) if (w and w.current) else None
        return bool(job and job["status"] == "UPLOADING_VIDEOS")

    async def _dw_ask(self, event, action: str) -> None:
        from telethon import Button
        if self.sup.client is None:
            return await event.respond("🔐 Please /login first (the DiskWala bot is driven from your Telegram account).")
        busy = self._videos_busy()
        if action == "start" and not busy:
            return await self._dw_run(event.chat_id, "start")
        what = (f"🔁 <b>Restart the DiskWala bot?</b>\n{DIV}\nThe bot will be stopped (blocked) → unblocked → sent /start. Its old state is reset."
                if action == "restart" else "🚀 <b>Send /start to the DiskWala bot?</b>")
        if busy:
            what += "\n\n⚠️ <b>Videos are uploading right now.</b> Doing this mid-upload may lose the link of the video in progress."
        await event.respond(what, parse_mode="html",
                            buttons=[[Button.inline("✅ Yes, do it", f"dw:{action}".encode()), Button.inline("↩️ No", b"dw:no")]])

    async def cmd_dwstart(self, event) -> None:
        await self._dw_ask(event, "start")

    async def cmd_dwrestart(self, event) -> None:
        await self._dw_ask(event, "restart")

    async def _dw_run(self, chat_id: int, action: str) -> None:
        from .diskwala_uploader import excerpt, looks_manual
        client = self.sup.client
        if client is None:
            return await self.bot.send_message(chat_id, "🔐 Please /login first.")
        name = self.cfg.uploader.bot_username
        try:
            ent = await client.get_entity(name)
            if action == "restart":
                from telethon.tl.functions.contacts import BlockRequest, UnblockRequest
                await client(BlockRequest(ent))
                await asyncio.sleep(2)
                await client(UnblockRequest(ent))
                await asyncio.sleep(1)
            sent = await client.send_message(ent, "/start")
            reply = None
            for _ in range(10):  # wait up to ~30s for the bot's reply
                await asyncio.sleep(3)
                msgs = await client.get_messages(ent, limit=10, min_id=sent.id)
                got = [m for m in reversed(msgs) if not m.out]
                if got:
                    reply = got[0]
                    break
            head = "🔁 <b>DiskWala bot restarted</b>" if action == "restart" else "🚀 <b>/start sent</b>"
            if reply is None:
                body = "⌛ The bot did not reply within 30s. Check your chat with @" + esc(name) + " in Telegram."
            else:
                body = f"💬 <b>Bot reply:</b>\n<i>{esc(excerpt(reply.raw_text or '', 300) or '(no text)')}</i>"
                if looks_manual(reply.raw_text or ""):
                    body += "\n\n⚠️ The bot needs a <b>manual step</b> (join / verify / captcha). Complete it in the bot's chat in Telegram."
            await self.bot.send_message(chat_id, f"{head}\n{body}", parse_mode="html")
        except Exception as e:  # noqa: BLE001
            log.warning("DiskWala %s failed: %s", action, type(e).__name__)
            await self.bot.send_message(chat_id, f"❌ DiskWala {action} failed: <code>{esc(type(e).__name__)}: {esc(str(e)[:160])}</code>",
                                        parse_mode="html")

    # ------------------------------------------------------------------ buttons
    async def _on_callback(self, event) -> None:
        from telethon.errors import MessageNotModifiedError
        if event.sender_id not in self.bs.admins:
            return await event.answer("⛔ Access denied.", alert=True)
        data = (event.data or b"").decode()
        chat_id = event.chat_id

        async def edit(text, btns=None):
            try:
                await event.edit(text, buttons=btns, parse_mode="html")
            except MessageNotModifiedError:
                pass

        if data == "refresh":
            text, btns, active = self._render()
            await edit(text, btns)
            await event.answer("🔄 Updated")
            if active and (chat_id not in self._cards or self._cards[chat_id].done()):
                self._cards[chat_id] = asyncio.create_task(self._card_loop(chat_id, await event.get_message()))
        elif data == "cancel_ask":
            self._stop_card(chat_id)
            text, btns = self._cancel_prompt()
            await edit(text, btns)
            await event.answer()
        elif data == "cancel_no":
            text, btns, active = self._render()
            await edit(text, btns)
            await event.answer()
            if active:
                self._cards[chat_id] = asyncio.create_task(self._card_loop(chat_id, await event.get_message()))
        elif data == "cancel_yes":
            self._stop_card(chat_id)
            w = self.sup.worker
            aid = w.cancel_current() if w else None
            await edit(f"🛑 <b>Task cancelled</b>\n{DIV}\n🆔 <code>{esc(aid)}</code>\n\n⏭ The queue is moving on.\n♻️ Use /requeue to bring it back."
                       if aid else "✅ The task had already finished.",
                       self._buttons([[("📊 Status", b"refresh")]]))
            await event.answer("🛑 Cancelled" if aid else "Nothing is running")
        elif data == "logout_yes":
            await edit("⏳ Logging out…")
            res = await self.sup.logout()
            await edit("👋 <b>Logged out.</b>\n🔐 Send /login to sign in again." if res == "ok" else esc(res))
            await event.answer()
        elif data == "logout_no":
            await edit("👍 Okay, you are still logged in.")
            await event.answer()
        elif data in ("dw:start", "dw:restart"):
            await edit("⏳ Working on the DiskWala bot…")
            await event.answer()
            await self._dw_run(chat_id, data[3:])
        elif data == "dw:no":
            await edit("👍 Okay, nothing was changed.")
            await event.answer()
        elif data == "login_cancel":
            if self._login_task and not self._login_task.done():
                self._login_task.cancel()
            await event.answer("✖️ Cancelled")
        elif data.startswith("resume:"):
            try:
                self.sup.repo.resume(data[7:])
                await edit(f"▶️ <b>Back in the queue:</b> <code>{esc(data[7:])}</code>", self._buttons([[("📊 Status", b"refresh")]]))
            except Exception as e:  # noqa: BLE001
                await edit(f"❌ {esc(e)}")
            await event.answer()
        elif data.startswith("requeue:"):
            try:
                self.sup.repo.requeue(data[8:])
                await edit(f"♻️ <b>Re-queued:</b> <code>{esc(data[8:])}</code>", self._buttons([[("📊 Status", b"refresh")]]))
            except Exception as e:  # noqa: BLE001
                await edit(f"❌ {esc(e)}")
            await event.answer()
        else:
            await event.answer()
