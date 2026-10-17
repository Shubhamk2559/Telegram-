"""ImgBB API v1 upload (official docs: https://api.imgbb.com/).

Docs ke mutabiq:
  POST https://api.imgbb.com/1/upload?key=API_KEY   (image = binary file / base64 / URL, max 32 MB)
  Response JSON: data.url, data.display_url, data.url_viewer, data.thumb.url, data.medium.url,
                 data.delete_url, success, status.

IMPORTANT: API response me HTML embed code NAHI aata (ye sirf ImgBB website ke UI me dikhta hai).
Isliye `build_html_embeds()` API ke URL fields se snippet BANATA hai, aur manifest me ye
"constructed" ke naam se save hota hai, "returned by ImgBB" ke naam se nahi.
"""
from __future__ import annotations

import html
import json
import logging
import mimetypes
import re
import threading
import time
from pathlib import Path
from typing import Callable

from .http_util import Http, NetworkError, encode_multipart

IMGBB_ENDPOINT = "https://api.imgbb.com/1/upload"
MAX_IMAGE_BYTES = 32_000_000  # docs: "up to 32 MB"


class ImgBBError(Exception):
    """Ek image ka upload fail hua (baaki images chal sakti hain)."""


class ImgBBFatal(ImgBBError):
    """Poora batch rokna chahiye (galat API key, rate limit...). Bypass nahi karte."""


class ImgBBRateLimited(ImgBBFatal):
    """Is API key par rate limit lagi. Multi-key pool me dusri key try hoti hai; akeli key ho to batch ruk kar wait karta hai."""


def redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def build_html_embeds(data: dict) -> dict:
    """API fields se 'HTML full linked' aur 'HTML thumbnail linked' jaise snippets banao.

    Format ImgBB website ke snippets jaisa rakha hai (<a href=viewer><img src=...></a>),
    par ye ImgBB ne return nahi kiye - hum ne banaye hain.
    """
    viewer = data.get("url_viewer") or data.get("url") or ""
    full = data.get("url") or ""
    thumb = (data.get("thumb") or {}).get("url") or full
    alt = html.escape(str(data.get("title") or ""), quote=True)

    def one(src: str) -> str:
        return (f'<a href="{html.escape(viewer, quote=True)}">'
                f'<img src="{html.escape(src, quote=True)}" alt="{alt}" border="0"></a>')

    return {"full_linked": one(full), "thumb_linked": one(thumb)}


