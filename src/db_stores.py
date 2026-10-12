"""Per-ZIP stores jo JSON file ki jagah database me likhte hain.

ImageStore / UploadStore ki saari logic (sync, select, counts...) reuse hoti hai; sirf load/save badla hai.
Har update sirf ek row likhta hai (poori list nahi), aur turant commit hota hai.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .db import Database, now_iso
from .image_store import ImageStore
from .upload_store import UploadStore


class DbImageStore(ImageStore):
    def __init__(self, db: Database, archive_id: str):  # super().__init__ jaanbujhkar nahi (file load nahi chahiye)
        self.db, self.archive_id = db, archive_id
        self.path = Path(f"db://{archive_id}")
        rows = db.query("SELECT data FROM images WHERE archive_id = ? ORDER BY ord, file_id", (archive_id,))
        self.data = {"schema_version": 1, "updated_at": now_iso(), "telegraph": None,
                     "files": [json.loads(r["data"]) for r in rows]}

    def _persist(self, rec: dict) -> None:
        self.db.execute(
            "INSERT INTO images (archive_id, file_id, ord, data, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (archive_id, file_id) DO UPDATE SET ord = EXCLUDED.ord, data = EXCLUDED.data, "
            "updated_at = EXCLUDED.updated_at",
            (self.archive_id, rec["file_id"], int(rec.get("order", 0)), json.dumps(rec), now_iso()))

    def update(self, rec: dict, section: str, **fields) -> None:
        rec[section].update(fields)
        self._persist(rec)

    def save(self) -> None:
        with self.db.transaction():
            for rec in self.files:
                self._persist(rec)


class DbUploadStore(UploadStore):
    """Videos. Disk paths DB me workspace-relative rehte hain, memory me absolute (uploader ko wahi chahiye)."""

    def __init__(self, db: Database, archive_id: str, bot: str, workspace: Path):
        self.db, self.archive_id = db, archive_id
        self.workspace = Path(workspace).resolve()
        self.path = Path(f"db://{archive_id}")
        self.recovered: list[str] = []  # in-flight records ko hum yahan INTERRUPTED nahi banate: StrictVideoUploader recover karta hai
        rows = db.query("SELECT data FROM videos WHERE archive_id = ? ORDER BY ord, file_id", (archive_id,))
        files = [json.loads(r["data"]) for r in rows]
        for rec in files:
            p = Path(rec["path"])
            rec["path"] = str(p if p.is_absolute() else self.workspace / p)
        self.data = {"schema_version": 1, "bot": bot, "updated_at": now_iso(), "files": files}

    def _rel(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.workspace) if Path(path).resolve().is_relative_to(self.workspace) else path
        except (OSError, ValueError):
            return path

    def _persist(self, rec: dict) -> None:
        row = {**rec, "path": self._rel(rec["path"])}
        self.db.execute(
            "INSERT INTO videos (archive_id, file_id, ord, data, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (archive_id, file_id) DO UPDATE SET ord = EXCLUDED.ord, data = EXCLUDED.data, "
            "updated_at = EXCLUDED.updated_at",
            (self.archive_id, rec["file_id"], self.files.index(rec), json.dumps(row), now_iso()))

    def sync(self, videos: list[dict]) -> int:
        """Naye videos ke paths turant absolute (Part 2 ka relative-path bug yahin band hota hai)."""
        fixed = []
        for v in videos:
            p = Path(v["path"])
            fixed.append({**v, "path": str(p if p.is_absolute() else self.workspace / p)})
        return super().sync(fixed)

    def update(self, rec: dict, **fields) -> None:
        rec.update(fields)
        self._persist(rec)

    def save(self) -> None:
        with self.db.transaction():
            for rec in self.files:
                self._persist(rec)


class ArticleStore:
    """ZIP ka Telegraph article (pages, fingerprint, status) - ek row per archive."""

    def __init__(self, db: Database):
        self.db = db

    def get(self, archive_id: str) -> dict | None:
        row = self.db.query_one("SELECT data FROM articles WHERE archive_id = ?", (archive_id,))
        return json.loads(row["data"]) if row else None

    def put(self, archive_id: str, data: dict) -> None:
        self.db.execute(
            "INSERT INTO articles (archive_id, data, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (archive_id) DO UPDATE SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at",
            (archive_id, json.dumps(data), now_iso()))


class PostStore:
    """Final Telegram posts. Content publish se PEHLE save hota hai aur kabhi overwrite nahi hota."""

    def __init__(self, db: Database):
        self.db = db

    def save_plan(self, archive_id: str, plans) -> int:
        n = 0
        with self.db.transaction():
            for p in plans:
                n += self.db.execute(
                    "INSERT INTO posts (archive_id, seq, content, link_start, link_count, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?) ON CONFLICT (archive_id, seq) DO NOTHING",
                    (archive_id, p.seq, p.text, p.link_start, p.link_count, now_iso()))
        return n

    def list(self, archive_id: str) -> list[dict]:
        return self.db.query("SELECT * FROM posts WHERE archive_id = ? ORDER BY seq", (archive_id,))

    def mark_sending(self, archive_id: str, seq: int) -> None:
        self.db.execute("UPDATE posts SET status = 'sending', attempts = attempts + 1, sending_started_at = ?, "
                        "error = NULL WHERE archive_id = ? AND seq = ?", (now_iso(), archive_id, seq))

    def mark_published(self, archive_id: str, seq: int, message_id: int) -> None:
        self.db.execute("UPDATE posts SET status = 'published', message_id = ?, published_at = ?, error = NULL "
                        "WHERE archive_id = ? AND seq = ?", (message_id, now_iso(), archive_id, seq))

    def mark_failed(self, archive_id: str, seq: int, error: str) -> None:
        self.db.execute("UPDATE posts SET status = 'failed', error = ? WHERE archive_id = ? AND seq = ?",
                        (error[:500], archive_id, seq))
