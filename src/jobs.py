"""Persistent ZIP job queue (archives table). Ek ZIP = ek independent job, archive_id = chat_id:message_id."""
from __future__ import annotations

import json
from dataclasses import dataclass

from .db import Database, now_iso

DISCOVERED, QUEUED = "DISCOVERED", "QUEUED"
DOWNLOADING, EXTRACTING = "DOWNLOADING", "EXTRACTING"
UPLOADING_IMAGES, CREATING_TELEGRAPH = "UPLOADING_IMAGES", "CREATING_TELEGRAPH"
UPLOADING_VIDEOS, GENERATING_POSTS, PUBLISHING_POSTS = "UPLOADING_VIDEOS", "GENERATING_POSTS", "PUBLISHING_POSTS"
COMPLETED, PAUSED, FAILED = "COMPLETED", "PAUSED", "FAILED"

PIPELINE = (DOWNLOADING, EXTRACTING, UPLOADING_IMAGES, CREATING_TELEGRAPH,
            UPLOADING_VIDEOS, GENERATING_POSTS, PUBLISHING_POSTS)
STATES = (DISCOVERED, QUEUED) + PIPELINE + (COMPLETED, PAUSED, FAILED)
TERMINAL = (COMPLETED, FAILED)
CANCELLED_STAGE = "CANCELLED"  # FAILED + yeh stage = user ne cancel kiya (requeue se wapas aa sakta hai)


class JobError(Exception):
    pass


