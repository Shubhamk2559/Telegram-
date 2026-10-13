"""Telegram control bot (Telethon bot account): /login (QR) /logout /status (live card) /cancel /queue /resume /requeue.

Sirf ADMIN_IDS wale users chala sakte hain. 2FA password env se aata hai (TELEGRAM_2FA_PASSWORD), chat me kabhi nahi
poocha jaata aur log me kabhi nahi aata. Login ke baad session string DATABASE me save hoti hai (logout tak rehti hai).
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
from .status import esc, render_idle, render_job, render_paused

log = logging.getLogger("control_bot")

QR_WAIT = 25          # sec: ek QR ki umar (Telegram ~30s)
QR_REFRESHES = 12     # ~5 min
CARD_TICK = 4.0       # sec: live card edit interval (bot ko flood se bachata hai)
CARD_MAX_TICKS = 600  # ~40 min ke baad auto-refresh band; Refresh button se phir chalega
PW_NAMES = ("TELEGRAM_2FA_PASSWORD", "TWO_FA_PASSWORD", "TG_2FA_PASSWORD", "TELEGRAM_PASSWORD", "PASSWORD_2FA")

COMMANDS = [("login", "QR se Telegram login"), ("status", "Live progress card"), ("cancel", "Chal rahi task cancel"),
            ("queue", "Saari ZIP jobs"), ("resume", "PAUSED job wapas queue me"), ("requeue", "Cancelled/failed job wapas"),
            ("logout", "Telegram session hatao"), ("help", "Madad")]


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


def make_qr_png(url: str) -> bytes | None:
    try:
        import qrcode
        q = qrcode.QRCode(border=3, box_size=10)
        q.add_data(url)
        q.make(fit=True)
        buf = io.BytesIO()
        q.make_image(fill_color="black", back_color="white").save(buf)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - Pillow nahi hai: sirf link bhejenge
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
            raise ConfigError(f"Control bot start nahi hua ({type(e).__name__}). BOT_TOKEN check karo.") from e
        self.bot.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        self.bot.add_event_handler(self._on_callback, events.CallbackQuery())
        self.sup.notify = self.notify
        try:
            from telethon.tl.functions.bots import SetBotCommandsRequest
            from telethon.tl.types import BotCommand, BotCommandScopeDefault
            await self.bot(SetBotCommandsRequest(scope=BotCommandScopeDefault(), lang_code="",
                                                 commands=[BotCommand(command=c, description=d) for c, d in COMMANDS]))
        except Exception as e:  # noqa: BLE001
            log.warning("Bot commands menu set nahi hua: %s", type(e).__name__)
        me = await self.bot.get_me()
        log.info("Control bot ready: @%s (admins: %s)", me.username, len(self.bs.admins))
        if not self.bs.admins:
            log.warning("ADMIN_IDS set nahi hai: koi bhi command nahi chalegi. Bot ko /start bhejo, wo tumhari ID batayega.")

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
            except Exception as e:  # noqa: BLE001 - admin ne bot /start nahi kiya ho sakta hai
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
            await event.respond(f"⛔ Access nahi hai.\nTumhari ID: <code>{uid}</code>\n"
                                f"Admin banne ke liye Koyeb me <code>ADMIN_IDS={uid}</code> set karke redeploy karo.",
                                parse_mode="html")
            return
        fn = {"start": self.cmd_help, "help": self.cmd_help, "login": self.cmd_login, "logout": self.cmd_logout,
              "status": self.cmd_status, "cancel": self.cmd_cancel, "queue": self.cmd_queue,
              "resume": self.cmd_resume, "requeue": self.cmd_requeue}.get(cmd)
        if not fn:
            return await event.respond("Ye command nahi pata. /help dekho.")
        try:
            await fn(event)
        except Exception as e:  # noqa: BLE001
            log.exception("command /%s fail", cmd)
            await event.respond(f"❌ Error: <code>{esc(type(e).__name__)}: {esc(str(e)[:200])}</code>", parse_mode="html")

    def _acct_line(self) -> str:
        st = self.sup.state
        if st == "running":
            return f"🟢 Logged in{' · ' + esc(self.sup.me_name) if self.sup.me_name else ''} · worker chal raha hai"
        return {"needs_login": "🔐 Logged out · /login karo", "connecting": "⏳ Telegram se connect ho raha hai…",
                "starting": "⏳ Start ho raha hai…"}.get(st, f"⚪ {esc(st)}")

    async def cmd_help(self, event) -> None:
        warn = ""
        if self.sup.db.kind == "sqlite":
            warn = ("\n\n⚠️ <b>Database SQLite hai</b> (redeploy par ud jaata hai): login, queue aur Telegraph account "
                    "har deploy par reset honge. Koyeb/Neon ka PostgreSQL <code>DATABASE_URL</code> lagao.")
        await event.respond(
            f"🤖 <b>ZIP Worker Control</b>\n{self._acct_line()}\n\n"
            "/login — QR se login (2FA password env se)\n/logout — session hatao\n/status — live progress card\n"
            "/cancel — chal rahi task cancel\n/queue — saari jobs\n/resume — PAUSED job chalao\n"
            "/requeue — cancelled/failed job wapas" + warn, parse_mode="html")

    # ------------------------------------------------------------------ login / logout
    async def cmd_login(self, event) -> None:
        if self.sup.session_string()[0]:
            return await event.respond(f"{self._acct_line()}\nPehle se logged in hai. Naya login chahiye to pehle /logout.",
                                       parse_mode="html")
        s = self.cfg.settings
        if not (s.api_id and s.api_hash):
            return await event.respond("❌ API_ID / API_HASH set nahi hain.")
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
                        raise LoginError("2-step verification laga hai par 2FA password env set nahi. Koyeb me "
                                         "<code>TELEGRAM_2FA_PASSWORD</code> daalo, redeploy karo, phir /login.")
                    try:
                        await uc.sign_in(password=self.bs.password)
                    except PasswordHashInvalidError:
                        raise LoginError("2FA password galat hai (Koyeb variable check karo).") from None
                    break
            else:
                raise LoginError("QR expire ho gaya (5 min). Dobara /login bhejo.")
            if not await uc.is_user_authorized():
                raise LoginError("Login complete nahi hua. Dobara /login.")
            me = await uc.get_me()
            session = StringSession.save(uc.session)
            await self._drop(msg)
            self.sup.save_session(session)  # database me save (logout tak rehti hai) + worker start signal
            name = " ".join(x for x in (me.first_name, me.last_name) if x) or str(me.id)
            await self.bot.send_message(chat_id, f"✅ <b>Login ho gaya:</b> {esc(name)}\nSession save hui. Worker shuru ho raha hai…\n"
                                                 "/status se dekho.", parse_mode="html")
            if self.sup.db.kind == "sqlite":
                await self.bot.send_message(chat_id, "⚠️ Database SQLite hai: redeploy par ye login ud jayega. PostgreSQL <code>DATABASE_URL</code> lagao.", parse_mode="html")
        except asyncio.CancelledError:
            await self._drop(msg)
            await self.bot.send_message(chat_id, "✖️ Login cancel.")
        except LoginError as e:
            await self._drop(msg)
            await self.bot.send_message(chat_id, f"❌ {e}", parse_mode="html")
        except Exception as e:  # noqa: BLE001
            log.warning("Login fail: %s", type(e).__name__)
            await self._drop(msg)
            await self.bot.send_message(chat_id, f"❌ Login fail: <code>{esc(type(e).__name__)}</code>\nDobara /login.", parse_mode="html")
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
        caption = ("📲 <b>Telegram Login</b>\n\n1️⃣ Telegram kholo → <b>Settings → Devices → Link Desktop Device</b>\n"
                   "2️⃣ Is QR ko scan karo — ya neeche <b>Open in Telegram</b> dabao\n\n"
                   "⏳ QR har ~25s me apne aap badalta hai.")
        png = make_qr_png(url)
        if png is None:
            caption += f"\n\n🔗 <code>{esc(url)}</code>"
        cancel = [Button.inline("✖️ Cancel", b"login_cancel")]
        variants = ([[Button.url("📲 Open in Telegram", url)], cancel], [cancel])  # tg:// button reject ho to bina url ke

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
            return await event.respond("Pehle se logged out hai. /login")
        await event.respond("🚪 <b>Pakka logout?</b>\nTelegram se session terminate hoga, chal rahi task ruk jayegi "
                            "(kaam DB me safe rehta hai). Dobara /login lagega.",
                            buttons=[[Button.inline("✅ Haan, logout", b"logout_yes"), Button.inline("↩️ Nahi", b"logout_no")]],
                            parse_mode="html")

    # ------------------------------------------------------------------ status card
    def _buttons(self, rows):
        from telethon import Button
        return [[Button.inline(t, d) for t, d in row] for row in rows]

    def _render(self):
        """(text, buttons, active) active=True => card live-refresh hota rahe."""
        sup, repo = self.sup, self.sup.repo
        refresh = ("🔄 Refresh", b"refresh")
        if sup.state == "needs_login":
            return "🔐 <b>Login nahi hai.</b>\n/login se QR login karo.", self._buttons([[refresh]]), False
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
            return (f"⏳ <b>Agla:</b> {esc(sel.job['filename'])}\nWorker abhi uthane wala hai…", self._buttons([[refresh]]), True)
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
        except Exception as e:  # noqa: BLE001 - message delete ho gaya etc.
            log.debug("card loop band: %s", type(e).__name__)

    # ------------------------------------------------------------------ cancel / queue / resume
    def _cancel_prompt(self):
        w = self.sup.worker
        if not (w and w.current):
            return "Abhi koi task chal nahi raha.", None
        job = self.sup.repo.get(w.current) or {}
        return (f"⛔ <b>Ye task cancel karun?</b>\n📦 {esc(job.get('filename', w.current))}\n\n"
                "Ho chuka kaam (ImgBB/Telegraph/publish ho chuke posts) delete nahi hota. Queue aage badhegi. "
                "Wapas lene ke liye /requeue.",
                self._buttons([[("✅ Haan, cancel", b"cancel_yes"), ("↩️ Nahi", b"cancel_no")]]))

    async def cmd_cancel(self, event) -> None:
        text, btns = self._cancel_prompt()
        await event.respond(text, buttons=btns, parse_mode="html")

    async def cmd_queue(self, event) -> None:
        rows = self.sup.repo.list()
        if not rows:
            return await event.respond("Queue khaali hai.")
        icon = {"COMPLETED": "✅", "FAILED": "❌", "PAUSED": "⏸", "QUEUED": "⏳", "DISCOVERED": "⏳"}
        lines = [f"{icon.get(r['status'], '🔄')} <code>{esc(r['archive_id'])}</code> {esc(r['filename'][:34])} · {r['status'].lower()}"
                 for r in rows[-12:]]
        await event.respond("📋 <b>Queue</b> (latest 12)\n" + "\n".join(lines), parse_mode="html")

    async def cmd_resume(self, event) -> None:
        b = self.sup.repo.next_job().blocked_by
        if not b:
            return await event.respond("Koi PAUSED job nahi hai.")
        self.sup.repo.resume(b["archive_id"])
        await event.respond(f"▶️ Queue me wapas: <code>{esc(b['archive_id'])}</code>\n/status se dekho.", parse_mode="html")

    async def cmd_requeue(self, event) -> None:
        failed = [r for r in self.sup.repo.list() if r["status"] == "FAILED"][-5:]
        if not failed:
            return await event.respond("Koi cancelled/failed job nahi hai.")
        btns = self._buttons([[(f"♻️ {r['filename'][:30]}", f"requeue:{r['archive_id']}".encode())] for r in failed])
        await event.respond("Kaun si job wapas queue me daalni hai?", buttons=btns)

    # ------------------------------------------------------------------ buttons
    async def _on_callback(self, event) -> None:
        from telethon.errors import MessageNotModifiedError
        if event.sender_id not in self.bs.admins:
            return await event.answer("Access nahi hai.", alert=True)
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
            await event.answer("Updated")
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
            await edit(f"🛑 <b>Cancel ho gaya</b>\n<code>{esc(aid)}</code>\nQueue aage badh rahi hai. Wapas lene ke liye /requeue."
                       if aid else "Task pehle hi khatam ho chuka tha.",
                       self._buttons([[("📊 Status", b"refresh")]]))
            await event.answer("Cancelled" if aid else "Kuch chal nahi raha")
        elif data == "logout_yes":
            await edit("⏳ Logout ho raha hai…")
            res = await self.sup.logout()
            await edit("👋 <b>Logout ho gaya.</b> Wapas login ke liye /login." if res == "ok" else esc(res))
            await event.answer()
        elif data == "logout_no":
            await edit("Theek hai, logout nahi kiya.")
            await event.answer()
        elif data == "login_cancel":
            if self._login_task and not self._login_task.done():
                self._login_task.cancel()
            await event.answer("Cancel")
        elif data.startswith("resume:"):
            try:
                self.sup.repo.resume(data[7:])
                await edit(f"▶️ Queue me wapas: <code>{esc(data[7:])}</code>", self._buttons([[("📊 Status", b"refresh")]]))
            except Exception as e:  # noqa: BLE001
                await edit(f"❌ {esc(e)}")
            await event.answer()
        elif data.startswith("requeue:"):
            try:
                self.sup.repo.requeue(data[8:])
                await edit(f"♻️ Queue me wapas: <code>{esc(data[8:])}</code>", self._buttons([[("📊 Status", b"refresh")]]))
            except Exception as e:  # noqa: BLE001
                await edit(f"❌ {esc(e)}")
            await event.answer()
        else:
            await event.answer()
