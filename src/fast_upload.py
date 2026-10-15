"""Parallel Telegram upload (video -> bot).

Telethon ka send_file parts ek ke baad ek bhejta hai (har 512 KB ke baad ack ka intezaar) => upload slow. Yahan file ke
512 KB parts ek saath (N in-flight) SaveBigFilePart se jaate hain, phir ek hi send. Document (UPLOAD_AS=document, default) ke liye.
Upload phase me koi bhi error => caller purane send_file par fallback karta hai.
"""
from __future__ import annotations

import asyncio
import logging
import math
import mimetypes
import os
import random
from pathlib import Path
from types import SimpleNamespace

log = logging.getLogger("fast_upload")

PART = 512 * 1024
MIN_SIZE = 10 * 1024 * 1024    # Telegram "big file" API 10 MB se upar ke liye hai
MAX_PARTS = 4000               # Telegram limit (512 KB * 4000 = ~2 GB)
PART_RETRIES = 3


def connections_from_env(env=os.environ, default: int = 8) -> int:
    try:
        return max(1, min(int(env.get("UPLOAD_CONNECTIONS") or default), 16))
    except ValueError:
        return default


def _tl() -> SimpleNamespace:
    from telethon.tl.functions.upload import SaveBigFilePartRequest
    from telethon.tl.types import DocumentAttributeFilename, InputFileBig, InputMediaUploadedDocument
    return SimpleNamespace(SaveBigFilePartRequest=SaveBigFilePartRequest, InputFileBig=InputFileBig,
                           InputMediaUploadedDocument=InputMediaUploadedDocument,
                           DocumentAttributeFilename=DocumentAttributeFilename)


async def parallel_upload_file(client, path, connections: int = 8, progress_callback=None, part: int = PART, tl=None):
    """File ke parts ek saath upload; InputFileBig return."""
    tl = tl or _tl()
    path = Path(path)
    size = path.stat().st_size
    total = math.ceil(size / part)
    if size < 1 or total > MAX_PARTS:
        raise ValueError(f"parallel upload ke liye size theek nahi ({size} bytes, {total} parts)")
    file_id = random.randrange(-2**63, 2**63)
    state = {"next": 0, "done": 0}

    with open(path, "rb") as f:
        async def worker() -> None:
            while True:
                i = state["next"]
                if i >= total:
                    return
                state["next"] = i + 1
                f.seek(i * part)             # await ke bina seek+read: asyncio me atomic
                data = f.read(part)
                for attempt in range(PART_RETRIES + 1):
                    try:
                        ok = await client(tl.SaveBigFilePartRequest(file_id, i, total, data))
                        break
                    except (ConnectionError, TimeoutError, OSError, asyncio.TimeoutError):
                        if attempt == PART_RETRIES:
                            raise
                        await asyncio.sleep(2 * (attempt + 1))
                if not ok:
                    raise IOError(f"Telegram ne part {i} reject kiya")
                state["done"] += len(data)
                if progress_callback:
                    progress_callback(state["done"], size)

        tasks = [asyncio.ensure_future(worker()) for _ in range(max(1, min(connections, total)))]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    if state["done"] != size:
        raise IOError(f"upload adhoora: {state['done']} / {size} bytes")
    return tl.InputFileBig(id=file_id, parts=total, name=path.name)


async def send_file_parallel(client, entity, path, progress_callback=None, connections: int | None = None, tl=None):
    """Parallel upload + document ke roop me send. Message return. Upload phase fail => Exception (fallback ke liye)."""
    tl = tl or _tl()
    n = connections or connections_from_env()
    handle = await parallel_upload_file(client, path, n, progress_callback, tl=tl)
    name = Path(path).name
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    media = tl.InputMediaUploadedDocument(file=handle, mime_type=mime, force_file=True,
                                          attributes=[tl.DocumentAttributeFilename(file_name=name)])
    try:
        return await client.send_file(entity, media)
    except (TypeError, ValueError) as e:
        # Telethon ne media accept hi nahi kiya => kuch bheja nahi gaya; caller safe fallback kar sakta hai
        raise UploadNotSent(str(e)) from e
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        # Send ke dauran error: message shayad pahunch gaya. Fallback = double send ka khatra, isliye upar jaane do.
        raise SendPhaseError(f"{type(e).__name__}: {e}") from e


class UploadNotSent(Exception):
    """Parallel send se pehle hi fail (kuch Telegram par nahi gaya): purane tareeke se bhejna safe hai."""


class SendPhaseError(Exception):
    """Send ke beech fail: ambiguous (shayad chala gaya). Fallback mat karo."""
