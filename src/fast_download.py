"""Parallel Telegram download (public Telethon API: client.iter_download).

Telegram ek request me max 512 KB deta hai aur Telethon ek ke baad ek request karta hai => speed latency/throttle par atak jaati
hai (~300 KB/s). Yahan file ko N hisso me baant kar N iter_download ek saath chalte hain, har hissa apne offset par file me likhta hai.
Koi bhi gadbad (kam bytes, error) => exception; app.py purane download_media par fallback karta hai.
"""
from __future__ import annotations

import asyncio
import inspect
import math
import os

PART = 512 * 1024              # Telegram request size (1 MB boundary cross nahi karta)
MIN_SIZE = 8 * 1024 * 1024     # isse chhoti files par parallel ka fayda nahi


def connections_from_env(env=os.environ, default: int = 4) -> int:
    try:
        return max(1, min(int((env.get("DOWNLOAD_CONNECTIONS") or default)), 8))
    except ValueError:
        return default


def plan(size: int, connections: int, part: int = PART) -> list[tuple[int, int]]:
    """[(start_part, parts_count)] - har worker ko barabar contiguous hissa."""
    n_parts = math.ceil(size / part)
    conns = max(1, min(connections, n_parts))
    per = math.ceil(n_parts / conns)
    out, start = [], 0
    while start < n_parts:
        cnt = min(per, n_parts - start)
        out.append((start, cnt))
        start += cnt
    return out


async def parallel_download(client, message, path: str, connections: int = 4, progress_callback=None,
                            part: int = PART) -> int:
    size = int(message.file.size or 0)
    if size <= 0 or getattr(message, "media", None) is None:
        raise ValueError("parallel download: file size/media nahi mila")
    media = message.media
    done = 0

    def bump(n: int) -> None:
        nonlocal done
        done += n
        if progress_callback:
            progress_callback(done, size)

    with open(path, "wb") as f:
        f.truncate(size)

        async def worker(start_part: int, count: int) -> None:
            offset = start_part * part
            end = min(size, (start_part + count) * part)
            pos = offset
            it = client.iter_download(media, offset=offset, limit=count, request_size=part, file_size=size)
            try:
                async for chunk in it:
                    n = min(len(chunk), end - pos)
                    if n <= 0:
                        break
                    f.seek(pos)           # await ke bina seek+write: asyncio me atomic
                    f.write(chunk[:n])
                    pos += n
                    bump(n)
                    if pos >= end:
                        break
            finally:
                close = getattr(it, "close", None)
                if close:
                    try:
                        res = close()
                        if inspect.isawaitable(res):
                            await res
                    except Exception:  # noqa: BLE001
                        pass
            if pos != end:
                raise IOError(f"hissa adhoora: {pos - offset} / {end - offset} bytes (offset {offset})")

        tasks = [asyncio.ensure_future(worker(s, c)) for s, c in plan(size, connections, part)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    got = os.path.getsize(path)
    if got != size or done != size:
        raise IOError(f"download adhoora: {done} / {size} bytes")
    return size
