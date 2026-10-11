"""Final Telegram posts: video sorting, template, 8-link splitting.

Pure logic - na network, na database. Isliye poori tarah testable hai.
"""
from __future__ import annotations

import re
import string
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_TEMPLATE = "Articles Images {telegraph_url}\n\nVideo links {video_links}"
DEFAULT_IMAGES_ONLY_TEMPLATE = "Articles Images {telegraph_url}"
DEFAULT_VIDEOS_ONLY_TEMPLATE = "Video links {video_links}"
VARS_FULL = frozenset({"telegraph_url", "telegraph_urls", "video_links"})
VARS_IMAGES_ONLY = frozenset({"telegraph_url", "telegraph_urls"})
VARS_VIDEOS_ONLY = frozenset({"video_links"})
TELEGRAM_LIMIT = 4096  # characters (UTF-16 units me ginte hain, emoji ke liye conservative)

KIND_FULL, KIND_IMAGES_ONLY, KIND_VIDEOS_ONLY = "full", "images_only", "videos_only"


class TemplateError(ValueError):
    pass


class PostError(ValueError):
    pass


# ---------------------------------------------------------------- videos
def sort_videos(videos: list[dict]) -> list[dict]:
    """Size ke hisaab se bada pehle. Barabar size par filename (case-insensitive, phir exact) tie-breaker."""
    return sorted(videos, key=lambda v: (-int(v["size"]), v["filename"].lower(), v["filename"]))


def with_actual_sizes(videos: list[dict], base_dir: Path) -> list[dict]:
    """Manifest ke size par bharosa nahi: disk se asli size naap ke copies lautao."""
    base = Path(base_dir).resolve()
    out = []
    for v in videos:
        p = (base / v["path"]).resolve()
        if base not in p.parents:
            raise PostError(f"Video path base folder ke bahar hai: {v['path']}")
        try:
            size = p.stat().st_size
        except OSError as e:
            raise PostError(f"Video disk par nahi mili: {v['path']} ({e})") from e
        out.append({**v, "size": size})
    return out


# ---------------------------------------------------------------- template
_ESC = {"n": "\n", "t": "\t", "\\": "\\"}


def unescape_template(raw: str) -> str:
    """.env me likhe literal \\n, \\t, \\\\ ko asli characters me badlo."""
    return re.sub(r"\\([nt\\])", lambda m: _ESC[m.group(1)], raw)


def validate_template(template: str, allowed: frozenset, require_any: frozenset | None = None) -> None:
    """Sirf {name} placeholders; unknown/format-spec/attribute/index/positional sab reject."""
    used: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as e:
        raise TemplateError(f"Template ke braces galat hain: {e}") from e
    for _lit, field, spec, conv in parsed:
        if field is None:
            continue
        if conv or spec:
            raise TemplateError(f"Placeholder {{{field}}} me format spec/conversion allowed nahi.")
        if not re.fullmatch(r"[A-Za-z_]\w*", field):
            raise TemplateError(f"Placeholder '{{{field}}}' allowed nahi (sirf simple naam).")
        if field not in allowed:
            raise TemplateError(f"Unknown variable {{{field}}}. Allowed: {', '.join('{' + a + '}' for a in sorted(allowed))}")
        used.add(field)
    if require_any and not (used & require_any):
        raise TemplateError("Template me zaroori variable nahi hai: " + " ya ".join("{" + a + "}" for a in sorted(require_any)))


def check_url(u: str, what: str) -> str:
    p = urlparse(u or "")
    if p.scheme not in ("http", "https") or not p.hostname or re.search(r"\s", u):
        raise PostError(f"{what} valid http(s) URL nahi hai: {u!r}")
    return u


def format_links(links: list[str]) -> str:
    """Har link alag line par, do links ke beech theek ek blank line."""
    return "\n\n".join(links)


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class PostPlan:
    seq: int
    kind: str
    text: str
    link_start: int  # video links ka 0-based start index (images-only me 0)
    link_count: int


def plan_posts(*, video_links: list[str], telegraph_urls: list[str],
               template: str = DEFAULT_TEMPLATE,
               images_only_template: str = DEFAULT_IMAGES_ONLY_TEMPLATE,
               videos_only_template: str = DEFAULT_VIDEOS_ONLY_TEMPLATE,
               max_links: int = 8, max_chars: int = TELEGRAM_LIMIT) -> list[PostPlan]:
    """Ek ZIP ke links -> ordered posts. Links ka order kabhi nahi badalta, har post me <= max_links.

    - video + article dono: `template`
    - sirf images (article, koi video link nahi): `images_only_template`, ek post
    - sirf videos (article nahi): `videos_only_template`
    - dono khaali: PostError (khaali post publish nahi hota)
    Post lamba ho jaye (> max_chars) to us post me links kam kiye jaate hain, order wahi rehta hai.
    """
    if max_links < 1:
        raise PostError("max_links kam se kam 1 hona chahiye.")
    links = [check_url(u, f"Video link #{i + 1}") for i, u in enumerate(video_links)]
    pages = [check_url(u, f"Telegraph URL #{i + 1}") for i, u in enumerate(telegraph_urls)]
    if not links and not pages:
        raise PostError("Na video link hain na Telegraph article - khaali post nahi banega.")

    if not links:
        validate_template(images_only_template, VARS_IMAGES_ONLY, VARS_IMAGES_ONLY)
        text = images_only_template.format_map({"telegraph_url": pages[0], "telegraph_urls": "\n".join(pages)})
        _check_len(text, max_chars, "Images-only post")
        return [PostPlan(1, KIND_IMAGES_ONLY, text, 0, 0)]

    if pages:
        validate_template(template, VARS_FULL, frozenset({"video_links"}))
        tpl, kind = template, KIND_FULL
    else:
        validate_template(videos_only_template, VARS_VIDEOS_ONLY, VARS_VIDEOS_ONLY)
        tpl, kind = videos_only_template, KIND_VIDEOS_ONLY

    def render(chunk: list[str]) -> str:
        return tpl.format_map({"telegraph_url": pages[0] if pages else "",
                               "telegraph_urls": "\n".join(pages),
                               "video_links": format_links(chunk)})

    plans: list[PostPlan] = []
    i = 0
    while i < len(links):
        take = min(max_links, len(links) - i)
        while take >= 1:
            text = render(links[i:i + take])
            if utf16_len(text) <= max_chars:
                break
            take -= 1
        else:
            raise PostError(f"Video link #{i + 1} ke saath bhi post {max_chars} characters se bada hai "
                            "(template/URL chhota karo).")
        plans.append(PostPlan(len(plans) + 1, kind, text, i, take))
        i += take
    return plans


def _check_len(text: str, max_chars: int, what: str) -> None:
    if utf16_len(text) > max_chars:
        raise PostError(f"{what} {max_chars} characters se bada hai.")
