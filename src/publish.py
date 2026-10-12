"""Part 3 ka flow: ImgBB upload -> Telegraph article -> final report. Part 1/2 ka code yahan nahi chhua gaya."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .image_store import (
    FAILED, T_FAILED, T_SKIPPED, T_UNVERIFIED, T_VERIFIED, UPLOADED, ImageStore,
)
from .imgbb_uploader import ImgBBClient, ImgBBError, ImgBBFatal
from .manifest import atomic_write_json, utc_now
from .telegraph_publisher import (
    METHOD_EMBED, TelegraphClient, TelegraphError, image_figure, split_units, verify_page, walk_img_srcs,
)
from .upload_store import COMPLETED, UploadStore


class PublishError(Exception):
    pass


def resolve_media_path(workspace: Path, rel: str) -> Path:
    ws = Path(workspace).resolve()
    p = (ws / rel).resolve()
    if ws not in p.parents:
        raise ImgBBError("File ka path workspace ke bahar hai; upload nahi karunga.")
    return p


# ------------------------------------------------------------------ ImgBB stage
@dataclass
class ImgBBRun:
    uploaded: int = 0
    failed: int = 0
    stopped_reason: str | None = None


def upload_images(store: ImageStore, client: ImgBBClient, work: list[dict], workspace: Path, *,
                  delay: float = 0.5, out: Callable[[str], None] = print,
                  sleep: Callable[[float], None] = time.sleep) -> ImgBBRun:
    run = ImgBBRun()
    for i, rec in enumerate(work, 1):
        label = f"[{i}/{len(work)}] {rec['filename']}"
        store.update(rec, "imgbb", attempts=rec["imgbb"]["attempts"] + 1)
        try:
            res = client.upload(resolve_media_path(workspace, rec["path"]), rec["filename"])
        except ImgBBFatal as e:
            run.stopped_reason = str(e)
            out(f"{label}: ROKA GAYA - {e}")
            break  # image 'pending' hi rehti hai, galti image ki nahi hai
        except ImgBBError as e:
            store.update(rec, "imgbb", status=FAILED, error=str(e))
            run.failed += 1
            out(f"{label}: FAILED - {e}")
        else:
            store.update(rec, "imgbb", status=UPLOADED, error=None, id=res["id"], url=res["url"],
                         display_url=res["display_url"], viewer_url=res["viewer_url"],
                         thumb_url=res["thumb_url"], medium_url=res["medium_url"],
                         delete_url=res["delete_url"], uploaded_at=utc_now())
            store.update(rec, "html_embed", constructed=res["html_embeds"])
            run.uploaded += 1
            out(f"{label}: OK")
        if i < len(work) and delay:
            sleep(delay)
    return run


# ------------------------------------------------------------------ Telegraph stage
def ensure_telegraph_token(client: TelegraphClient, env_token: str, account_path: Path,
                           author: str = "", out: Callable[[str], None] = print) -> None:
    if env_token:
        client.token = env_token
        return
    account_path = Path(account_path)
    if account_path.exists():
        try:
            token = json.loads(account_path.read_text(encoding="utf-8")).get("access_token")
        except (ValueError, OSError, AttributeError):
            token = None
        if token:
            client.token = token
            return
        raise PublishError(f"{account_path.name} padh nahi paya. Delete karke dobara chalao (naya account banega).")
    acct = client.create_account("gallery", author)
    atomic_write_json(account_path, {"access_token": acct["access_token"], "created_at": utc_now()})
    try:
        os.chmod(account_path, 0o600)
    except OSError:
        pass
    out("Naya Telegraph account bana. Token workspace/sessions/ me save hua (private rakho).")


def publish_article(store: ImageStore, client: TelegraphClient, http, *, title: str, author: str = "",
                    method: str = METHOD_EMBED, force: bool = False,
                    out: Callable[[str], None] = print) -> dict:
    existing = store.data.get("telegraph")
    if existing and not force:
        raise PublishError(f"Article pehle bana chuka hai: {existing.get('main_url')}. Naya banana ho to --force lagao.")
    imgs = store.ordered()
    uploaded = [r for r in imgs if r["imgbb"]["status"] == UPLOADED]
    if not uploaded:
        raise PublishError("ImgBB par koi image uploaded nahi hai. Pehle: python -m src.main imgbb-upload --run")

    for r in imgs:
        if r["imgbb"]["status"] != UPLOADED:
            store.update(r, "telegraph", method=method, status=T_SKIPPED, page_url=None,
                         error=f"ImgBB upload nahi hua (status: {r['imgbb']['status']}): {r['imgbb']['error'] or '-'}",
                         checked_at=utc_now())

    multi_archive = len({r.get("archive_id") for r in uploaded}) > 1
    units: list[tuple[dict, str | None]] = []
    src_by_id: dict[str, str] = {}
    last_arch = object()
    for r in uploaded:
        try:
            node, dropped = image_figure(r, method)
        except TelegraphError as e:
            store.update(r, "telegraph", method=method, status=T_FAILED, error=str(e), page_url=None,
                         checked_at=utc_now())
            continue
        if multi_archive and r.get("archive_id") != last_arch:
            units.append(({"tag": "h3", "children": [r.get("archive_filename") or str(r.get("archive_id"))]}, None))
            last_arch = r.get("archive_id")
        r["telegraph"]["dropped_from_embed"] = dropped
        src_by_id[r["file_id"]] = walk_img_srcs(node)[0]
        units.append((node, r["file_id"]))
    store.save()

    chunks = split_units(units)
    if not chunks:
        raise PublishError("Article me daalne layak koi image nahi bachi (details image_manifest.json me).")
    n = len(chunks)
    pages: list[dict | None] = [None] * n
    next_url = None
    cache: dict = {}
    for idx in range(n - 1, -1, -1):  # ulta: har page ko agle page ka URL pata ho
        chunk = chunks[idx]
        nodes = [u[0] for u in chunk]
        if next_url:
            nodes.append({"tag": "p", "children": [{"tag": "a", "attrs": {"href": next_url},
                                                    "children": ["Next page →"]}]})
        page_title = title if n == 1 else f"{title} (Part {idx + 1}/{n})"
        ids = [u[1] for u in chunk if u[1]]
        try:
            page = client.create_page(page_title, nodes, author)
        except TelegraphError as e:
            out(f"Page {idx + 1}/{n} FAIL: {e}")
            for fid in ids:
                store.update(store.get(fid), "telegraph", method=method, status=T_FAILED, error=str(e),
                             page_url=None, checked_at=utc_now())
            continue
        next_url = page["url"]
        pages[idx] = {"url": page["url"], "path": page["path"], "title": page_title, "image_count": len(ids)}
        out(f"Page {idx + 1}/{n} bana: {page['url']}")
        results = verify_page(client, http, page["path"], {fid: src_by_id[fid] for fid in ids}, cache)
        for fid, (status, err) in results.items():
            store.update(store.get(fid), "telegraph", method=method, status=status, error=err,
                         page_url=page["url"], checked_at=utc_now())

    made = [p for p in pages if p]
    store.data["telegraph"] = {
        "created_at": utc_now(), "method": method, "title": title,
        "main_url": pages[0]["url"] if pages[0] else None, "pages": made,
    }
    store.save()
    return store.data["telegraph"]


# ------------------------------------------------------------------ final report
def build_report(store: ImageStore, uploads: UploadStore) -> dict:
    imgs = store.ordered()
    tel = store.data.get("telegraph") or {}
    verified = [r for r in imgs if r["telegraph"]["status"] == T_VERIFIED]
    unverified = [r for r in imgs if r["telegraph"]["status"] == T_UNVERIFIED]
    problems = []
    for r in imgs:
        st = r["telegraph"]["status"]
        if st in (T_FAILED, T_SKIPPED, T_UNVERIFIED) or (st is None and r["imgbb"]["status"] != UPLOADED):
            problems.append({"filename": r["filename"], "status": st or "not_published",
                             "reason": r["telegraph"]["error"] or r["imgbb"]["error"] or "Telegraph step abhi chala nahi"})
        elif st is None:
            problems.append({"filename": r["filename"], "status": "not_published",
                             "reason": "Telegraph step abhi chala nahi"})
    links = [{"filename": f["filename"], "url": f["url"], "urls": f.get("urls") or [f["url"]],
              "needs_review": bool(f.get("needs_review"))}
             for f in uploads.files if f["status"] == COMPLETED and f.get("url")]
    return {
        "generated_at": utc_now(),
        "telegraph_article_url": tel.get("main_url"),
        "telegraph_pages": [p["url"] for p in tel.get("pages", [])],
        "diskwala_links": links,
        "diskwala_not_completed": {k: v for k, v in uploads.counts().items() if k != COMPLETED},
        "images_total": len(imgs),
        "images_uploaded_to_imgbb": sum(r["imgbb"]["status"] == UPLOADED for r in imgs),
        "images_embedded_verified": len(verified),
        "images_embedded_unverified": len(unverified),
        "images_not_embedded": problems,
    }


def format_report(rep: dict) -> str:
    L = ["", "===== FINAL REPORT =====", ""]
    L.append("1) Telegraph article:")
    if rep["telegraph_article_url"]:
        L += [f"   {u}" for u in rep["telegraph_pages"]]
    else:
        L.append("   (abhi nahi bana)")
    L += ["", f"2) DiskWala video links ({len(rep['diskwala_links'])}):"]
    for d in rep["diskwala_links"]:
        flag = "  [needs_review]" if d["needs_review"] else ""
        L.append(f"   {d['filename']}: {d['url']}{flag}")
    if not rep["diskwala_links"]:
        L.append("   (koi completed link nahi)")
    if rep["diskwala_not_completed"]:
        L.append(f"   Baaki videos (completed nahi): {rep['diskwala_not_completed']}")
    L += ["", f"3) ImgBB par upload hui images: {rep['images_uploaded_to_imgbb']} / {rep['images_total']}",
          f"4) Telegraph me embed (verified): {rep['images_embedded_verified']}"
          + (f"   (+{rep['images_embedded_unverified']} article me hain par verify nahi hui)"
             if rep["images_embedded_unverified"] else ""),
          "", "5) Jo images embed nahi hui / verify nahi hui:"]
    for p in rep["images_not_embedded"]:
        L.append(f"   - {p['filename']} [{p['status']}]: {p['reason']}")
    if not rep["images_not_embedded"]:
        L.append("   (koi nahi)")
    L += ["", "Note: 'verified' = Telegraph ke stored content + page HTML me <img> mila aur image URL asli image deta hai.",
          "Browser me ek baar link khol ke dekh lo."]
    return "\n".join(L)
