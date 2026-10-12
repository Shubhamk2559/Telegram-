"""workspace/manifests/video_uploads.json - har video ka upload status + bot ka link.

Har badlav turant disk par save hota hai (atomic), taaki restart par kaam wahin se resume ho.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .manifest import atomic_write_json, utc_now

SCHEMA_VERSION = 1

PENDING = "pending"
UPLOADING = "uploading"
AWAITING = "awaiting_response"
COMPLETED = "completed"
FAILED = "failed"
INTERRUPTED = "interrupted"

IN_FLIGHT = (UPLOADING, AWAITING)


class UploadStoreError(Exception):
    pass


def _blank(video: dict) -> dict:
    return {
        "file_id": video["file_id"],
        "filename": video["filename"],
        "size": video["size"],
        "path": video["path"],
        "archive_id": video.get("archive_id"),
        "status": PENDING,
        "url": None,
        "urls": [],
        "error": None,
        "attempts": 0,
        "sent_message_id": None,
        "reply_message_id": None,
        "match": None,
        "needs_review": False,
        "bot_reply_excerpt": None,
        "started_at": None,
        "finished_at": None,
    }


def collect_videos(manifest_data: dict) -> list[dict]:
    """Part 1 manifest se videos: sirf completed archives, sirf type=video, file_id se dedupe."""
    seen: set[str] = set()
    out: list[dict] = []
    for arch in manifest_data.get("archives", []):
        if arch.get("status") != "completed":
            continue
        for f in arch.get("files", []):
            if f.get("type") != "video" or f["file_id"] in seen:
                continue
            seen.add(f["file_id"])
            path = f.get("path") or f"{arch.get('extraction_dir')}/{f['relative_path']}"
            out.append({
                "file_id": f["file_id"], "filename": f["filename"], "size": f["size"],
                "path": path, "archive_id": arch.get("archive_id"),
            })
    return out


class UploadStore:
    def __init__(self, path: Path, bot: str):
        self.path = Path(path)
        self.data = {"schema_version": SCHEMA_VERSION, "bot": bot, "updated_at": utc_now(), "files": []}
        self.recovered: list[str] = []  # pichli baar beech me ruki files ke naam
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded.get("files"), list):
                raise ValueError("files missing")
        except (ValueError, OSError, AttributeError) as e:
            backup = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            self.path.replace(backup)
            raise UploadStoreError(f"video_uploads.json kharab thi, backup: {backup.name}. Dobara chalao.") from e
        self.data = loaded
        for rec in self.files:
            if rec["status"] in IN_FLIGHT:
                # Bot ko file mili ya nahi, pata nahi => blind retry nahi (duplicate upload ka risk)
                rec["status"] = INTERRUPTED
                rec["error"] = ("Pichli baar beech me ruk gaya. Ho sakta hai bot ko file mil gayi ho - "
                                "Telegram chat check karo, phir --retry-interrupted se dobara bhejo.")
                self.recovered.append(rec["filename"])
        if self.recovered:
            self.save()

    @property
    def files(self) -> list[dict]:
        return self.data["files"]

    def get(self, file_id: str) -> dict | None:
        return next((r for r in self.files if r["file_id"] == file_id), None)

    def sync(self, videos: list[dict]) -> int:
        """Naye videos pending ke roop me add karo; purane records ko chhedo mat."""
        added = 0
        for v in videos:
            if self.get(v["file_id"]) is None:
                self.files.append(_blank(v))
                added += 1
        if added:
            self.save()
        return added

    def update(self, rec: dict, **fields) -> None:
        rec.update(fields)
        self.save()

    def save(self) -> None:
        self.data["updated_at"] = utc_now()
        atomic_write_json(self.path, self.data)

    def select_work(self, retry_failed: bool = False, retry_interrupted: bool = False,
                    limit: int | None = None) -> list[dict]:
        wanted = {PENDING}
        if retry_failed:
            wanted.add(FAILED)
        if retry_interrupted:
            wanted.add(INTERRUPTED)
        work = [r for r in self.files if r["status"] in wanted]
        return work[:limit] if limit else work

    def counts(self) -> dict:
        c: dict[str, int] = {}
        for r in self.files:
            c[r["status"]] = c.get(r["status"], 0) + 1
        return c
