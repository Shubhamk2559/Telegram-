"""DiskWala videos: STRICT sequential. Existing DiskWalaUploader.upload_one reuse hota hai.

Guarantees:
- Videos size ke descending order me (posts.sort_videos). Ek time par sirf ek video awaiting.
- Agla video tabhi jab current ka valid link save ho chuka ho. Koi video kabhi skip nahi: fail ho to
  VideoBlocked raise hota hai aur ZIP pause hota hai (chhote videos aage nahi jaate).
- Restart recovery: AWAITING video ko dobara bheje bina uska reply wait karta hai. 'UPLOADING' me atke
  video ke liye chat dekhta hai ki file bot tak pahunchi ya nahi (pre_send_latest_id se); pata na chale to ruk jaata hai.
- Timeout par pehle late reply check, phir (policy ke hisaab se) resend, max attempts ke baad pause.
"""
from __future__ import annotations

from dataclasses import dataclass

from .diskwala_uploader import DiskWalaUploader, Outcome, UploaderConfig
from .posts import sort_videos
from .upload_store import AWAITING, COMPLETED, FAILED, INTERRUPTED, PENDING, UPLOADING

TRANSIENT_MARK = "Network/temporary error"


class VideoBlocked(Exception):
    """Ek video complete nahi hua -> ZIP pause. Agle videos ko haath nahi lagaya."""

    def __init__(self, filename: str, reason: str):
        super().__init__(f"{filename}: {reason}")
        self.filename, self.reason = filename, reason


@dataclass
class VideoPolicy:
    max_attempts: int = 3            # DISKWALA_MAX_RETRIES: ek video ke liye total sends (ek run me)
    resend_on_timeout: bool = True   # timeout/no-link par automatic resend (late-reply check ke baad)
    late_reply_grace: float = 300.0  # resend se pehle itni der aur wait


class StrictVideoUploader(DiskWalaUploader):
    def __init__(self, channel, store, cfg: UploaderConfig, *, policy: VideoPolicy | None = None, **kw):
        super().__init__(channel, store, cfg, **kw)
        self.policy = policy or VideoPolicy()
        self.last_kind: str | None = None

    # ---- hooks into existing uploader --------------------------------------
    def _apply(self, rec, out: Outcome) -> str:
        self.last_kind = out.kind
        return super()._apply(rec, out)

    async def upload_one(self, rec: dict) -> str:
        self.last_kind = None
        return await super().upload_one(rec)

    async def _wait_with(self, rec: dict, sent_id: int, timeout: float) -> str:
        old = self.cfg.response_timeout
        self.cfg.response_timeout = timeout
        try:
            return self._apply(rec, await self._wait_for_link(rec, sent_id))
        finally:
            self.cfg.response_timeout = old

    # ---- main entry ---------------------------------------------------------
    async def process_all(self, recs: list[dict]) -> int:
        """Sorted order me ek-ek video. Returns total completed. VideoBlocked = pause ZIP."""
        ordered = sort_videos(recs)
        for i, rec in enumerate(ordered):
            if rec["status"] == COMPLETED:
                continue
            await self._drive(rec)
            if self._fatal:
                raise VideoBlocked(rec["filename"], self._fatal)
            if i < len(ordered) - 1:
                await self.sleep(self.cfg.delay_between)
        return sum(r["status"] == COMPLETED for r in ordered)

    async def _drive(self, rec: dict) -> None:
        name = rec["filename"]
        if rec["status"] in (AWAITING, INTERRUPTED, UPLOADING, FAILED):
            if await self._recover(rec):
                return
        for attempt in range(1, self.policy.max_attempts + 1):
            self.store.update(rec, pre_send_latest_id=await self.channel.latest_id())
            status = await self.upload_one(rec)
            if status == COMPLETED:
                return
            if self._fatal:  # rate-limit / account level: resend bekaar
                raise VideoBlocked(name, self._fatal)
            err = rec.get("error") or "unknown error"
            if self.last_kind in ("timeout", "no_link"):
                if rec.get("sent_message_id"):  # late reply ho sakta hai: resend se pehle ek aur mauka
                    if await self._wait_with(rec, rec["sent_message_id"], self.policy.late_reply_grace) == COMPLETED:
                        return
                    if self._fatal:
                        raise VideoBlocked(name, self._fatal)
                if self.policy.resend_on_timeout and attempt < self.policy.max_attempts:
                    continue
            elif self.last_kind is None and TRANSIENT_MARK in err and attempt < self.policy.max_attempts:
                await self.sleep(min(30 * attempt, 300))
                continue
            raise VideoBlocked(name, f"{err} (attempt {attempt}/{self.policy.max_attempts})")

    async def _recover(self, rec: dict) -> bool:
        """Pichli run ka adhura video: reply mil gaya to True (resend nahi). Warna False => fresh send safe hai."""
        name, st = rec["filename"], rec["status"]
        sent = rec.get("sent_message_id")
        if not sent and st in (UPLOADING, INTERRUPTED, FAILED):
            pre = rec.get("pre_send_latest_id")
            if pre is not None:
                found = await self.channel.find_recent_outgoing(pre, rec["size"])
                if found:  # file bot tak pahunch chuki thi: dobara bhejna = duplicate
                    self.store.update(rec, status=AWAITING, sent_message_id=found, error=None)
                    sent = found
            elif st in (UPLOADING, INTERRUPTED):
                raise VideoBlocked(name, "Pichli baar beech me ruka; pata nahi file bot tak gayi ya nahi. "
                                         "Telegram chat dekh ke tay karo.")
        if not sent:
            return False
        self._attribution_risk = True  # restart ke baad late/purane reply se mix hone ka khatra
        full = st in (AWAITING, INTERRUPTED, UPLOADING)  # in-progress wait => poora timeout; FAILED => sirf grace
        status = await self._wait_with(rec, sent, self.cfg.response_timeout if full else self.policy.late_reply_grace)
        if self._fatal:
            raise VideoBlocked(name, self._fatal)
        return status == COMPLETED
