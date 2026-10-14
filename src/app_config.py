"""Production config. Existing env names (API_ID, CHANNEL_ID, DISKWALA_BOT...) hi chalte rahenge; spec ke naye
naam (TELEGRAM_API_ID, FILE_MANAGER_CHANNEL_ID...) unke aliases hain. Dono set hon to PURANA naam jeetta hai."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError, Settings, load_settings
from .diskwala_uploader import UploaderConfig
from .posts import (
    DEFAULT_IMAGES_ONLY_TEMPLATE, DEFAULT_TEMPLATE, DEFAULT_VIDEOS_ONLY_TEMPLATE, VARS_FULL, VARS_IMAGES_ONLY,
    VARS_VIDEOS_ONLY, TemplateError, unescape_template, validate_template,
)
from .strict_videos import VideoPolicy
from .tg_publisher import PublishPolicy
from .worker import WorkerConfig
from .zip_processor import MB

ALIASES = {  # existing name <- spec name
    "API_ID": "TELEGRAM_API_ID",
    "API_HASH": "TELEGRAM_API_HASH",
    "CHANNEL_ID": "FILE_MANAGER_CHANNEL_ID",
    "DISKWALA_BOT": "DISKWALA_BOT_USERNAME",
    "UPLOAD_RESPONSE_TIMEOUT": "DISKWALA_RESPONSE_TIMEOUT_SECONDS",
    "UPLOAD_RETRIES": "DISKWALA_MAX_RETRIES",
    "IMGBB_DELAY_SECONDS": "IMGBB_UPLOAD_DELAY_SECONDS",
    "IMGBB_RETRIES": "IMGBB_MAX_RETRIES",
    "TELEGRAPH_TITLE": "TELEGRAPH_ARTICLE_TITLE",
    "SESSION_STRING": "TELEGRAM_SESSION_STRING",
}
WORKER_DEFAULTS = {"UPLOAD_RESPONSE_TIMEOUT": "1800", "IMGBB_DELAY_SECONDS": "5", "IMGBB_RETRIES": "5"}
# Telegraph title ka koi built-in naam nahi: TELEGRAPH_ARTICLE_TITLE (ya TELEGRAPH_TITLE) env me apna rakho.
# Env khaali ho to "Gallery" use hota hai (Telegraph ko title chahiye hi).


def apply_env_aliases(env=os.environ, defaults: dict | None = None) -> None:
    for old, new in ALIASES.items():
        if not (env.get(old) or "").strip() and (env.get(new) or "").strip():
            env[old] = env[new].strip()
    for k, v in (defaults or {}).items():
        if not (env.get(k) or "").strip():
            env[k] = v


def _num(env, name, default, cast=int):
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError as e:
        raise ConfigError(f"{name} number hona chahiye, mila: {raw!r}") from e


def _bool(env, name, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


@dataclass
class AppConfig:
    settings: Settings
    worker: WorkerConfig
    video_policy: VideoPolicy
    uploader: UploaderConfig
    publish_policy: PublishPolicy
    database_url: str
    session_string: str
    dest_id: int | None
    backfill_from: int | None
    link_preview: bool
    health_port: int
    health_enabled: bool
    log_level: str

    def __repr__(self) -> str:  # secrets (api_hash, session, DB password, keys) kabhi repr/log me na jayein
        return "AppConfig(<redacted>)"


def load_app_config(env=os.environ, *, require_api: bool = True) -> AppConfig:
    apply_env_aliases(env, WORKER_DEFAULTS)
    s = load_settings(require_api=require_api)
    ws = s.workspace
    template = unescape_template(env.get("FINAL_POST_TEMPLATE") or DEFAULT_TEMPLATE)
    images_only = unescape_template(env.get("FINAL_POST_TEMPLATE_IMAGES_ONLY") or DEFAULT_IMAGES_ONLY_TEMPLATE)
    videos_only = unescape_template(env.get("FINAL_POST_TEMPLATE_VIDEOS_ONLY") or DEFAULT_VIDEOS_ONLY_TEMPLATE)
    try:  # startup par hi galat template pakdo, 3 ghante baad nahi
        validate_template(template, VARS_FULL, frozenset({"video_links"}))
        validate_template(images_only, VARS_IMAGES_ONLY, VARS_IMAGES_ONLY)
        validate_template(videos_only, VARS_VIDEOS_ONLY, VARS_VIDEOS_ONLY)
    except TemplateError as e:
        raise ConfigError(f"Post template galat: {e}") from e
    max_links = _num(env, "MAX_VIDEO_LINKS_PER_POST", 8)
    if not 1 <= max_links <= 8:
        raise ConfigError("MAX_VIDEO_LINKS_PER_POST 1 se 8 ke beech hona chahiye (spec: max 8).")
    if _num(env, "MAX_CONCURRENT_ZIPS", 1) != 1:
        raise ConfigError("MAX_CONCURRENT_ZIPS sirf 1 ho sakta hai (strict ZIP-by-ZIP).")
    policy = (env.get("IMAGE_FAILURE_POLICY") or "pause").strip().lower()
    if policy not in ("pause", "skip"):
        raise ConfigError("IMAGE_FAILURE_POLICY 'pause' ya 'skip' hona chahiye.")
    dest_raw = (env.get("FINAL_POST_DESTINATION_ID") or "").strip()
    bf = (env.get("SCAN_BACKFILL_FROM_MESSAGE_ID") or "").strip()
    default_db = f"sqlite:///{ws / 'app.db'}"
    return AppConfig(
        settings=s,
        worker=WorkerConfig(
            workspace=ws, limits=s.limits, poll_interval=_num(env, "WORKER_POLL_INTERVAL_SECONDS", 30.0, float),
            stage_max_retries=_num(env, "STAGE_MAX_RETRIES", 5), imgbb_delay=s.imgbb_delay,
            image_failure_policy=policy, telegraph_title=s.telegraph_title, telegraph_author=s.telegraph_author,
            telegraph_require_verified=_bool(env, "TELEGRAPH_REQUIRE_VERIFIED", False),
            template=template, images_only_template=images_only, videos_only_template=videos_only,
            video_name_base=(env.get("VIDEO_NAME_BASE") or "").strip(),
            image_name_base=(env.get("IMAGE_NAME_BASE") or "").strip(),
            max_links_per_post=max_links, cleanup=_bool(env, "CLEANUP_AFTER_COMPLETE", True),
            bot_username=s.diskwala_bot),
        video_policy=VideoPolicy(max_attempts=max(1, s.upload_retries), resend_on_timeout=_bool(env, "DISKWALA_RESEND_ON_TIMEOUT", True),
                                 late_reply_grace=_num(env, "DISKWALA_LATE_REPLY_GRACE_SECONDS", 300.0, float)),
        uploader=UploaderConfig(bot_username=s.diskwala_bot, response_timeout=float(s.upload_timeout),
                                poll_interval=s.upload_poll, max_retries=s.upload_retries,
                                max_upload_bytes=(s.max_upload_mb or 2000) * MB, link_domains=s.link_domains,
                                delay_between=s.upload_delay, max_flood_wait=s.max_flood_wait,
                                delete_after_link=_bool(env, "DISKWALA_DELETE_AFTER_LINK", True)),
        publish_policy=PublishPolicy(max_flood_wait=s.max_flood_wait),
        database_url=(env.get("DATABASE_URL") or "").strip() or default_db,
        session_string=(env.get("SESSION_STRING") or "").strip(),
        dest_id=int(dest_raw) if dest_raw else None,
        backfill_from=int(bf) if bf else None,
        link_preview=_bool(env, "POST_LINK_PREVIEW", False),
        health_port=_num(env, "PORT", _num(env, "HEALTH_PORT", 8000)),
        health_enabled=_bool(env, "HEALTH_ENABLED", True),
        log_level=(env.get("LOG_LEVEL") or "INFO").strip().upper())
