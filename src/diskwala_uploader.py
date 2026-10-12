"""DiskWala bot ko videos bhejna aur bot ka reply (link) capture karna.

Design notes (Hinglish):
- Ek time par sirf ek video. Link tabhi accept hota hai jab reply us video ke bheje gaye message ke BAAD aaya ho.
- Agar bot ka reply kisi aur message ka reply hai (reply_to alag), to ignore - galat video se link nahi jud sakta.
- Network error par blind retry nahi: pehle check hota hai ki file kahin bot tak pahunch to nahi gayi.
- Bot ke commands/format invent nahi kiye. CAPTCHA/join/verify jaisa kuch dikhe to rukkar user se poochta hai.
- Telegram ya bot ke rate-limit/protection ko bypass nahi karta.
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Protocol
from urllib.parse import urlparse

from .manifest import utc_now
from .upload_store import AWAITING, COMPLETED, FAILED, INTERRUPTED, UPLOADING, UploadStore
from .zip_processor import MB

# ----------------------------------------------------------------------------- parsing

URL_RE = re.compile(r"(?i)(?:https?://|www\.)[^\s<>\"'\u200b]+")
TRAILING = ".,;:!?)]}>*'\""

MANUAL_RE = re.compile(
    r"captcha|not a robot|are you (a )?human|prove (that )?you|please verify|verify (yourself|that you|you are)"
    r"|must (first )?join|please join|join (our|the|this|my) (official )?(channel|group)|subscribe to"
    r"|(click|press|send|use|type) /start|start the bot",
    re.I,
)
RATE_RE = re.compile(
    r"too many requests|rate.?limit|quota|limit (exceeded|reached)|daily limit|try again (later|after)|cooldown|flood",
    re.I,
)
ERROR_RE = re.compile(
    r"unsupported|not supported|too (large|big)|file is too|failed|error|invalid|couldn.?t|could not|unable to"
    r"|not allowed|banned|blocked|forbidden",
    re.I,
)


@dataclass
class BotReply:
    id: int
    text: str = ""
    entity_urls: tuple = ()
    button_urls: tuple = ()
    reply_to_id: int | None = None


def normalize_url(raw: str) -> str | None:
    u = raw.strip().rstrip(TRAILING)
    if u.lower().startswith("www."):
        u = "https://" + u
    p = urlparse(u)
    host = (p.hostname or "")
    if p.scheme not in ("http", "https") or "." not in host:
        return None
    return u


def extract_urls(text: str, entity_urls=(), button_urls=(), domains=()) -> list[str]:
    """Text + hidden (entity) links + inline-button links. Order: text, entities, buttons. Duplicate hatao."""
    raw = URL_RE.findall(text or "") + list(entity_urls) + list(button_urls)
    out: list[str] = []
    for r in raw:
        u = normalize_url(r)
        if not u or u in out:
            continue
        host = (urlparse(u).hostname or "").lower()
        if domains and not any(host == d or host.endswith("." + d) for d in domains):
            continue
        out.append(u)
    return out


def looks_manual(text: str) -> bool:
    return bool(MANUAL_RE.search(text or ""))


def looks_rate_limited(text: str) -> bool:
    return bool(RATE_RE.search(text or ""))


def looks_bot_error(text: str) -> bool:
    return bool(ERROR_RE.search(text or ""))


def excerpt(text: str, n: int = 200) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


# ----------------------------------------------------------------------------- errors

class UploaderError(Exception):
    pass


class UploadFailed(Exception):
    """Ek video fail - baaki chalte rahenge."""


class FatalUpload(UploadFailed):
    """Poora batch rokna chahiye (account/bot level problem)."""


FATAL_NAMES = {"PeerFloodError", "UserIsBlockedError", "AuthKeyUnregisteredError", "ChatWriteForbiddenError",
               "UserDeactivatedError", "UserDeactivatedBanError", "InputUserDeactivatedError"}
TRANSIENT_NAMES = {"ServerError", "TimedOutError", "RpcCallFailError", "RpcMcgetFailError",
                   "InterdcCallErrorError", "FilePartMissingError"}
FLOOD_NAMES = {"FloodWaitError", "SlowModeWaitError", "FloodPremiumWaitError"}


def flood_seconds(e: BaseException) -> int | None:
    if type(e).__name__ in FLOOD_NAMES:
        return int(getattr(e, "seconds", 30) or 30)
    return None


def is_fatal(e: BaseException) -> bool:
    return type(e).__name__ in FATAL_NAMES


def is_transient(e: BaseException) -> bool:
    return isinstance(e, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError)) or \
        type(e).__name__ in TRANSIENT_NAMES


# ----------------------------------------------------------------------------- bot channel

class BotChannel(Protocol):
    async def latest_id(self) -> int: ...
    async def send_text(self, text: str) -> int: ...
    async def send_video(self, path: Path, progress: Callable[[int, int], None]) -> int: ...
    async def fetch_replies(self, after_id: int) -> list[BotReply]: ...
    async def find_recent_outgoing(self, after_id: int, size: int) -> int | None: ...


def reply_from_message(m) -> BotReply:
    """Telethon Message -> BotReply (caption, hidden links, inline buttons sab)."""
    ent_urls = tuple(u for u in (getattr(e, "url", None) for e in (m.entities or [])) if u)
    btn_urls: list[str] = []
    for row in (getattr(m, "buttons", None) or []):
        for b in (row if isinstance(row, (list, tuple)) else [row]):
            u = getattr(b, "url", None)
            if u:
                btn_urls.append(u)
    return BotReply(id=m.id, text=m.raw_text or "", entity_urls=ent_urls,
                    button_urls=tuple(btn_urls), reply_to_id=getattr(m, "reply_to_msg_id", None))


class TelethonBotChannel:
    """Real Telethon client ke upar BotChannel."""

    def __init__(self, client, entity, as_document: bool = True):
        self.client, self.entity, self.as_document = client, entity, as_document

    @classmethod
    async def connect(cls, client, username: str, as_document: bool = True) -> "TelethonBotChannel":
        try:
            entity = await client.get_entity(username)
        except Exception as e:  # noqa: BLE001
            raise UploaderError(f"Bot @{username} nahi mila ({type(e).__name__}).") from e
        if not getattr(entity, "bot", False):
            raise UploaderError(f"@{username} bot nahi hai - kuch aur account hai. Roka gaya.")
        if (getattr(entity, "username", "") or "").lower() != username.lower():
            raise UploaderError(f"Username match nahi hua: mila @{getattr(entity, 'username', None)}.")
        return cls(client, entity, as_document)

    async def latest_id(self) -> int:
        msgs = await self.client.get_messages(self.entity, limit=1)
        return msgs[0].id if msgs else 0

    async def send_text(self, text: str) -> int:
        return (await self.client.send_message(self.entity, text)).id

    async def send_video(self, path: Path, progress) -> int:
        msg = await self.client.send_file(
            self.entity, str(path), force_document=self.as_document,
            supports_streaming=not self.as_document, progress_callback=progress)
        return msg.id

    async def fetch_replies(self, after_id: int) -> list[BotReply]:
        msgs = await self.client.get_messages(self.entity, limit=30, min_id=after_id)
        return [reply_from_message(m) for m in msgs if not m.out]

    async def find_recent_outgoing(self, after_id: int, size: int) -> int | None:
        msgs = await self.client.get_messages(self.entity, limit=10, min_id=after_id)
        for m in msgs:
            if m.out and m.file and int(m.file.size or 0) == size:
                return m.id
        return None


# ----------------------------------------------------------------------------- UI / control

class NullUI:
    def start(self, i, n, rec): ...
    def status(self, msg): ...
    def progress(self, done, total): ...
    def result(self, rec): ...


class TerminalUI(NullUI):
    def __init__(self):
        self._last = -10

    def start(self, i, n, rec):
        self._last = -10
        print(f"\n[{i}/{n}] {rec['filename']}  ({rec['size'] / MB:.1f} MB)")

    def status(self, msg):
        print(f"  {msg}")

    def progress(self, done, total):
        if not total:
            return
        pct = int(done * 100 / total)
        if pct >= self._last + 10:
            self._last = pct - pct % 10
            print(f"  Uploading: {pct}%")

    def result(self, rec):
        if rec["status"] == COMPLETED:
            print(f"  DONE -> {rec['url']}" + ("   (review karo: match pakka nahi)" if rec.get("needs_review") else ""))
        else:
            print(f"  {rec['status'].upper()}: {rec['error']}")


class ControlFile:
    """pause / resume / stop ke liye chhoti text file (dusre terminal se control ho sakta hai)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> str:
        try:
            v = self.path.read_text(encoding="utf-8").strip().lower()
        except OSError:
            return "run"
        return v if v in ("run", "pause", "stop") else "run"

    def write(self, cmd: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(cmd, encoding="utf-8")


# ----------------------------------------------------------------------------- engine

@dataclass
class UploaderConfig:
    bot_username: str = "DiskWalaFileUploaderBot"
    response_timeout: float = 180.0
    poll_interval: float = 3.0
    max_retries: int = 2
    max_upload_bytes: int = 2000 * MB
    link_domains: tuple = ()
    delay_between: float = 3.0
    max_flood_wait: int = 900
    max_flood_retries: int = 5


@dataclass
class Outcome:
    kind: str  # link | timeout | no_link | bot_error | rate_limited | cancelled
    urls: list = field(default_factory=list)
    reply_id: int | None = None
    match: str | None = None
    error: str | None = None
    excerpt: str | None = None


@dataclass
class RunSummary:
    completed: int = 0
    failed: int = 0
    interrupted: int = 0
    stopped_reason: str | None = None


ManualHandler = Callable[[BotReply], Awaitable[str]]  # returns "continue" | "accept" | "quit"


async def _no_manual(_reply: BotReply) -> str:
    return "quit"


class DiskWalaUploader:
    def __init__(self, channel: BotChannel, store: UploadStore, cfg: UploaderConfig, *,
                 ui: NullUI | None = None, control: ControlFile | None = None,
                 on_manual: ManualHandler | None = None, sleep=asyncio.sleep, now=time.monotonic):
        self.channel, self.store, self.cfg = channel, store, cfg
        self.ui = ui or NullUI()
        self.control = control
        self.on_manual = on_manual or _no_manual
        self.sleep, self.now = sleep, now
        self._fatal: str | None = None
        self._attribution_risk = False  # pichla video timeout/ruka => late reply ka khatra

    # ---- conversation check -------------------------------------------------
    async def prepare(self) -> None:
        """Bot chat khaali ho (kabhi start nahi hua) tabhi /start bhejo."""
        if await self.channel.latest_id() != 0:
            return
        self.ui.status("Bot ke saath chat khaali hai -> /start bhej raha hoon (sirf ek baar).")
        start_id = await self.channel.send_text("/start")
        deadline = self.now() + 30
        while self.now() < deadline:
            replies = [r for r in await self.channel.fetch_replies(start_id) if r.reply_to_id in (None, start_id)]
            if replies:
                r = replies[0]
                self.ui.status(f"Bot ka reply: {excerpt(r.text)}")
                if looks_manual(r.text):
                    if await self.on_manual(r) == "quit":
                        raise UploaderError("Manual step poora nahi hua. Telegram me bot ke saath complete karke dobara chalao.")
                return
            await self.sleep(self.cfg.poll_interval)

    # ---- batch loop ---------------------------------------------------------
    async def run(self, work: list[dict]) -> RunSummary:
        summary = RunSummary()
        for i, rec in enumerate(work, 1):
            if self.control and not await self._gate():
                summary.stopped_reason = "Tumne stop kiya (upload-control stop)."
                break
            self.ui.start(i, len(work), rec)
            status = await self.upload_one(rec)
            self.ui.result(rec)
            if status == COMPLETED:
                summary.completed += 1
            elif status == INTERRUPTED:
                summary.interrupted += 1
            else:
                summary.failed += 1
            if self._fatal:
                summary.stopped_reason = self._fatal
                break
            if i < len(work):
                await self.sleep(self.cfg.delay_between)
        return summary

    async def _gate(self) -> bool:
        """True = aage badho, False = stop. Pause me yahin ruka rehta hai."""
        announced = False
        while True:
            cmd = self.control.read()
            if cmd == "stop":
                return False
            if cmd != "pause":
                return True
            if not announced:
                self.ui.status("PAUSED - 'python -m src.main upload-control resume' se aage badhao.")
                announced = True
            await self.sleep(2)

    # ---- single video -------------------------------------------------------
    async def upload_one(self, rec: dict) -> str:
        st = self.store
        st.update(rec, status=UPLOADING, error=None, url=None, urls=[], match=None, needs_review=False,
                  bot_reply_excerpt=None, sent_message_id=None, reply_message_id=None,
                  attempts=rec["attempts"] + 1, started_at=utc_now(), finished_at=None)
        try:
            path = self._check_file(rec)
            sent_id = await self._send(rec, path)
            st.update(rec, status=AWAITING, sent_message_id=sent_id)
            self.ui.status("Upload ho gaya. Bot ke reply ka intezaar...")
            out = await self._wait_for_link(rec, sent_id)
            return self._apply(rec, out)
        except asyncio.CancelledError:
            st.update(rec, status=INTERRUPTED, finished_at=utc_now(),
                      error="Beech me roka gaya (Ctrl+C). Bot ko file mili ho sakti hai - chat check karo.")
            self._attribution_risk = True
            raise
        except FatalUpload as e:
            self._fatal = str(e)
            st.update(rec, status=FAILED, error=str(e), finished_at=utc_now())
            return FAILED
        except UploadFailed as e:
            st.update(rec, status=FAILED, error=str(e), finished_at=utc_now())
            return FAILED

    def _check_file(self, rec: dict) -> Path:
        path = Path(rec["path"])
        if not path.is_file():
            raise UploadFailed(f"File disk par nahi mili: {rec['path']}")
        size = path.stat().st_size
        if size != rec["size"]:
            raise UploadFailed(f"File size badal gaya (manifest {rec['size']} vs disk {size}). Skip.")
        if size > self.cfg.max_upload_bytes:
            raise UploadFailed(f"Telegram limit se bada: {size // MB} MB > {self.cfg.max_upload_bytes // MB} MB.")
        if size == 0:
            raise UploadFailed("File khaali hai.")
        return path

    async def _send(self, rec: dict, path: Path) -> int:
        base_id = await self.channel.latest_id()
        retries = floods = 0
        while True:
            try:
                return await self.channel.send_video(path, self.ui.progress)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                secs = flood_seconds(e)
                if secs is not None:  # FloodWait: message gaya hi nahi, retry safe hai
                    floods += 1
                    if secs > self.cfg.max_flood_wait or floods > self.cfg.max_flood_retries:
                        raise FatalUpload(f"Telegram FloodWait {secs}s - batch roka. Baad me dobara chalao.") from e
                    self.ui.status(f"Telegram ne {secs}s rukne ko kaha - ruk raha hoon...")
                    await self.sleep(secs + 1)
                    continue
                if is_fatal(e):
                    raise FatalUpload(f"{type(e).__name__}: Telegram ne is account/bot par rok lagayi. Batch roka.") from e
                if is_transient(e):
                    try:
                        already = await self.channel.find_recent_outgoing(base_id, rec["size"])
                    except Exception:  # noqa: BLE001
                        already = None
                    if already:  # file pahunch chuki thi => dobara bhejna = duplicate
                        self.ui.status("Error aaya par file pahunch chuki hai - dobara nahi bhej raha.")
                        return already
                    retries += 1
                    if retries > self.cfg.max_retries:
                        raise UploadFailed(f"Network/temporary error ({type(e).__name__}) - {retries - 1} retry ke baad bhi fail.") from e
                    wait = 5 * retries
                    self.ui.status(f"Temporary error ({type(e).__name__}). {wait}s baad retry {retries}/{self.cfg.max_retries}...")
                    await self.sleep(wait)
                    continue
                raise UploadFailed(f"{type(e).__name__}: {e}") from e

    async def _wait_for_link(self, rec: dict, sent_id: int) -> Outcome:
        cfg = self.cfg
        deadline = self.now() + cfg.response_timeout
        seen: dict[int, tuple] = {}
        last_texts: list[str] = []
        while True:
            for r in sorted(await self.channel.fetch_replies(sent_id), key=lambda x: x.id):
                if r.id <= sent_id or r.reply_to_id not in (None, sent_id):
                    continue  # purane ya kisi aur upload ke reply
                urls = extract_urls(r.text, r.entity_urls, r.button_urls, cfg.link_domains)
                sig = (r.text, tuple(urls))
                if seen.get(r.id) == sig:
                    continue
                seen[r.id] = sig

                if looks_rate_limited(r.text):
                    return Outcome("rate_limited", reply_id=r.id, excerpt=excerpt(r.text),
                                   error=f"Bot ne limit/rate-limit bataya: '{excerpt(r.text)}'. Batch roka (bypass nahi kiya).")
                if looks_manual(r.text):
                    self.ui.status(f"Bot ko manual step chahiye: {excerpt(r.text)}")
                    choice = await self.on_manual(r)
                    if choice == "accept" and urls:
                        return self._link(rec, r, urls, sent_id, match="user_confirmed")
                    if choice == "quit":
                        return Outcome("cancelled", reply_id=r.id, excerpt=excerpt(r.text),
                                       error="Manual step pe tumne roka. Telegram me complete karke --retry-interrupted chalao.")
                    deadline = self.now() + cfg.response_timeout
                    continue
                if urls:
                    return self._link(rec, r, urls, sent_id)
                if looks_bot_error(r.text):
                    return Outcome("bot_error", reply_id=r.id, excerpt=excerpt(r.text),
                                   error=f"Bot ne error bataya: '{excerpt(r.text)}'")
                last_texts.append(excerpt(r.text) or "(bina text ka reply)")
                self.ui.status(f"Bot: {excerpt(r.text, 80)}")
            if self.now() >= deadline:
                break
            await self.sleep(cfg.poll_interval)

        if last_texts:
            return Outcome("no_link", excerpt=last_texts[-1], error=(
                f"{int(cfg.response_timeout)}s me bot ke {len(last_texts)} reply aaye par kisi me valid http(s) link nahi mila. "
                f"Aakhri reply: '{last_texts[-1]}'"
                + (f" (DISKWALA_LINK_DOMAINS filter on hai: {', '.join(cfg.link_domains)})" if cfg.link_domains else "")))
        return Outcome("timeout", error=f"Timeout: {int(cfg.response_timeout)}s me bot ne koi reply nahi diya.")

    def _link(self, rec: dict, r: BotReply, urls: list, sent_id: int, match: str | None = None) -> Outcome:
        if match is None:
            if r.reply_to_id == sent_id:
                match = "reply_to"
            elif rec["filename"].lower() in (r.text or "").lower():
                match = "filename"
            else:
                match = "sequential"
        return Outcome("link", urls=urls, reply_id=r.id, match=match, excerpt=excerpt(r.text))

    def _apply(self, rec: dict, out: Outcome) -> str:
        st = self.store
        now = utc_now()
        if out.kind == "link":
            review = out.match == "sequential" and self._attribution_risk
            if out.match in ("reply_to", "filename", "user_confirmed"):
                self._attribution_risk = False
            st.update(rec, status=COMPLETED, url=out.urls[0], urls=out.urls, error=None, match=out.match,
                      reply_message_id=out.reply_id, bot_reply_excerpt=out.excerpt, needs_review=review, finished_at=now)
            return COMPLETED
        if out.kind == "cancelled":
            self._attribution_risk = True
            st.update(rec, status=INTERRUPTED, error=out.error, bot_reply_excerpt=out.excerpt, finished_at=now)
            return INTERRUPTED
        if out.kind == "rate_limited":
            self._fatal = out.error
        if out.kind in ("timeout", "no_link"):
            self._attribution_risk = True  # late reply aage ke video se mix ho sakta hai
        st.update(rec, status=FAILED, error=out.error, bot_reply_excerpt=out.excerpt, finished_at=now)
        return FAILED


# ----------------------------------------------------------------------------- CLI helpers

def print_preview(work: list[dict], store: UploadStore, limit_note: str = "") -> None:
    total = sum(r["size"] for r in work)
    print(f"\nUpload preview{limit_note}: {len(work)} video(s), total {total / MB:.1f} MB")
    for r in work:
        print(f"  - {r['filename']}  ({r['size'] / MB:.1f} MB)  [{r['status']}]")
    c = store.counts()
    print("Store me status:", ", ".join(f"{k}={v}" for k, v in sorted(c.items())) or "(khaali)")


def print_summary(store: UploadStore, run: RunSummary | None = None) -> None:
    print("\n==== Summary ====")
    if run:
        print(f"Is run me: completed={run.completed}, failed={run.failed}, interrupted={run.interrupted}")
        if run.stopped_reason:
            print(f"Roka gaya: {run.stopped_reason}")
    done = [r for r in store.files if r["status"] == COMPLETED]
    print(f"\nSuccessful links ({len(done)}):")
    for r in done:
        print(f"  {r['filename']}  ->  {r['url']}" + ("   [REVIEW]" if r.get("needs_review") else ""))
    bad = [r for r in store.files if r["status"] in ("failed", "interrupted")]
    if bad:
        print(f"\nFailed / interrupted ({len(bad)}):")
        for r in bad:
            print(f"  {r['filename']}  [{r['status']}]  {r['error']}")


async def cli_manual_prompt(reply: BotReply) -> str:
    print("\n--- BOT KO TUMHARI MADAD CHAHIYE ---")
    print(f"Bot ka message: {excerpt(reply.text, 400)}")
    urls = extract_urls(reply.text, reply.entity_urls, reply.button_urls)
    print("Telegram app me bot ke chat me jaakar (CAPTCHA / join / verify) khud complete karo.")
    if not sys.stdin.isatty():
        print("Terminal interactive nahi hai, isliye ruk raha hoon.")
        return "quit"
    msg = "Complete karke Enter dabao (aage badhne ke liye), 'q' = roko"
    if urls:
        print(f"Is message me link mila: {urls[0]}")
        msg += ", 'a' = ye link hi sahi result hai"
    ans = (await asyncio.to_thread(input, msg + ": ")).strip().lower()
    return "quit" if ans == "q" else "accept" if (ans == "a" and urls) else "continue"
