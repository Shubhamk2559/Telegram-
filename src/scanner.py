"""File Manager channel ko scan karke naye ZIPs aur seedhe bheje image/video batches queue me daalna.

- Checkpoint (last_scanned message id) DB me; offline rehne par jo ZIP aaye wo agle scan me mil jaate hain.
- Non-ZIP aur non-media messages ignore, par checkpoint unke upar bhi aage badhta hai.
- PEHLI baar (checkpoint nahi) default me purani history backfill NAHI hoti (warna poori channel history
  dobara process/publish ho jaati). Backfill ke liye SCAN_BACKFILL_FROM_MESSAGE_ID ya `enqueue --from-message-id`.

Seedhe bheje image/video (ZIP ke bina) - BATCH rule:
  image(s) phir video(s) = ek batch (ek "folder"). Video ke baad jaise hi nayi image aaye, naya batch shuru.
  Batch tab "band" maana jata hai jab (a) uske baad nayi image aaye, ya (b) ZIP aaye, ya (c) aakhri message ko
  `quiet_seconds` beet jayein. Tab tak batch queue me nahi jaata aur checkpoint uske pehle message par ruka rehta hai
  (taaki user ke aur videos bhejte rehne par woh isi batch me aayein).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .channel_reader import ZIP_MIMES
from .jobs import JobRepo
from .zip_processor import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS


def zip_info(msg) -> tuple[str, int] | None:
    f = getattr(msg, "file", None)
    if not f:
        return None
    name = f.name or ""
    if not (name.lower().endswith(".zip") or (f.mime_type or "") in ZIP_MIMES):
        return None
    return name or f"archive_{msg.id}.zip", int(f.size or 0)


def media_info(msg) -> dict | None:
    """Image ya video message ho to {"id","kind","name","size"}, warna None. Sticker skip.
    Photo (Telegram ne compress kar di) bhi image hai: behtar hai ki user images ko File/document ke roop me bheje."""
    f = getattr(msg, "file", None)
    if not f or getattr(msg, "sticker", None):
        return None
    name = f.name or ""
    ext = (getattr(f, "ext", None) or Path(name).suffix or "").lower()
    mime = (f.mime_type or "").lower()
    if ext in IMAGE_EXTENSIONS or mime.startswith("image/"):
        kind, default_ext = "image", ".jpg"
    elif ext in VIDEO_EXTENSIONS or mime.startswith("video/"):
        kind, default_ext = "video", ".mp4"
    else:
        return None
    if not name:
        name = f"{kind}_{msg.id}{ext or default_ext}"
    return {"id": int(msg.id), "kind": kind, "name": name, "size": int(f.size or 0)}


class _Batch:
    def __init__(self):
        self.items: list[dict] = []
        self.first_date = None
        self.last_date = None

    @property
    def first_id(self) -> int:
        return self.items[0]["id"]

    @property
    def has_video(self) -> bool:
        return any(i["kind"] == "video" for i in self.items)

    def add(self, item: dict, date) -> None:
        if date is not None and date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        self.items.append(item)
        self.first_date = self.first_date or date
        self.last_date = date or self.last_date


async def scan_channel(client, entity, chat_id: int, repo: JobRepo, *, backfill_from: int | None = None,
                       media_batches: bool = True, quiet_seconds: float = 180.0, now=None) -> dict:
    """Returns {"new": n, "last": checkpoint, "first_run": bool, "open_batch": bool}."""
    now = now or (lambda: datetime.now(timezone.utc))
    last = repo.last_scanned(chat_id)
    first_run = repo.get_kv(f"last_scanned:{chat_id}") is None
    if first_run and backfill_from is None:
        latest = await client.get_messages(entity, limit=1)
        newest = latest[0].id if latest else 0
        repo.set_kv(f"last_scanned:{chat_id}", str(newest))  # abhi se shuru; history skip
        return {"new": 0, "last": newest, "first_run": True, "open_batch": False}
    min_id = max(last, (backfill_from - 1) if (first_run and backfill_from) else 0)
    new, top = 0, max(last, min_id)
    batch: _Batch | None = None

    def flush() -> None:
        nonlocal batch, new
        if batch is not None:
            d = batch.first_date
            new += repo.discover_batch(chat_id, batch.items, d.isoformat() if d else None)
            batch = None

    async for msg in client.iter_messages(entity, min_id=min_id, reverse=True):  # oldest -> newest
        top = max(top, msg.id)
        info = zip_info(msg)
        if info:
            flush()  # ZIP aane se pehle ka batch band
            new += repo.discover(chat_id, msg.id, info[0], info[1], msg.date.isoformat() if msg.date else None)
            continue
        if not media_batches:
            continue
        m = media_info(msg)
        if not m:
            continue
        if batch is not None and m["kind"] == "image" and batch.has_video:
            flush()  # video ke baad nayi image = naya folder
        if batch is None:
            batch = _Batch()
        batch.add(m, msg.date)

    checkpoint, open_batch = top, False
    if batch is not None:
        age = (now() - batch.last_date).total_seconds() if batch.last_date else quiet_seconds
        if age >= quiet_seconds:
            flush()
        else:  # user shayad abhi aur videos bhej raha hai: batch abhi nahi, checkpoint iske pehle message ke pehle
            checkpoint, open_batch = batch.first_id - 1, True
    if first_run:
        repo.set_kv(f"last_scanned:{chat_id}", str(checkpoint))
    else:
        repo.set_last_scanned(chat_id, checkpoint)
    repo.queue_discovered()
    return {"new": new, "last": checkpoint, "first_run": first_run, "open_batch": open_batch}
