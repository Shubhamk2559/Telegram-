"""JSON manifest: kaunsa archive process hua, kya mila, kya error aaya."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1


class ManifestError(Exception):
    pass


def atomic_write_json(path: Path, data) -> None:
    """Temp file me likho -> fsync -> rename. Beech me band hone par file kharab nahi hoti."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def archive_key(channel_id: int, message_id: int) -> str:
    return f"{channel_id}:{message_id}"


class Manifest:
    def __init__(self, path: Path, workspace: Path):
        self.path = Path(path)
        self.workspace = Path(workspace).resolve()
        self.data = {
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "channel": None,
            "archives": [],
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded.get("archives"), list):
                raise ValueError("archives missing")
        except (ValueError, OSError, AttributeError) as e:
            backup = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            self.path.replace(backup)
            raise ManifestError(
                f"Manifest kharab tha, backup bana diya: {backup.name}. Dobara run karo."
            ) from e
        self.data = loaded

    def rel(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.workspace).as_posix()

    def set_channel(self, channel_id: int, title: str) -> None:
        self.data["channel"] = {"id": channel_id, "title": title}

    def get(self, channel_id: int, message_id: int) -> dict | None:
        key = archive_key(channel_id, message_id)
        return next((a for a in self.data["archives"] if a.get("archive_id") == key), None)

    def is_completed(self, channel_id: int, message_id: int) -> bool:
        rec = self.get(channel_id, message_id)
        return bool(rec and rec.get("status") == "completed")

    def upsert(self, record: dict) -> None:
        archives = self.data["archives"]
        for i, a in enumerate(archives):
            if a.get("archive_id") == record["archive_id"]:
                archives[i] = record
                return
        archives.append(record)

    def save(self) -> None:
        self.data["updated_at"] = utc_now()
        atomic_write_json(self.path, self.data)
