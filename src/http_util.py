"""Chhota HTTP layer (sirf standard library). Tests me iski jagah fake transport lagta hai.

Koi naya pip package nahi chahiye.
"""
from __future__ import annotations

import socket
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Protocol


class NetworkError(Exception):
    """Connection / timeout jaisi temporary problem (HTTP response mila hi nahi)."""


@dataclass
class Response:
    status: int
    headers: dict = field(default_factory=dict)  # keys lowercase
    body: bytes = b""

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class Http(Protocol):
    def request(self, method: str, url: str, *, headers: dict | None = None,
                data: bytes | None = None, timeout: float = 60, max_bytes: int | None = None) -> Response: ...


class UrllibHttp:
    """HTTP errors (4xx/5xx) exception nahi dete, Response me status aata hai."""

    def request(self, method, url, *, headers=None, data=None, timeout=60, max_bytes=None) -> Response:
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - https URLs hi use hote hain
                body = r.read(max_bytes) if max_bytes else r.read()
                return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, body)
        except urllib.error.HTTPError as e:
            try:
                body = e.read(max_bytes) if max_bytes else e.read()
            except Exception:  # noqa: BLE001
                body = b""
            return Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            raise NetworkError(f"{type(e).__name__}: {e}") from e


def encode_multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """multipart/form-data body banata hai. files = {field: (filename, bytes, mimetype)}."""
    boundary = uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
    for name, (filename, content, mime) in files.items():
        safe = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{safe}"\r\n'
                f"Content-Type: {mime}\r\n\r\n").encode()
        out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"
