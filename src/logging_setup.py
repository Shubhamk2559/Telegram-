"""Logging jisme secrets kabhi print nahi hote (API hash, session string, tokens, keys, DB password)."""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlparse

SECRET_ENV = ("API_HASH", "TELEGRAM_API_HASH", "SESSION_STRING", "TELEGRAM_SESSION_STRING", "IMGBB_API_KEY", "IMGBB_API_KEYS",
              "TELEGRAPH_ACCESS_TOKEN", "DATABASE_URL")
PATTERNS = [re.compile(r"(?i)(key|access_token|token|password|session)=([^&\s\"']+)")]


def secret_values(env=os.environ) -> list[str]:
    out = []
    for k in SECRET_ENV:
        v = (env.get(k) or "").strip()
        if not v:
            continue
        out.append(v)
        if k == "IMGBB_API_KEYS":  # har key alag mask ho (poori list ek string me match nahi hoti)
            out.extend(p for p in re.split(r"[,;\s]+", v) if p)
        if k == "DATABASE_URL":
            pw = urlparse(v).password
            if pw:
                out.append(pw)
    return sorted({s for s in out if len(s) >= 6}, key=len, reverse=True)


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        text = text.replace(s, "***")
    for p in PATTERNS:
        text = p.sub(lambda m: f"{m.group(1)}=***", text)
    return text


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        self.secrets = secrets if secrets is not None else secret_values()

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self.secrets)
        record.args = ()
        if record.exc_info:  # traceback text me bhi secrets na jayein
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info), self.secrets)
            record.exc_info = None
        return True


def setup_logging(level: str = "INFO") -> None:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    h.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger("telethon").setLevel(logging.WARNING)
