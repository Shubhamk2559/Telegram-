"""Channel dhundhna aur uske ZIP messages list karna. Sirf wahi channels jinka access tumhare account ko hai."""
from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .zip_processor import MB, safe_name

ZIP_MIMES = {"application/zip", "application/x-zip-compressed", "application/x-zip"}


class ChannelError(Exception):
    pass


class DownloadError(Exception):
    pass


@dataclass
class ChannelRef:
    id: int
    title: str
    kind: str
    entity: Any = field(repr=False, default=None)


@dataclass
class ZipMessage:
    message_id: int
    filename: str
    size: int
    date: datetime | None
    message: Any = field(repr=False, default=None)


async def find_candidates(client, name: str, channel_id: int | None) -> list[ChannelRef]:
    found: list[ChannelRef] = []
    async for d in client.iter_dialogs():
        if not (d.is_channel or d.is_group):
            continue
        ent = d.entity
        if channel_id is not None:
            ok = channel_id in (d.id, getattr(ent, "id", None))
        else:
            ok = d.name == name  # exact match
        if ok:
            kind = "channel" if getattr(ent, "broadcast", False) else "group"
            found.append(ChannelRef(id=d.id, title=d.name, kind=kind, entity=ent))
    return found


async def resolve_channel(client, name: str, channel_id: int | None) -> ChannelRef:
    matches = await find_candidates(client, name, channel_id)
    if not matches:
        raise ChannelError(
            f"Channel nahi mila (naam: {name!r}, id: {channel_id}). "
            "Naam exact check karo ya .env me CHANNEL_ID daalo."
        )
    if len(matches) == 1:
        return matches[0]
    print(f"\n{len(matches)} channels is naam se mile. Sahi wala chuno:")
    for i, m in enumerate(matches, 1):
        print(f"  {i}. {m.title}  (id: {m.id}, type: {m.kind})")
    if not sys.stdin.isatty():
        ids = ", ".join(str(m.id) for m in matches)
        raise ChannelError(f"Multiple matches. .env me CHANNEL_ID set karo. IDs: {ids}")
    while True:
        choice = input("Number likho: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(matches):
            picked = matches[int(choice) - 1]
            print(f"Tip: agli baar poochne se bachne ke liye .env me CHANNEL_ID={picked.id} daalo.")
            return picked
        print("Galat number.")


async def list_zip_messages(client, channel: ChannelRef) -> list[ZipMessage]:
    out: list[ZipMessage] = []
    async for msg in client.iter_messages(channel.entity):
        f = getattr(msg, "file", None)
        if not f:
            continue
        name = f.name or ""
        if not (name.lower().endswith(".zip") or (f.mime_type or "") in ZIP_MIMES):
            continue
        out.append(ZipMessage(
            message_id=msg.id,
            filename=name or f"archive_{msg.id}.zip",
            size=int(f.size or 0),
            date=msg.date,
            message=msg,
        ))
    out.sort(key=lambda z: z.message_id)
    return out


async def download_zip(client, channel: ChannelRef, z: ZipMessage, settings: Settings) -> Path:
    """Download -> .part file -> size check -> rename. Interrupted download kabhi final naam nahi leta."""
    limits = settings.limits
    if z.size and z.size > limits.max_archive_bytes:
        raise DownloadError(f"Archive bohot bada hai ({z.size // MB} MB > limit {limits.max_archive_bytes // MB} MB)")
    settings.downloads_dir.mkdir(parents=True, exist_ok=True)
    final = settings.downloads_dir / f"{abs(channel.id)}_{z.message_id}_{safe_name(z.filename)}"
    if final.exists() and (not z.size or final.stat().st_size == z.size):
        print("  Pehle se downloaded hai, dobara download skip.")
        return final

    free = shutil.disk_usage(settings.downloads_dir).free
    if free < z.size + limits.min_free_bytes:
        raise DownloadError(f"Disk space kam: {free // MB} MB free, chahiye ~{(z.size + limits.min_free_bytes) // MB} MB")

    part = final.with_name(final.name + ".part")
    last = {"pct": -10}

    def progress(done: int, total: int) -> None:
        if total:
            pct = int(done * 100 / total)
            if pct >= last["pct"] + 10:
                last["pct"] = pct - pct % 10
                print(f"  Download: {pct}%")

    await client.download_media(z.message, file=str(part), progress_callback=progress)
    if not part.exists():
        raise DownloadError("Download file nahi bani.")
    if z.size and part.stat().st_size != z.size:
        raise DownloadError(
            f"Download adhoora ({part.stat().st_size} / {z.size} bytes). '.part' file rakhi hai, dobara chalao."
        )
    part.replace(final)
    return final
