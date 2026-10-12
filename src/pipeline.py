"""Ek archive ka poora flow: download -> hash -> extract -> manifest."""
from __future__ import annotations

import asyncio

from .channel_reader import ChannelRef, DownloadError, ZipMessage, download_zip
from .config import Settings
from .manifest import Manifest, archive_key, utc_now
from .zip_processor import (
    ArchiveCorrupted, ArchiveRejected, InsufficientSpace,
    extract_archive, safe_name, sha256_file, unique_dir,
)


def new_record(channel: ChannelRef, z: ZipMessage) -> dict:
    return {
        "archive_id": archive_key(channel.id, z.message_id),
        "archive_filename": z.filename,
        "message_id": z.message_id,
        "channel_id": channel.id,
        "message_date": z.date.isoformat() if z.date else None,
        "archive_size": z.size,
        "archive_sha256": None,
        "archive_path": None,
        "extraction_dir": None,
        "status": "pending",
        "counts": {"images": 0, "videos": 0, "skipped": 0},
        "total_extracted_bytes": 0,
        "files": [],
        "skipped_entries": [],
        "errors": [],
        "started_at": utc_now(),
        "finished_at": None,
    }


async def process_archive(client, channel: ChannelRef, z: ZipMessage,
                          settings: Settings, manifest: Manifest, force: bool = False) -> str:
    if manifest.is_completed(channel.id, z.message_id) and not force:
        print(f"[{z.message_id}] {z.filename}: pehle se complete hai, skip.")
        return "skipped"

    print(f"[{z.message_id}] {z.filename}: shuru...")
    rec = new_record(channel, z)
    rec["status"] = "downloading"
    manifest.upsert(rec)
    manifest.save()
    try:
        path = await download_zip(client, channel, z, settings)
        rec.update(archive_path=manifest.rel(path), archive_size=path.stat().st_size,
                   archive_sha256=await asyncio.to_thread(sha256_file, path), status="extracting")
        manifest.save()

        dest = unique_dir(settings.extracted_dir / f"{abs(channel.id)}_{z.message_id}_{safe_name(path.stem)}")
        rec["extraction_dir"] = manifest.rel(dest)
        res = await asyncio.to_thread(extract_archive, path, dest, settings.limits)
        for f in res.files:
            f["path"] = f"{rec['extraction_dir']}/{f['relative_path']}"
        rec.update(
            files=res.files, skipped_entries=res.skipped, total_extracted_bytes=res.total_bytes,
            counts={"images": sum(f["type"] == "image" for f in res.files),
                    "videos": sum(f["type"] == "video" for f in res.files),
                    "skipped": res.skipped_count},
            status="completed")
        print(f"  Done: {rec['counts']['images']} images, {rec['counts']['videos']} videos.")
    except ArchiveRejected as e:
        rec["status"] = "rejected"; rec["errors"].append(str(e))
        print(f"  REJECTED (safety): {e}")
    except ArchiveCorrupted as e:
        rec["status"] = "corrupted"; rec["errors"].append(str(e))
        print(f"  CORRUPTED: {e}")
    except (DownloadError, InsufficientSpace) as e:
        rec["status"] = "failed"; rec["errors"].append(str(e))
        print(f"  FAILED: {e}")
    except Exception as e:  # noqa: BLE001 - ek archive fail ho to baaki chalte rahein
        rec["status"] = "failed"; rec["errors"].append(f"{type(e).__name__}: {e}")
        print(f"  FAILED: {type(e).__name__}: {e}")
    finally:
        rec["finished_at"] = utc_now()
        manifest.upsert(rec)
        manifest.save()
    return rec["status"]
