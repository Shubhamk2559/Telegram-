"""Video upload bots: DiskWala + Flezen, aur unke beech switch.

Design notes (Hinglish):
- Har bot ka ek BotProfile: username, timeout, aur uska UploaderConfig/VideoPolicy.
- Active bot database (kv) me save rehta hai => redeploy par bhi wahi rehta hai. Control bot ka /switchbot ise badalta hai.
- Switch ka asar agli baar jab ZIP ka video stage shuru ho (ya PAUSED ZIP resume ho) tab dikhta hai. Beech ke video ko
  purane bot par hi khatam hone diya jata hai (worker ek ZIP ke videos ke liye ek hi uploader banata hai).
- Flezen bot (real chat se): "Downloading the file, please wait..." video ko reply deta hai, phir ~20 min baad link
  (photo + caption) bina reply ke aata hai; 1024 MB se bada file nahi leta. Link milte hi video + uske saare bot messages chat se delete hote hain.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from .config import ConfigError
from .diskwala_uploader import UploaderConfig
from .strict_videos import VideoPolicy
from .zip_processor import MB

KV_ACTIVE_BOT = "upload_bot"

DISKWALA = "diskwala"
FLEZEN = "flezen"

FLEZEN_DEFAULT_USERNAME = "FlezenUploadBot"
# Real chat: 394.7 MB video -> "Downloading..." 11:51, link 12:12 (~21 min). Isliye 1 ghanta; bada video ho to env se badhao.
FLEZEN_DEFAULT_TIMEOUT = 3600.0
FLEZEN_DEFAULT_MAX_MB = 1000  # bot: "I can only upload files smaller than 1024.0 MB" -> bheje bina hi pakad lo
# "Downloading the file, please wait..." - env se badal sakte ho (FLEZEN_PROGRESS_REGEX)
FLEZEN_DEFAULT_PROGRESS_RE = r"downloading (the )?file"


@dataclass(frozen=True)
class BotProfile:
    key: str
    label: str
    uploader: UploaderConfig
    policy: VideoPolicy

    @property
    def username(self) -> str:
        return self.uploader.bot_username


def _num(env, name: str, default, cast=float):
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError as e:
        raise ConfigError(f"{name} number hona chahiye, mila: {raw!r}") from e


def _bool(env, name: str, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


def build_profiles(env, diskwala_uploader: UploaderConfig, diskwala_policy: VideoPolicy) -> tuple[dict, str]:
    """(profiles, default_key). DiskWala ka config jaisa tha waisa hi; Flezen uska copy + apne overrides."""
    username = (env.get("FLEZEN_BOT_USERNAME") or "").strip().lstrip("@") or FLEZEN_DEFAULT_USERNAME
    domains = tuple(d.strip().lower() for d in (env.get("FLEZEN_LINK_DOMAINS") or "").split(",") if d.strip())
    progress = (env.get("FLEZEN_PROGRESS_REGEX") or "").strip() or FLEZEN_DEFAULT_PROGRESS_RE
    flezen_uploader = dataclasses.replace(
        diskwala_uploader,
        bot_username=username,
        response_timeout=_num(env, "FLEZEN_RESPONSE_TIMEOUT_SECONDS", FLEZEN_DEFAULT_TIMEOUT),
        link_domains=domains,  # DiskWala ke domain filter se alag
        max_upload_bytes=_num(env, "FLEZEN_MAX_UPLOAD_MB", FLEZEN_DEFAULT_MAX_MB, int) * MB,
        progress_re=progress,
        reply_to_video=False,
        delete_after_link=_bool(env, "FLEZEN_DELETE_AFTER_LINK", True))
    flezen_policy = dataclasses.replace(
        diskwala_policy,
        resend_on_timeout=_bool(env, "FLEZEN_RESEND_ON_TIMEOUT", False),  # 1 ghante baad resend = duplicate; pause behtar
        late_reply_grace=_num(env, "FLEZEN_LATE_REPLY_GRACE_SECONDS", diskwala_policy.late_reply_grace))
    profiles = {
        DISKWALA: BotProfile(DISKWALA, "DiskWala", diskwala_uploader, diskwala_policy),
        FLEZEN: BotProfile(FLEZEN, "Flezen", flezen_uploader, flezen_policy),
    }
    default = (env.get("UPLOAD_BOT_DEFAULT") or "").strip().lower() or DISKWALA
    if default not in profiles:
        raise ConfigError(f"UPLOAD_BOT_DEFAULT '{default}' galat hai. Options: {', '.join(profiles)}")
    return profiles, default


# ----------------------------------------------------------------------------- active bot (database)

def active_key(repo, profiles: dict, default: str) -> str:
    v = (repo.get_kv(KV_ACTIVE_BOT) or "").strip().lower()
    return v if v in profiles else default


def set_active(repo, profiles: dict, key: str) -> BotProfile:
    key = key.strip().lower()
    if key not in profiles:
        raise ConfigError(f"Unknown bot '{key}'. Options: {', '.join(profiles)}")
    repo.set_kv(KV_ACTIVE_BOT, key)
    return profiles[key]


# ----------------------------------------------------------------------------- lazy channel

class LazyBotChannel:
    """BotChannel jo pehli use par hi Telegram se connect hota hai.
    Isse jo bot abhi active nahi hai (ya galat username wala) wo worker ka startup nahi rokta."""

    def __init__(self, client, username: str, as_document: bool = True):
        self.client, self.username, self.as_document = client, username, as_document
        self._real = None

    async def _ch(self):
        if self._real is None:
            from .diskwala_uploader import TelethonBotChannel
            self._real = await TelethonBotChannel.connect(self.client, self.username, self.as_document)
        return self._real

    async def latest_id(self) -> int:
        return await (await self._ch()).latest_id()

    async def send_text(self, text: str) -> int:
        return await (await self._ch()).send_text(text)

    async def send_video(self, path, progress) -> int:
        return await (await self._ch()).send_video(path, progress)

    async def fetch_replies(self, after_id: int):
        return await (await self._ch()).fetch_replies(after_id)

    async def find_recent_outgoing(self, after_id: int, size: int):
        return await (await self._ch()).find_recent_outgoing(after_id, size)

    async def delete_messages(self, ids: list[int]) -> None:
        await (await self._ch()).delete_messages(ids)
