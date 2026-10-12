"""File Manager channel ko scan karke naye ZIPs queue me daalna.

- Checkpoint (last_scanned message id) DB me; offline rehne par jo ZIP aaye wo agle scan me mil jaate hain.
- Non-ZIP messages ignore, par checkpoint unke upar bhi aage badhta hai.
- PEHLI baar (checkpoint nahi) default me purani history backfill NAHI hoti (warna poori channel history
  dobara process/publish ho jaati). Backfill ke liye SCAN_BACKFILL_FROM_MESSAGE_ID ya `enqueue --from-message-id`.
"""
from __future__ import annotations

from .channel_reader import ZIP_MIMES
from .jobs import JobRepo


def zip_info(msg) -> tuple[str, int] | None:
    f = getattr(msg, "file", None)
    if not f:
        return None
    name = f.name or ""
    if not (name.lower().endswith(".zip") or (f.mime_type or "") in ZIP_MIMES):
        return None
    return name or f"archive_{msg.id}.zip", int(f.size or 0)


async def scan_channel(client, entity, chat_id: int, repo: JobRepo, *, backfill_from: int | None = None) -> dict:
    """Returns {"new": n, "last": id, "first_run": bool}."""
    last = repo.last_scanned(chat_id)
    first_run = repo.get_kv(f"last_scanned:{chat_id}") is None
    if first_run and backfill_from is None:
        latest = await client.get_messages(entity, limit=1)
        newest = latest[0].id if latest else 0
        repo.set_kv(f"last_scanned:{chat_id}", str(newest))  # abhi se shuru; history skip
        return {"new": 0, "last": newest, "first_run": True}
    min_id = max(last, (backfill_from - 1) if (first_run and backfill_from) else 0)
    new, top = 0, max(last, min_id)
    async for msg in client.iter_messages(entity, min_id=min_id, reverse=True):  # oldest -> newest
        top = max(top, msg.id)
        info = zip_info(msg)
        if info:
            new += repo.discover(chat_id, msg.id, info[0], info[1], msg.date.isoformat() if msg.date else None)
    if first_run:
        repo.set_kv(f"last_scanned:{chat_id}", str(top))
    else:
        repo.set_last_scanned(chat_id, top)
    repo.queue_discovered()
    return {"new": new, "last": top, "first_run": first_run}
