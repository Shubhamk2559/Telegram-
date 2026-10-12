"""Settings .env se load hoti hain. Secrets kabhi print nahi hote."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .zip_processor import MB, Limits

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


def _num(name: str, default, cast=int):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError as e:
        raise ConfigError(f"{name} ek number hona chahiye (abhi: {raw!r})") from e


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str = field(repr=False)
    session_name: str
    channel_name: str
    channel_id: int | None
    workspace: Path
    limits: Limits
    # --- Part 2 (DiskWala uploader) ---
    diskwala_bot: str = "DiskWalaFileUploaderBot"
    upload_timeout: int = 180
    upload_poll: float = 3.0
    upload_retries: int = 2
    max_upload_mb: int = 0  # 0 = auto (2000 MB normal, 4000 MB Premium)
    upload_as_document: bool = True
    link_domains: tuple = ()
    upload_delay: float = 3.0
    max_flood_wait: int = 900
    # --- Part 3 (ImgBB + Telegraph) ---
    imgbb_api_key: str = field(default="", repr=False)
    imgbb_retries: int = 3
    imgbb_delay: float = 0.5
    telegraph_token: str = field(default="", repr=False)
    telegraph_title: str = "Gallery"
    telegraph_author: str = ""

    @property
    def downloads_dir(self) -> Path:
        return self.workspace / "downloads"

    @property
    def extracted_dir(self) -> Path:
        return self.workspace / "extracted"

    @property
    def manifests_dir(self) -> Path:
        return self.workspace / "manifests"

    @property
    def sessions_dir(self) -> Path:
        return self.workspace / "sessions"

    @property
    def manifest_path(self) -> Path:
        return self.manifests_dir / "manifest.json"

    @property
    def uploads_path(self) -> Path:
        return self.manifests_dir / "video_uploads.json"

    @property
    def control_path(self) -> Path:
        return self.manifests_dir / "upload_control.txt"

    @property
    def images_path(self) -> Path:
        return self.manifests_dir / "image_manifest.json"

    @property
    def report_path(self) -> Path:
        return self.manifests_dir / "final_report.json"

    @property
    def telegraph_account_path(self) -> Path:
        return self.sessions_dir / "telegraph_account.json"

    @property
    def session_base(self) -> Path:
        return self.sessions_dir / self.session_name


def load_settings(require_api: bool = True) -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    api_id_raw = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()
    if not require_api and not (api_id_raw and api_hash):
        api_id_raw, api_hash = "0", "-"  # offline commands ko API keys nahi chahiye
    if not api_id_raw or not api_hash:
        raise ConfigError(
            "API_ID / API_HASH nahi mile. '.env' file me apni values bharo "
            "(my.telegram.org se milti hain). '.env.example' dekho."
        )
    if not api_id_raw.isdigit():
        raise ConfigError("API_ID sirf number hona chahiye.")

    ws = Path(os.getenv("WORKSPACE_DIR", "workspace").strip() or "workspace")
    if not ws.is_absolute():
        ws = PROJECT_ROOT / ws

    cid_raw = os.getenv("CHANNEL_ID", "").strip()
    try:
        channel_id = int(cid_raw) if cid_raw else None
    except ValueError as e:
        raise ConfigError("CHANNEL_ID number hona chahiye (e.g. -1001234567890).") from e

    limits = Limits(
        max_archive_bytes=_num("MAX_ARCHIVE_MB", 2048) * MB,
        max_extracted_bytes=_num("MAX_EXTRACTED_MB", 4096) * MB,
        max_files=_num("MAX_FILES", 10_000),
        max_ratio=_num("MAX_COMPRESSION_RATIO", 200.0, float),
        min_free_bytes=_num("MIN_FREE_DISK_MB", 500) * MB,
    )
    return Settings(
        api_id=int(api_id_raw),
        api_hash=api_hash,
        session_name=os.getenv("SESSION_NAME", "file_manager").strip() or "file_manager",
        channel_name=os.getenv("CHANNEL_NAME", "File Manager").strip() or "File Manager",
        channel_id=channel_id,
        workspace=ws,
        limits=limits,
        diskwala_bot=(os.getenv("DISKWALA_BOT", "").strip().lstrip("@") or "DiskWalaFileUploaderBot"),
        upload_timeout=_num("UPLOAD_RESPONSE_TIMEOUT", 180),
        upload_poll=_num("UPLOAD_POLL_SECONDS", 3.0, float),
        upload_retries=_num("UPLOAD_RETRIES", 2),
        max_upload_mb=_num("MAX_UPLOAD_MB", 0),
        upload_as_document=os.getenv("UPLOAD_AS", "document").strip().lower() != "video",
        link_domains=tuple(d.strip().lower() for d in os.getenv("DISKWALA_LINK_DOMAINS", "").split(",") if d.strip()),
        upload_delay=_num("UPLOAD_DELAY_SECONDS", 3.0, float),
        max_flood_wait=_num("MAX_FLOOD_WAIT_SECONDS", 900),
        imgbb_api_key=os.getenv("IMGBB_API_KEY", "").strip(),
        imgbb_retries=_num("IMGBB_RETRIES", 3),
        imgbb_delay=_num("IMGBB_DELAY_SECONDS", 0.5, float),
        telegraph_token=os.getenv("TELEGRAPH_ACCESS_TOKEN", "").strip(),
        telegraph_title=os.getenv("TELEGRAPH_TITLE", "").strip() or "Gallery",
        telegraph_author=os.getenv("TELEGRAPH_AUTHOR", "").strip(),
    )
