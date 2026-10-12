"""Safe ZIP extraction. Sirf Python standard library use hoti hai.

Rules:
- Pehle poora archive VALIDATE hota hai (kuch bhi extract karne se pehle).
- Path traversal, absolute path, symlink, encrypted entries => archive REJECT.
- Size / file-count / compression-ratio limits.
- Extracted files kabhi execute nahi hoti, aur kuch bhi auto-delete nahi hota.
- Sirf images aur videos extract hoti hain: <dest>/images/... aur <dest>/videos/...
"""
from __future__ import annotations

import hashlib
import re
import shutil
import stat
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

MB = 1024 * 1024
CHUNK = 1024 * 1024
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov", ".webm", ".avi"})
MAX_SKIPPED_LISTED = 200


class ArchiveError(Exception):
    """Base error."""


class ArchiveRejected(ArchiveError):
    """Security rule ya limit todi gayi."""


class ArchiveCorrupted(ArchiveError):
    """ZIP kharab / adhoori hai."""


class InsufficientSpace(ArchiveError):
    """Disk space kam hai."""


@dataclass(frozen=True)
class Limits:
    max_archive_bytes: int = 2048 * MB
    max_extracted_bytes: int = 4096 * MB
    max_files: int = 10_000
    max_ratio: float = 200.0
    min_free_bytes: int = 500 * MB


@dataclass
class ExtractionResult:
    files: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    skipped_count: int = 0
    total_bytes: int = 0


def classify(name: str) -> str | None:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def safe_name(name: str, max_len: int = 100) -> str:
    cleaned = re.sub(r"[^\w.\- ]", "_", name, flags=re.UNICODE).strip(" .")
    return (cleaned or "file")[:max_len]


def unique_dir(base: Path) -> Path:
    """Folder pehle se bhara ho to naya naam (_retry2...) - purana kabhi overwrite/delete nahi."""
    if not base.exists() or not any(base.iterdir()):
        return base
    n = 2
    while True:
        candidate = base.with_name(f"{base.name}_retry{n}")
        if not candidate.exists():
            return candidate
        n += 1


def _free_name(path: Path) -> Path:
    if not path.exists():
        return path
    n = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _normalise(name: str) -> list[str]:
    if "\x00" in name:
        raise ArchiveRejected(f"Suspicious entry (null byte): {name!r}")
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        raise ArchiveRejected(f"Absolute path in archive: {name!r}")
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ArchiveRejected(f"Path traversal detected: {name!r}")
    return parts


def _validate(zf: zipfile.ZipFile, archive_size: int, limits: Limits):
    infos = zf.infolist()
    if len(infos) > limits.max_files:
        raise ArchiveRejected(f"Too many entries ({len(infos)} > {limits.max_files})")

    plan: list[tuple[zipfile.ZipInfo, list[str], str]] = []
    skipped: list[str] = []
    skipped_count = 0
    total = 0
    for info in infos:
        parts = _normalise(info.filename)
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ArchiveRejected(f"Symbolic link in archive: {info.filename!r}")
        if info.flag_bits & 0x1:
            raise ArchiveRejected(f"Encrypted entry not supported: {info.filename!r}")
        if info.is_dir() or not parts:
            continue
        total += info.file_size
        if total > limits.max_extracted_bytes:
            raise ArchiveRejected("Declared extracted size exceeds limit")
        if info.file_size >= MB:
            if info.compress_size == 0 or info.file_size / info.compress_size > limits.max_ratio:
                raise ArchiveRejected(f"Suspicious compression ratio: {info.filename!r}")
        junk = parts[0] == "__MACOSX" or parts[-1].startswith("._") or parts[-1] == ".DS_Store"
        kind = None if junk else classify(parts[-1])
        if kind is None:
            skipped_count += 1
            if len(skipped) < MAX_SKIPPED_LISTED:
                skipped.append("/".join(parts))
            continue
        plan.append((info, parts, kind))

    if total >= MB and total / max(archive_size, 1) > limits.max_ratio:
        raise ArchiveRejected("Overall compression ratio too high (possible zip bomb)")
    return plan, skipped, skipped_count


def extract_archive(zip_path: Path, dest_dir: Path, limits: Limits) -> ExtractionResult:
    zip_path, dest_dir = Path(zip_path), Path(dest_dir)
    size = zip_path.stat().st_size
    if size > limits.max_archive_bytes:
        raise ArchiveRejected(f"Archive too large ({size} bytes)")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            plan, skipped, skipped_count = _validate(zf, size, limits)
            needed = sum(i.file_size for i, _, _ in plan)
            dest_dir.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(dest_dir).free
            if free < needed + limits.min_free_bytes:
                raise InsufficientSpace(
                    f"Need ~{needed // MB} MB + reserve, only {free // MB} MB free"
                )
            result = ExtractionResult(skipped=skipped, skipped_count=skipped_count)
            _extract(zf, plan, dest_dir, limits, result)
            return result
    except zipfile.BadZipFile as e:
        raise ArchiveCorrupted(f"Bad ZIP file: {e}") from e
    except (zlib.error, EOFError, NotImplementedError, RuntimeError) as e:
        raise ArchiveCorrupted(f"Cannot read ZIP data: {type(e).__name__}: {e}") from e


def _extract(zf, plan, dest_dir: Path, limits: Limits, result: ExtractionResult) -> None:
    dest_resolved = dest_dir.resolve()
    for info, parts, kind in plan:
        folder = "images" if kind == "image" else "videos"
        target = _free_name(dest_dir.joinpath(folder, *parts))
        if not target.resolve().is_relative_to(dest_resolved):
            raise ArchiveRejected(f"Path escapes extraction dir: {info.filename!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        h = hashlib.sha256()
        written = 0
        try:
            with zf.open(info) as src, open(part, "wb") as out:
                while chunk := src.read(CHUNK):
                    written += len(chunk)
                    result.total_bytes += len(chunk)
                    if written > info.file_size + 1024:
                        raise ArchiveRejected(f"Entry larger than declared: {info.filename!r}")
                    if result.total_bytes > limits.max_extracted_bytes:
                        raise ArchiveRejected("Extracted size exceeds limit")
                    h.update(chunk)
                    out.write(chunk)
            part.replace(target)
        except BaseException:
            part.unlink(missing_ok=True)  # sirf adhoori .part temp file hatati hai
            raise
        result.files.append({
            "file_id": h.hexdigest(),
            "type": kind,
            "filename": target.name,
            "size": written,
            "relative_path": target.relative_to(dest_dir).as_posix(),
            "original_path_in_zip": "/".join(parts),
        })
