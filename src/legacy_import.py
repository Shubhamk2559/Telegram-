"""Purane JSON manifests (manifest.json, image_manifest.json, video_uploads.json) ko database me import karna.

- JSON files sirf PADHI jaati hain (kabhi badli/delete nahi hoti).
- Repeatable: dobara chalane par kuch overwrite nahi hota (INSERT ... DO NOTHING). Successful ImgBB/Telegraph/DiskWala
  records jaise hain waise rehte hain.
- Safety: import hua ZIP jo poora complete dikhta hai -> COMPLETED (koi naya post auto-publish nahi hota).
  Jo adhura hai -> PAUSED (queue ruk jaati hai) taaki koi purana ZIP achanak auto-publish na ho; `resume <id>` se
  aage badhao.
"""
from __future__ import annotations

import json
from pathlib import Path

from .db import Database, now_iso

NOTE_DONE = "Legacy import: pehle se complete (post auto-generate nahi hua)."


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _article_from_images(imgs: list[dict]) -> dict | None:
    uploaded = [r for r in imgs if r["imgbb"]["status"] == "uploaded"]
    if not uploaded or any(not r["telegraph"].get("page_url") for r in uploaded):
        return None
    urls: list[str] = []
    for r in sorted(uploaded, key=lambda r: r.get("order", 0)):
        if r["telegraph"]["page_url"] not in urls:
            urls.append(r["telegraph"]["page_url"])
    verified = sum(r["telegraph"].get("status") == "embedded_verified" for r in uploaded)
    all_ok = all(r["telegraph"].get("status") in ("embedded_verified", "embedded_unverified") for r in uploaded)
    if not all_ok:
        return None
    return {"status": "verified" if verified == len(uploaded) else "created_unverified", "fingerprint": "legacy",
            "title": None, "method": uploaded[0]["telegraph"].get("method"), "total_pages": len(urls),
            "pages": {}, "pending_index": None, "urls": urls,
            "counts": {"embedded_verified": verified, "embedded_unverified": len(uploaded) - verified, "failed": 0}}


def import_legacy(db: Database, manifests_dir: Path) -> dict:
    d = Path(manifests_dir)
    manifest = _load(d / "manifest.json") or {"archives": []}
    images = (_load(d / "image_manifest.json") or {}).get("files", [])
    videos = (_load(d / "video_uploads.json") or {}).get("files", [])
    summary = {"archives": 0, "images": 0, "videos": 0, "articles": 0, "completed": 0, "paused": 0,
               "skipped_archives": 0, "orphan_images": 0, "orphan_videos": 0}
    known: set[str] = set()
    now = now_iso()
    with db.transaction():
        for a in manifest.get("archives", []):
            aid = a.get("archive_id", "")
            if a.get("status") != "completed" or ":" not in aid:
                summary["skipped_archives"] += 1  # extract hi nahi hua tha: queue naye sire se sambhalegi
                continue
            known.add(aid)
            chat_s, _, mid_s = aid.rpartition(":")
            chat_id, mid = int(a.get("channel_id") or chat_s), int(a.get("message_id") or mid_s)
            imgs = [r for r in images if r.get("archive_id") == aid]
            vids = [r for r in videos if r.get("archive_id") == aid]
            article = _article_from_images(imgs)
            imgs_done = all(r["imgbb"]["status"] == "uploaded" for r in imgs)
            vids_done = all(v["status"] == "completed" and v.get("url") for v in vids)
            complete = imgs_done and vids_done and (article is not None or not imgs)
            status = "COMPLETED" if complete else "PAUSED"
            reason = None if complete else "Legacy import: kaam adhura hai. Dekh ke `resume` karo (done stages skip honge)."
            extraction = {"archive_path": a.get("archive_path"), "dir": a.get("extraction_dir"),
                          "images": (a.get("counts") or {}).get("images", 0),
                          "videos": (a.get("counts") or {}).get("videos", 0), "legacy": True}
            n = db.execute(
                "INSERT INTO archives (archive_id, chat_id, message_id, filename, size, msg_date, status, stage, error, "
                "paused_reason, extraction, created_at, updated_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (archive_id) DO NOTHING",
                (aid, chat_id, mid, a.get("archive_filename", f"archive_{mid}.zip"), int(a.get("archive_size") or 0),
                 a.get("message_date"), status, "LEGACY", NOTE_DONE if complete else reason, reason,
                 json.dumps(extraction), now, now, now if complete else None))
            summary["archives"] += n
            summary["completed" if complete else "paused"] += n
            for r in imgs:
                summary["images"] += db.execute(
                    "INSERT INTO images (archive_id, file_id, ord, data, updated_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (archive_id, file_id) DO NOTHING", (aid, r["file_id"], int(r.get("order", 0)), json.dumps(r), now))
            for i, v in enumerate(vids):
                summary["videos"] += db.execute(
                    "INSERT INTO videos (archive_id, file_id, ord, data, updated_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (archive_id, file_id) DO NOTHING", (aid, v["file_id"], i, json.dumps(v), now))
            if article:
                summary["articles"] += db.execute(
                    "INSERT INTO articles (archive_id, data, updated_at) VALUES (?, ?, ?) ON CONFLICT (archive_id) DO NOTHING",
                    (aid, json.dumps(article), now))
        summary["orphan_images"] = sum(r.get("archive_id") not in known for r in images)
        summary["orphan_videos"] = sum(v.get("archive_id") not in known for v in videos)
        # Scanner ko batao ki yahan tak ka sab dekha ja chuka hai (purani history dobara queue na ho)
        by_chat: dict[int, int] = {}
        for a in manifest.get("archives", []):
            if ":" in a.get("archive_id", "") and a.get("status") == "completed":
                c = int(a.get("channel_id") or a["archive_id"].rpartition(":")[0])
                by_chat[c] = max(by_chat.get(c, 0), int(a.get("message_id") or 0))
        for c, top in by_chat.items():
            db.execute("INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO NOTHING",
                       (f"last_scanned:{c}", str(top), now))
    return summary
