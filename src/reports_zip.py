"""Per-ZIP aur overall reports (database se)."""
from __future__ import annotations

import json

from .db import Database
from .jobs import JobRepo


def zip_data(db: Database, archive_id: str) -> dict | None:
    job = JobRepo(db).get(archive_id)
    if not job:
        return None
    imgs = [json.loads(r["data"]) for r in db.query("SELECT data FROM images WHERE archive_id = ? ORDER BY ord", (archive_id,))]
    vids = [json.loads(r["data"]) for r in db.query("SELECT data FROM videos WHERE archive_id = ? ORDER BY ord", (archive_id,))]
    art = db.query_one("SELECT data FROM articles WHERE archive_id = ?", (archive_id,))
    posts = db.query("SELECT * FROM posts WHERE archive_id = ? ORDER BY seq", (archive_id,))
    return {"job": job, "images": imgs, "videos": vids, "article": json.loads(art["data"]) if art else None, "posts": posts}


def _range(p: dict) -> str:
    if not p["link_count"]:
        return "Telegraph only"
    a, b = p["link_start"] + 1, p["link_start"] + p["link_count"]
    return f"Video link {a}" if a == b else f"Video links {a}–{b}"


def zip_report(db: Database, archive_id: str) -> str:
    d = zip_data(db, archive_id)
    if d is None:
        return f"Unknown archive: {archive_id}"
    job, imgs, vids, art, posts = d["job"], d["images"], d["videos"], d["article"], d["posts"]
    up_i = sum(r["imgbb"]["status"] == "uploaded" for r in imgs)
    done_v = [v for v in vids if v["status"] == "completed" and v.get("url")]
    lines = ["===== ZIP PROCESSING REPORT =====", "", f"ZIP: {job['filename']}", f"Archive ID: {archive_id}", "",
             f"Images found: {len(imgs)}", f"Images uploaded: {up_i}", "", "Telegraph:"]
    lines += (art or {}).get("urls") or ["(nahi bana)"]
    if art:
        lines.append(f"(status: {art['status']})")
    lines += ["", f"Videos found: {len(vids)}", f"Videos uploaded: {len(done_v)}", "", "DiskWala links:", str(len(done_v)),
              "", "Final Telegram posts:", str(len(posts))]
    for p in posts:
        tag = f"published (msg {p['message_id']})" if p["status"] == "published" else p["status"]
        lines += ["", f"Post {p['seq']}:", f"{_range(p)} [{tag}]"]
    lines += ["", "Status:", job["status"]]
    if job.get("paused_reason") or (job["status"] == "FAILED" and job.get("error")):
        lines += ["", f"Reason: {job.get('paused_reason') or job['error']}"]
    problems = [f"- {r['filename']}: {r['imgbb']['error'] or r['telegraph']['error']}" for r in imgs
                if r["imgbb"]["status"] != "uploaded" or r["telegraph"].get("status") in ("failed", "skipped", "embedded_unverified")]
    if problems:
        lines += ["", "Image problems:"] + problems
    review = [v["filename"] for v in vids if v.get("needs_review")]
    if review:
        lines += ["", "Needs review: " + ", ".join(review)]
    return "\n".join(lines)


def overall_report(db: Database) -> str:
    o = JobRepo(db).overall()
    return "\n".join(["===== OVERALL REPORT =====", "", f"Total ZIP files: {o['total']}", "", f"Completed: {o['completed']}",
                      f"Processing: {o['processing']}", f"Pending: {o['pending']}", f"Paused: {o['paused']}", f"Failed: {o['failed']}"])
