"""Extraction ke turant baad videos/images ko fixed naam dena (disk par asli rename).

Videos: size ke hisaab se bada pehle  -> "<VIDEO_NAME_BASE> 01.mp4", "<VIDEO_NAME_BASE> 02.mp4", ...
Images: ZIP ke natural file order me  -> "<IMAGE_NAME_BASE> 01.jpg", "<IMAGE_NAME_BASE> 02.jpg", ...
Base khaali ho to us type ka rename nahi hota (purana behavior).
Numbering har ZIP me 01 se shuru hoti hai; 99 se zyada files hon to width apne aap badhti hai (001...).
Same content wali duplicate file (same file_id) ko number nahi milta (collect_images/collect_videos use skip karte hain).

restore_recorded_names(): redeploy ke baad ZIP dobara extract ho to files ko DB me saved naam/path par wapas rakhta hai.
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


def _apply_moves(files: list[dict], dest: Path, moves: dict[int, str]) -> int:
    """moves: id(file) -> naya relative_path. Do-phase: pehle sab temp naam, phir final (naam takrao se bachav)."""
    todo = [f for f in files if id(f) in moves and moves[id(f)] != f["relative_path"]]
    tmp: list[tuple[dict, Path]] = []
    for i, f in enumerate(todo):
        src = dest / f["relative_path"]
        t = src.with_name(f".rn_{i}{src.suffix}")
        src.rename(t)
        tmp.append((f, t))
    for f, t in tmp:
        rel = moves[id(f)]
        final = dest / rel
        final.parent.mkdir(parents=True, exist_ok=True)
        t.replace(final)
        f["relative_path"] = rel
        f["filename"] = final.name
    return len(tmp)


def rename_media(files: list[dict], dest: Path, video_base: str = "", image_base: str = "") -> int:
    """files (extract_archive ka result) ko jagah par badalta hai: disk par rename + filename/relative_path update.
    Sab files <dest>/images/ ya <dest>/videos/ me flat ho jaati hain (naam unique hain). Returns renamed count."""
    dest = Path(dest)
    plan: dict[int, str] = {}
    vb, ib = clean_base(video_base), clean_base(image_base)
    if vb:
        plan.update(_plan(files, "video", vb))
    if ib:
        plan.update(_plan(files, "image", ib))
    moves = {id(f): f"{'images' if f['type'] == 'image' else 'videos'}/{plan[id(f)]}" for f in files if id(f) in plan}
    return _apply_moves(files, dest, moves)


def restore_recorded_names(files: list[dict], dest: Path, recorded: dict[str, str]) -> int:
    """recorded: file_id -> dest ke relative path (jaisa DB me saved hai). Dobara extract hui files ko wahi naam do."""
    dest = Path(dest)
    moves, used = {}, set()
    for f in files:
        rel = recorded.get(f["file_id"])
        if rel and rel not in used:  # duplicate content ek hi record se jud sakta hai: pehli file hi lo
            moves[id(f)] = rel
            used.add(rel)
    return _apply_moves(files, dest, moves)
