"""Startup / `doctor` checks: galat config ya kam disk ka pata deploy ke time chale, 3 ghante baad nahi."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .app_config import AppConfig
from .db import Database, current_version, migrate
from .zip_processor import MB

OK, WARN, ERROR = "ok", "warn", "error"


@dataclass
class Check:
    level: str
    name: str
    message: str


def on_koyeb(env=os.environ) -> bool:
    """Koyeb apne instances me KOYEB_* env vars set karta hai (sirf hint, behavior isse nahi badalta)."""
    return any(k.startswith("KOYEB_") for k in env)


def required_disk_bytes(cfg: AppConfig) -> int:
    lim = cfg.worker.limits
    return lim.max_archive_bytes + lim.max_extracted_bytes + lim.min_free_bytes


def preflight(cfg: AppConfig, *, env=os.environ, free_bytes: int | None = None, check_db: bool = True) -> list[Check]:
    s, out = cfg.settings, []
    add = lambda level, name, msg: out.append(Check(level, name, msg))  # noqa: E731
    cloud = on_koyeb(env)

    add(OK if (s.api_id and s.api_hash) else ERROR, "telegram_api",
        "API_ID/API_HASH set hain." if (s.api_id and s.api_hash) else "API_ID / API_HASH (TELEGRAM_API_ID/HASH) set nahi hain.")
    if cfg.session_string:
        add(OK, "session", "TELEGRAM_SESSION_STRING set hai.")
    elif cloud:
        add(ERROR, "session", "Koyeb par TELEGRAM_SESSION_STRING zaroori hai (file session redeploy par ud jaata hai, QR login possible nahi).")
    else:
        has_file = Path(f"{s.session_base}.session").exists()
        add(OK if has_file else ERROR, "session",
            "Local session file mili." if has_file else "Session nahi: `python -m src.main login` chalao, phir `python -m src.app export-session`.")
    add(OK if cfg.dest_id is not None else ERROR, "destination",
        "FINAL_POST_DESTINATION_ID set hai." if cfg.dest_id is not None else "FINAL_POST_DESTINATION_ID set nahi hai.")
    n_keys = len(s.all_imgbb_keys())
    add(OK if n_keys else ERROR, "imgbb",
        (f"{n_keys} ImgBB API key set hain (rate limit par agli key chalegi)." if n_keys > 1 else "IMGBB_API_KEY set hai.")
        if n_keys else "IMGBB_API_KEY (ya IMGBB_API_KEYS) set nahi hai.")
    if s.telegraph_token:
        add(OK, "telegraph_token", "TELEGRAPH_ACCESS_TOKEN set hai.")
    else:
        add(OK, "telegraph_token", "Token khaali: pehli baar account ban ke database me save hoga (redeploy par wahi rahega).")

    if cfg.database_url.startswith("sqlite") or "://" not in cfg.database_url:
        add(ERROR if cloud else WARN, "database",
            "SQLite use ho raha hai: Koyeb par local disk redeploy par ud jaati hai. DATABASE_URL me PostgreSQL do." if cloud
            else "SQLite (local/dev). Production ke liye DATABASE_URL me PostgreSQL do.")
    if check_db:
        try:
            db = Database(cfg.database_url)
            try:
                migrate(db)
                add(OK, "database_connect", f"Database connect ({db.kind}), schema version {current_version(db)}.")
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001 - password message me na aaye
            add(ERROR, "database_connect", f"Database connect fail: {type(e).__name__}")

    ws = cfg.worker.workspace
    ws.mkdir(parents=True, exist_ok=True)
    free = free_bytes if free_bytes is not None else shutil.disk_usage(ws).free
    lim = cfg.worker.limits
    need, minimum = required_disk_bytes(cfg), lim.max_archive_bytes + lim.min_free_bytes
    tip = "Bada instance/disk lo, ya MAX_ARCHIVE_MB / MAX_EXTRACTED_MB kam karo."
    if free < minimum:
        add(ERROR, "disk", f"Free disk {free // MB} MB hai; ek maximum-size ZIP download karne ke liye bhi {minimum // MB} MB chahiye. {tip}")
    elif free < need:
        add(WARN, "disk", f"Free disk {free // MB} MB; poore limits (ZIP + extract + reserve) ke liye {need // MB} MB chahiye. "
                          f"Isse bade ZIP fail honge. {tip}")
    else:
        add(OK, "disk", f"Free disk {free // MB} MB (limits ke liye {need // MB} MB chahiye).")
    return out


def format_checks(checks: list[Check]) -> str:
    mark = {OK: "[ OK ]", WARN: "[WARN]", ERROR: "[FAIL]"}
    return "\n".join(f"{mark[c.level]} {c.name}: {c.message}" for c in checks)