def parse_upload_response(payload: dict) -> dict:
    """Valid response se zaroori fields nikalo; kuch bhi missing ho to ImgBBError."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not (isinstance(data, dict) and payload.get("success") is True and data.get("url")):
        raise ImgBBError("ImgBB response me success/data.url nahi mila")
    return {
        "id": data.get("id"),
        "url": data["url"],
        "display_url": data.get("display_url"),
        "viewer_url": data.get("url_viewer"),
        "thumb_url": (data.get("thumb") or {}).get("url"),
        "medium_url": (data.get("medium") or {}).get("url"),
        "delete_url": data.get("delete_url"),
        "width": data.get("width"),
        "height": data.get("height"),
        "size": data.get("size"),
        "html_embeds": build_html_embeds(data),
    }


def _error_message(body_text: str) -> str:
    try:
        err = json.loads(body_text).get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:200]
        if err:
            return str(err)[:200]
    except (ValueError, AttributeError):
        pass
    return re.sub(r"\s+", " ", body_text).strip()[:200] or "(khaali response)"


class ImgBBClient:
    def __init__(self, api_key: str, http: Http, *, max_retries: int = 3, timeout: float = 120,
                 sleep: Callable[[float], None] = time.sleep):
        if not api_key:
            raise ImgBBFatal("IMGBB_API_KEY khaali hai.")
        self._key = api_key
        self.http = http
        self.max_retries = max_retries
        self.timeout = timeout
        self.sleep = sleep

    def upload(self, path: Path, name: str | None = None) -> dict:
        path = Path(path)
        try:
            size = path.stat().st_size
        except OSError as e:
            raise ImgBBError(f"File nahi mili / padh nahi sakte: {e}") from e
        if size == 0:
            raise ImgBBError("File khaali hai (0 bytes).")
        if size > MAX_IMAGE_BYTES:
            raise ImgBBError(f"File {size / 1e6:.1f} MB hai; ImgBB limit 32 MB hai.")
        content = path.read_bytes()
        filename = name or path.name
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body, ctype = encode_multipart({}, {"image": (filename, content, mime)})
        del content  # 32 MB tak ki image ki do copy (content + body) ek saath memory me na rahein
        url = f"{IMGBB_ENDPOINT}?key={self._key}"

        last = "unknown error"
        rate_limited = False
        for attempt in range(self.max_retries + 1):
            if attempt:
                self.sleep(min(2 ** attempt, 30))
            try:
                resp = self.http.request("POST", url, headers={"Content-Type": ctype}, data=body,
                                         timeout=self.timeout)
            except NetworkError as e:
                last = redact(str(e), self._key)
                continue
            text = resp.text()
            if resp.status == 200:
                try:
                    return parse_upload_response(json.loads(text))
                except ValueError as e:
                    raise ImgBBError("ImgBB ne valid JSON nahi diya.") from e
            msg = redact(_error_message(text), self._key)
            # ImgBB rate limit kabhi 429 deta hai, kabhi HTTP 400 + "Rate limit reached." (asli logs me yahi aaya)
            if resp.status == 429 or (resp.status == 400 and re.search(r"rate.?limit", msg, re.I)):
                rate_limited = True
                last = f"HTTP {resp.status} rate limit: {msg}"
                ra = resp.headers.get("retry-after", "")
                if ra.isdigit() and int(ra) <= 60 and attempt < self.max_retries:
                    self.sleep(int(ra))
                continue
            if resp.status >= 500:
                last = f"HTTP {resp.status}: {msg}"
                continue
            if resp.status in (401, 403) or re.search(r"api.{0,10}key", msg, re.I):
                raise ImgBBFatal(f"ImgBB ne API key reject ki (HTTP {resp.status}): {msg}")
            raise ImgBBError(f"HTTP {resp.status}: {msg}")
        if rate_limited:
            raise ImgBBRateLimited(f"ImgBB rate limit, retries ke baad bhi: {last}. Thodi der baad dobara chalao.")
        raise ImgBBError(f"Retries ke baad bhi fail: {last}")


# ----------------------------------------------------------------------------- multiple API keys
log = logging.getLogger("imgbb")


class ImgBBPool:
    """Ek se zyada ImgBB API keys. Interface ImgBBClient jaisa: upload(path, name) -> dict.

    - Keys barabari se (round-robin) chalti hain, taaki kisi ek key par limit jaldi na lage.
    - Kisi key par rate limit lage => wo `cooldown` seconds ke liye side me, SAME image turant agli key se upload hoti hai.
    - Galat/reject hui key => is run me band, baaki keys chalti rahengi.
    - Sab keys limit/band ho to ImgBBFatal (worker wahi purana wait/pause karta hai).
    Upload ka result sirf upload hone par DB me save hota hai; image dobara upload nahi hoti, to uska URL kabhi nahi badalta.
    """

    def __init__(self, clients: list, *, cooldown: float = 120.0, now: Callable[[], float] = time.monotonic):
        if not clients:
            raise ImgBBFatal("Koi ImgBB API key nahi.")
        self._clients = list(clients)
        self._cool_until = [0.0] * len(self._clients)
        self._dead = [False] * len(self._clients)
        self._i = 0
        self._cooldown, self._now = cooldown, now
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._clients)

    def _pick(self, tried: set) -> int | None:
        n, t = len(self._clients), self._now()
        for step in range(n):
            j = (self._i + step) % n
            if j not in tried and not self._dead[j] and self._cool_until[j] <= t:
                self._i = (j + 1) % n
                return j
        return None

    def upload(self, path: Path, name: str | None = None) -> dict:
        tried: set[int] = set()
        rate_hit = False
        last_reject: ImgBBFatal | None = None
        while True:
            with self._lock:
                j = self._pick(tried)
            if j is None:
                break
            tried.add(j)
            try:
                return self._clients[j].upload(path, name)
            except ImgBBRateLimited:
                rate_hit = True
                with self._lock:
                    self._cool_until[j] = self._now() + self._cooldown
                log.warning("ImgBB key #%d par rate limit: %ds side me, agli key try ho rahi hai.", j + 1, int(self._cooldown))
            except ImgBBFatal as e:  # key reject (galat/expired)
                last_reject = e
                with self._lock:
                    self._dead[j] = True
                log.error("ImgBB key #%d reject hui, is run me band: %s", j + 1, e)
        t = self._now()
        cooling = any(not d and c > t for d, c in zip(self._dead, self._cool_until))
        if rate_hit or cooling:
            raise ImgBBRateLimited(f"ImgBB rate limit: sab {len(self._clients)} key(s) par limit lagi hai. Thodi der baad dobara chalao.")
        raise last_reject or ImgBBFatal("ImgBB: koi bhi API key kaam ki nahi.")


def make_imgbb(keys, http: Http, *, max_retries: int = 3, cooldown: float = 120.0):
    """1 key => seedha ImgBBClient (purana behaviour); 2+ keys => ImgBBPool."""
    keys = [k for k in keys if k]
    if not keys:
        raise ImgBBFatal("IMGBB_API_KEY khaali hai.")
    clients = [ImgBBClient(k, http, max_retries=max_retries) for k in keys]
    return clients[0] if len(clients) == 1 else ImgBBPool(clients, cooldown=cooldown)