def make_archive_id(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


@dataclass
class Selection:
    job: dict | None = None
    blocked_by: dict | None = None  # PAUSED job jo queue roke hue hai


def _decode(row: dict | None) -> dict | None:
    if row is None:
        return None
    for k in ("progress", "extraction", "media"):
        if row.get(k):
            row[k] = json.loads(row[k])
    return row


class JobRepo:
    def __init__(self, db: Database):
        self.db = db

    # ---- discovery ----------------------------------------------------------
    def discover(self, chat_id: int, message_id: int, filename: str, size: int, msg_date: str | None = None) -> bool:
        """Naya ZIP record. Pehle se ho to kuch nahi badalta. True = naya tha."""
        now = now_iso()
        n = self.db.execute(
            "INSERT INTO archives (archive_id, chat_id, message_id, filename, size, msg_date, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (archive_id) DO NOTHING",
            (make_archive_id(chat_id, message_id), chat_id, message_id, filename, size, msg_date, DISCOVERED, now, now))
        return n > 0

    def discover_batch(self, chat_id: int, items: list[dict], msg_date: str | None = None) -> bool:
        """File Manager me seedhe bheje image(s)+video(s) ka batch (ZIP nahi). archive_id = chat:pehla_message_id.
        items = [{"id","kind":"image"|"video","name","size"}, ...] message order me. Pehle se ho to kuch nahi badalta."""
        if not items:
            return False
        first = int(items[0]["id"])
        ni, nv = sum(i["kind"] == "image" for i in items), sum(i["kind"] == "video" for i in items)
        now = now_iso()
        n = self.db.execute(
            "INSERT INTO archives (archive_id, chat_id, message_id, filename, size, msg_date, status, kind, media, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (archive_id) DO NOTHING",
            (make_archive_id(chat_id, first), chat_id, first, f"Media batch #{first} ({ni} img, {nv} vid)",
             sum(int(i.get("size") or 0) for i in items), msg_date, DISCOVERED, "media", json.dumps(items), now, now))
        return n > 0

    def queue_discovered(self) -> int:
        return self.db.execute("UPDATE archives SET status = ?, updated_at = ? WHERE status = ?",
                               (QUEUED, now_iso(), DISCOVERED))

    # ---- reads --------------------------------------------------------------
    def get(self, archive_id: str) -> dict | None:
        return _decode(self.db.query_one("SELECT * FROM archives WHERE archive_id = ?", (archive_id,)))

    def list(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.db.query("SELECT * FROM archives WHERE status = ? ORDER BY message_id, chat_id", (status,))
        else:
            rows = self.db.query("SELECT * FROM archives ORDER BY message_id, chat_id")
        return [_decode(r) for r in rows]

    def next_job(self) -> Selection:
        """Sabse purana UNFINISHED ZIP (Telegram message order). Wo PAUSED ho to queue ruki rehti hai (skip nahi).

        Restart par yahi function crashed job ko wapas deta hai (DOWNLOADING.. PUBLISHING_POSTS state me atka hua),
        isliye recovery ke liye alag code nahi chahiye - pipeline DB se dekh ke completed stages skip karti hai.
        FAILED (permanent, jaise corrupt ZIP) queue nahi rokta, par report me dikhta hai; `requeue` se wapas aata hai.
        """
        row = _decode(self.db.query_one(
            "SELECT * FROM archives WHERE status NOT IN (?, ?) ORDER BY message_id, chat_id LIMIT 1", TERMINAL))
        if row is None:
            return Selection()
        if row["status"] == PAUSED:
            return Selection(blocked_by=row)
        return Selection(job=row)

    def peek_after(self, archive_id: str) -> dict | None:
        """Sirf padhna (claim/badlav nahi): archive_id ke baad queue me kaun chalega. Wo QUEUED na ho (PAUSED => queue wahan
        ruki rahegi, ya pipeline me atka) to None. Prefetch isse decide karta hai."""
        row = _decode(self.db.query_one(
            "SELECT * FROM archives WHERE status NOT IN (?, ?) AND archive_id != ? ORDER BY message_id, chat_id LIMIT 1",
            (*TERMINAL, archive_id)))
        return row if row is not None and row["status"] == QUEUED else None

    # ---- transitions --------------------------------------------------------
    def transition(self, archive_id: str, status: str, *, stage: str | None = None,
                   progress: dict | None = None, error: str | None = None) -> None:
        if status not in STATES:
            raise JobError(f"Unknown state: {status}")
        cur = self.get(archive_id)
        if cur is None:
            raise JobError(f"Unknown archive: {archive_id}")
        if cur["status"] == COMPLETED:
            raise JobError(f"{archive_id} pehle se COMPLETED hai; badla nahi ja sakta.")
        if cur["status"] == FAILED and status not in (FAILED, QUEUED):
            raise JobError(f"{archive_id} FAILED hai; pehle requeue karo.")
        now = now_iso()
        started = cur["started_at"] or (now if status in PIPELINE else None)
        finished = now if status in TERMINAL else None
        self.db.execute(
            "UPDATE archives SET status = ?, stage = ?, progress = ?, error = ?, paused_reason = ?, "
            "started_at = ?, finished_at = ?, updated_at = ? WHERE archive_id = ?",
            (status, stage or (status if status in PIPELINE else cur["stage"]),
             json.dumps(progress) if progress is not None else None, error,
             error if status == PAUSED else None, started, finished, now, archive_id))

    def set_progress(self, archive_id: str, progress: dict) -> None:
        self.db.execute("UPDATE archives SET progress = ?, updated_at = ? WHERE archive_id = ?",
                        (json.dumps(progress), now_iso(), archive_id))

    def pause(self, archive_id: str, reason: str) -> None:
        self.transition(archive_id, PAUSED, error=reason)

    def resume(self, archive_id: str) -> None:
        cur = self.get(archive_id)
        if not cur or cur["status"] != PAUSED:
            raise JobError(f"{archive_id} PAUSED nahi hai.")
        self.transition(archive_id, QUEUED)

    def requeue(self, archive_id: str) -> None:
        cur = self.get(archive_id)
        if not cur or cur["status"] != FAILED:
            raise JobError(f"{archive_id} FAILED nahi hai.")
        self.db.execute("UPDATE archives SET retry_count = retry_count + 1 WHERE archive_id = ?", (archive_id,))
        self.transition(archive_id, QUEUED)

    def cancel(self, archive_id: str) -> str:
        """ZIP ko cancel karo (kisi bhi unfinished state se). FAILED ban jaata hai => queue aage badhti hai.
        Kya NAHI hota: publish ho chuke Telegram posts delete nahi hote, ImgBB/Telegraph par bani cheezein rehti hain.
        Chal rahi job ko worker ~1 heartbeat (lease_ttl/3, default 40s) me rok deta hai. `requeue` se wapas la sakte ho."""
        cur = self.get(archive_id)
        if cur is None:
            raise JobError(f"Unknown archive: {archive_id}")
        if cur["status"] == COMPLETED:
            raise JobError(f"{archive_id} pehle se COMPLETED hai; cancel nahi hoga.")
        if cur["status"] == FAILED:
            raise JobError(f"{archive_id} pehle se FAILED/cancelled hai.")
        self.transition(archive_id, FAILED, stage=CANCELLED_STAGE, error="Cancelled by user")
        return cur["status"]

    def fail(self, archive_id: str, error: str) -> None:
        self.transition(archive_id, FAILED, error=error)

    def complete(self, archive_id: str) -> None:
        self.transition(archive_id, COMPLETED)

    def bump_retry(self, archive_id: str) -> int:
        self.db.execute("UPDATE archives SET retry_count = retry_count + 1, updated_at = ? WHERE archive_id = ?",
                        (now_iso(), archive_id))
        return self.get(archive_id)["retry_count"]

    def save_extraction(self, archive_id: str, info: dict) -> None:
        self.db.execute("UPDATE archives SET extraction = ?, updated_at = ? WHERE archive_id = ?",
                        (json.dumps(info), now_iso(), archive_id))

    # ---- scan checkpoint ----------------------------------------------------
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.db.query_one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
                        (key, value, now_iso()))

    def last_scanned(self, chat_id: int) -> int:
        return int(self.get_kv(f"last_scanned:{chat_id}", "0"))

    def set_last_scanned(self, chat_id: int, message_id: int) -> None:
        if message_id > self.last_scanned(chat_id):  # peeche kabhi nahi jaata
            self.set_kv(f"last_scanned:{chat_id}", str(message_id))

    # ---- overall counts -----------------------------------------------------
    def overall(self) -> dict:
        c = {r["status"]: r["n"] for r in self.db.query("SELECT status, COUNT(*) AS n FROM archives GROUP BY status")}
        total = sum(c.values())
        completed, failed, paused = c.get(COMPLETED, 0), c.get(FAILED, 0), c.get(PAUSED, 0)
        processing = sum(c.get(s, 0) for s in PIPELINE)
        return {"total": total, "completed": completed, "processing": processing, "paused": paused,
                "failed": failed, "pending": total - completed - processing - paused - failed}
