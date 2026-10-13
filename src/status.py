"""Live progress state + Telegram card renderer (HTML). Pure functions: bina Telegram ke test ho sakte hain."""
from __future__ import annotations

import html
import time
from collections import deque
from dataclasses import dataclass, field

from .jobs import (
    CREATING_TELEGRAPH, DOWNLOADING, EXTRACTING, GENERATING_POSTS, PIPELINE, PUBLISHING_POSTS, UPLOADING_IMAGES,
    UPLOADING_VIDEOS,
)

STAGE_LABEL = {
    DOWNLOADING: ("⬇️", "Download"), EXTRACTING: ("📂", "Extract"), UPLOADING_IMAGES: ("🖼", "Images → ImgBB"),
    CREATING_TELEGRAPH: ("📝", "Telegraph article"), UPLOADING_VIDEOS: ("🎬", "Videos → DiskWala"),
    GENERATING_POSTS: ("🧾", "Posts banana"), PUBLISHING_POSTS: ("📣", "Publish"),
}


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def bar(pct: float, width: int = 10) -> str:
    pct = max(0.0, min(100.0, pct))
    full = int(round(pct / 100 * width))
    return "▰" * full + "▱" * (width - full)


def fmt_size(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


@dataclass
class LiveStatus:
    """Worker ke callbacks yahan likhte hain; bot card padhta hai. Sab in-memory (restart par reset, DB me nahi)."""
    aid: str | None = None
    started: float = 0.0
    dl_done: int = 0
    dl_total: int = 0
    _dl_samples: deque = field(default_factory=lambda: deque(maxlen=12))
    vid_i: int = 0
    vid_n: int = 0
    vid_name: str = ""
    vid_done: int = 0
    vid_total: int = 0
    note: str = ""

    def reset(self, aid: str | None) -> None:
        self.aid = aid
        self.started = time.monotonic() if aid else 0.0
        self.dl_done = self.dl_total = 0
        self._dl_samples.clear()
        self.vid_i = self.vid_n = self.vid_done = self.vid_total = 0
        self.vid_name = self.note = ""

    # ---- download (Telethon progress_callback(done, total)) ----
    def on_download(self, done: int, total: int) -> None:
        self.dl_done, self.dl_total = int(done or 0), int(total or 0)
        self._dl_samples.append((time.monotonic(), self.dl_done))

    def dl_speed(self) -> float:
        s = self._dl_samples
        if len(s) < 2 or s[-1][0] - s[0][0] < 1:
            return 0.0
        return max(0.0, (s[-1][1] - s[0][1]) / (s[-1][0] - s[0][0]))

    # ---- video uploader UI (DiskWalaUploader ui interface) ----
    def start(self, i, n, rec) -> None:
        self.vid_i, self.vid_n, self.vid_name = i, n, str(rec.get("filename", ""))
        self.vid_done = self.vid_total = 0
        self.note = ""

    def status(self, msg) -> None:
        self.note = str(msg)[:120]

    def progress(self, done, total) -> None:
        self.vid_done, self.vid_total = int(done or 0), int(total or 0)

    def result(self, rec) -> None:
        self.vid_done = self.vid_total = 0


def _stage_lines(current: str | None) -> str:
    idx = PIPELINE.index(current) if current in PIPELINE else -1
    out = []
    for i, st in enumerate(PIPELINE):
        icon, label = STAGE_LABEL[st]
        mark = "✅" if (idx > i) else ("🔄" if idx == i else "⬜")
        out.append(f"{mark} {label}" if mark != "🔄" else f"{mark} <b>{label}</b>")
    return "\n".join(out)


def render_job(job: dict, live: LiveStatus, queue_waiting: int = 0) -> str:
    """Chal rahi job ka card."""
    st = job["status"]
    icon, label = STAGE_LABEL.get(st, ("⏳", st.title()))
    prog = job.get("progress") or {}
    lines = [f"📦 <b>{esc(job['filename'])}</b>", f"<code>{esc(job['archive_id'])}</code>", "",
             f"{icon} <b>{esc(label)}</b>"]
    if st == DOWNLOADING and live.dl_total:
        pct = live.dl_done * 100 / live.dl_total
        sp = live.dl_speed()
        eta = f" · ETA {fmt_dur((live.dl_total - live.dl_done) / sp)}" if sp > 0 else ""
        lines += [f"<code>{bar(pct)} {pct:.0f}%</code>",
                  f"{fmt_size(live.dl_done)} / {fmt_size(live.dl_total)}" + (f" · {fmt_size(sp)}/s" if sp else "") + eta]
    elif st == DOWNLOADING:
        lines.append("<code>▱▱▱▱▱▱▱▱▱▱</code> shuru ho raha hai…")
    lines += ["", _stage_lines(st), ""]
    it, iu = prog.get("images_total"), prog.get("images_uploaded")
    if it:
        lines.append(f"🖼 Images <code>{bar(iu * 100 / it)} {iu}/{it}</code>")
    if st == UPLOADING_VIDEOS or prog.get("videos_total"):
        vt = prog.get("videos_total") or live.vid_n
        vd = prog.get("videos_done", 0)
        if live.vid_n and st == UPLOADING_VIDEOS:
            vd = max(vd, live.vid_i - 1)
        if vt:
            lines.append(f"🎬 Videos <code>{bar(vd * 100 / vt)} {vd}/{vt}</code>")
        if st == UPLOADING_VIDEOS and live.vid_name:
            fp = f" {live.vid_done * 100 / live.vid_total:.0f}%" if live.vid_total else ""
            lines.append(f"   ↳ <code>{esc(live.vid_name)}</code>{fp}")
            if live.vid_total:
                lines.append(f"   <code>{bar(live.vid_done * 100 / live.vid_total)}</code> "
                             f"{fmt_size(live.vid_done)} / {fmt_size(live.vid_total)}")
        if live.note and st == UPLOADING_VIDEOS:
            lines.append(f"   💬 {esc(live.note)}")
    foot = []
    if live.started:
        foot.append(f"⏱ {fmt_dur(time.monotonic() - live.started)}")
    if queue_waiting:
        foot.append(f"queue me {queue_waiting} aur")
    if job.get("retry_count"):
        foot.append(f"retries {job['retry_count']}")
    if foot:
        lines.append(" · ".join(foot))
    return "\n".join(lines)


def render_paused(job: dict) -> str:
    return (f"⏸ <b>Queue ruki hai</b>\n📦 <b>{esc(job['filename'])}</b>\n<code>{esc(job['archive_id'])}</code>\n\n"
            f"Wajah: {esc((job.get('paused_reason') or '')[:600])}\n\nTheek karke ▶️ Resume dabao.")


def render_idle(counts: dict) -> str:
    done, failed = counts.get("COMPLETED", 0), counts.get("FAILED", 0)
    return f"😴 <b>Koi task nahi chal raha</b>\nDone: {done} · Failed/Cancelled: {failed}\nNaya ZIP channel me aate hi apne aap uthega."
