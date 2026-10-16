"""Memory kam rakhne ke helpers (Koyeb free instance ~512 MB).

Python + threads (asyncio.to_thread) me glibc malloc freed memory OS ko wapas nahi deta => RSS badhta jaata hai.
Har job ke baad gc + malloc_trim chalate hain aur RSS log karte hain (OOM restart se pehle trend dikhe).
"""
from __future__ import annotations

import ctypes
import gc
import logging

log = logging.getLogger("mem")


def rss_mb() -> float | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def trim() -> None:
    """gc + glibc ko freed memory OS ko lautane ko kaho. Kisi bhi platform par safe (fail => chup)."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def trim_and_log(tag: str = "job") -> None:
    before = rss_mb()
    trim()
    after = rss_mb()
    if before is not None and after is not None:
        log.info("Memory (%s ke baad): RSS %.0f MB -> %.0f MB", tag, before, after)
