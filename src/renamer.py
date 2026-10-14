"""Extraction ke turant baad videos/images ko fixed naam dena (disk par asli rename).

Videos: size ke hisaab se bada pehle  -> "<VIDEO_NAME_BASE> 01.mp4", "<VIDEO_NAME_BASE> 02.mp4", ...
Images: ZIP ke natural file order me  -> "<IMAGE_NAME_BASE> 01.jpg", "<IMAGE_NAME_BASE> 02.jpg", ...
Base khaali ho to us type ka rename nahi hota (purana behavior).
Numbering har ZIP me 01 se shuru hoti hai; 99 se zyada files hon to width apne aap badhti hai (001...).
Same content wali duplicate file (same file_id) ko number nahi milta (collect_images/collect_videos use skip karte hain).
"""
from __future__ import annotations

import re
from pathlib import Path

from .image_store import natural_key
from .posts import sort_videos

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def clean_base(base: str) -> str:
    return _BAD.sub("_", (base or "").strip()).strip(" .")[:100]


def _plan(files: list[dict], kind: str, base: str) -> dict[int, str]:
    """id(file_dict) -> naya filename."""
    items = [f for f in files if f["type"] == kind]
    if kind == "video":
        ordered = sort_videos(items)
    else:
        ordered = sorted(items, key=lambda f: natural_key(f.get("relative_path") or f["filename"]))
    uniq, seen, dups = [], set(), []
    for f in ordered:
        (dups if f["file_id"] in seen else uniq).append(f)
        seen.add(f["file_id"])
    width = max(2, len(str(len(uniq))))
    out = {id(f): f"{base} {i:0{width}d}{Path(f['filename']).suffix.lower()}" for i, f in enumerate(uniq, 1)}
    for j, f in enumerate(dups, 1):
        out[id(f)] = f"{base} dup {j:02d}{Path(f['filename']).suffix.lower()}"
    return out


def rename_media(files: list[dict], dest: Path, video_base: str = "", image_base: str = "") -> None:
    """files (extract_archive ka result) ko jagah par badalta hai: disk par rename + filename/relative_path update.
    Sab files <dest>/images/ ya <dest>/videos/ me flat ho jaati hain (naam unique hain)."""
    dest = Path(dest)
    plan: dict[int, str] = {}
    vb, ib = clean_base(video_base), clean_base(image_base)
    if vb:
        plan.update(_plan(files, "video", vb))
    if ib:
        plan.update(_plan(files, "image", ib))
    todo = [f for f in files if id(f) in plan]
    # Phase 1: sab ko temp naam (asli naam kisi naye naam se takra sakta hai), Phase 2: final naam.
    tmp: list[tuple[dict, Path]] = []
    for i, f in enumerate(todo):
        src = dest / f["relative_path"]
        t = src.with_name(f".rn_{i}{src.suffix}")
        src.rename(t)
        tmp.append((f, t))
    for f, t in tmp:
        folder = "images" if f["type"] == "image" else "videos"
        new = plan[id(f)]
        final = dest / folder / new
        final.parent.mkdir(parents=True, exist_ok=True)
        t.replace(final)
        f["filename"] = new
        f["relative_path"] = f"{folder}/{new}"
