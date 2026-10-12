"""workspace/manifests/image_manifest.json - har image ka ImgBB + Telegraph status.

Har badlav turant atomic save hota hai, taaki restart par kaam resume ho.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .manifest import atomic_write_json, utc_now

SCHEMA_VERSION = 1

# imgbb.status
PENDING = "pending"
UPLOADED = "uploaded"
FAILED = "failed"

# telegraph.status
T_VERIFIED = "embedded_verified"      # stored + page HTML me img tag + image URL image serve karta hai
T_UNVERIFIED = "embedded_unverified"  # article me hai, par poori verification confirm nahi hui
T_FAILED = "failed"
T_SKIPPED = "skipped"

HTML_ORIGIN_NOTE = (
    "ImgBB API v1 HTML embed code return nahi karta; html_embed.constructed API ke URL fields "
    "se banaya gaya hai."
)


class ImageStoreError(Exception):
    pass


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _blank(img: dict) -> dict:
    return {
        "file_id": img["file_id"],
        "filename": img["filename"],
        "relative_path": img.get("relative_path"),
        "path": img["path"],
        "size": img["size"],
        "archive_id": img.get("archive_id"),
        "archive_filename": img.get("archive_filename"),
        "order": img["order"],
        "imgbb": {
            "status": PENDING, "error": None, "id": None, "url": None, "display_url": None,
            "viewer_url": None, "thumb_url": None, "medium_url": None, "delete_url": None,
            "uploaded_at": None, "attempts": 0,
        },
        "html_embed": {
            "original_from_imgbb": None,  # API deta hi nahi, isliye hamesha None
            "constructed": None,
            "note": HTML_ORIGIN_NOTE,
        },
        "telegraph": {
            "method": None, "status": None, "error": None, "page_url": None,
            "dropped_from_embed": [], "checked_at": None,
        },
    }


def collect_images(manifest_data: dict) -> list[dict]:
    """Part 1 manifest se images: completed archives (message_id order), natural file order, file_id dedupe."""
    seen: set[str] = set()
    out: list[dict] = []
    archives = [a for a in manifest_data.get("archives", []) if a.get("status") == "completed"]
    archives.sort(key=lambda a: (a.get("message_id") or 0, a.get("archive_id") or ""))
    for arch in archives:
        files = [f for f in arch.get("files", []) if f.get("type") == "image"]
        files.sort(key=lambda f: natural_key(f.get("relative_path") or f.get("filename") or ""))
        for f in files:
            if f["file_id"] in seen:
                continue
            seen.add(f["file_id"])
            path = f.get("path") or f"{arch.get('extraction_dir')}/{f['relative_path']}"
            out.append({
                "file_id": f["file_id"], "filename": f["filename"], "size": f["size"], "path": path,
                "relative_path": f.get("relative_path"), "archive_id": arch.get("archive_id"),
                "archive_filename": arch.get("archive_filename"), "order": len(out),
            })
    return out


class ImageStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "files": [], "telegraph": None}
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
            raise ImageStoreError(f"image_manifest.json kharab thi, backup: {backup.name}. Dobara chalao.") from e
        self.data = loaded
        self.data.setdefault("telegraph", None)

    @property
    def files(self) -> list[dict]:
        return self.data["files"]

    def get(self, file_id: str) -> dict | None:
        return next((r for r in self.files if r["file_id"] == file_id), None)

    def sync(self, images: list[dict]) -> int:
        """Naye images add karo; purane records (aur unke URLs) ko chhedo mat. Sirf order refresh hota hai."""
        added = 0
        changed = False
        for img in images:
            rec = self.get(img["file_id"])
            if rec is None:
                self.files.append(_blank(img))
                added += 1
            elif rec.get("order") != img["order"]:
                rec["order"] = img["order"]
                changed = True
        if added or changed:
            self.save()
        return added

    def ordered(self) -> list[dict]:
        return sorted(self.files, key=lambda r: r.get("order", 0))

    def update(self, rec: dict, section: str, **fields) -> None:
        rec[section].update(fields)
        self.save()

    def save(self) -> None:
        self.data["updated_at"] = utc_now()
        atomic_write_json(self.path, self.data)

    def select_for_upload(self, retry_failed: bool = False, limit: int | None = None) -> list[dict]:
        wanted = {PENDING} | ({FAILED} if retry_failed else set())
        work = [r for r in self.ordered() if r["imgbb"]["status"] in wanted]
        return work[:limit] if limit else work

    def uploaded(self) -> list[dict]:
        return [r for r in self.ordered() if r["imgbb"]["status"] == UPLOADED]

    def counts(self) -> dict:
        c: dict[str, int] = {}
        for r in self.files:
            c[r["imgbb"]["status"]] = c.get(r["imgbb"]["status"], 0) + 1
        return c
