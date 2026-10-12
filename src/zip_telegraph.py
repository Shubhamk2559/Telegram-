"""Ek ZIP ka apna Telegraph article (sirf us ZIP ki images), duplicate-safe aur resumable.

- Title hamesha configured title (ZIP filename nahi).
- Pages ulte order me bante hain (har page ko agle ka URL pata ho); har page banne ke turant baad DB me save.
- createPage se pehle "intent" save hota hai. Response kho jaye to naya page banane se pehle account ke
  recent pages me wahi content dhoondha jata hai (image URLs ki list match) - tabhi duplicate nahi banta.
- 'verified' tab tak nahi jab tak telegraph_publisher.verify_page ne confirm na kiya ho.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Callable

from .db_stores import ArticleStore, DbImageStore
from .image_store import T_FAILED, T_UNVERIFIED, T_VERIFIED
from .manifest import utc_now
from .telegraph_publisher import (
    METHOD_EMBED, PAGE_BYTE_LIMIT, TelegraphAmbiguous, TelegraphClient, TelegraphError,
    image_figure, split_units, verify_page, walk_img_srcs,
)

ST_CREATING, ST_VERIFIED, ST_UNVERIFIED, ST_FAILURES = "creating", "verified", "created_unverified", "has_failures"
REUSABLE = (ST_VERIFIED, ST_UNVERIFIED)


class ZipArticleError(Exception):
    """Temporary failure: baad me dobara try kar sakte hain (state DB me safe hai)."""


def _plan(store: DbImageStore, method: str):
    units, src_by_id, ids = [], {}, []
    for rec in store.uploaded():
        try:
            node, dropped = image_figure(rec, method)
        except TelegraphError as e:
            store.update(rec, "telegraph", method=method, status=T_FAILED, error=str(e), page_url=None, checked_at=utc_now())
            continue
        store.update(rec, "telegraph", dropped_from_embed=dropped)
        src_by_id[rec["file_id"]] = walk_img_srcs(node)[0]
        units.append((node, rec["file_id"]))
        ids.append(rec["file_id"])
    return units, src_by_id, ids


def _fingerprint(title: str, method: str, limit: int, ids: list[str], src_by_id: dict) -> str:
    blob = json.dumps([title, method, limit, [[i, src_by_id[i]] for i in ids]], ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def recover_page(client: TelegraphClient, title: str, expected_srcs: list[str]) -> dict | None:
    """Kya ye page pehle hi ban chuka hai? Recent pages me title + image-URL list exact match dhoondo."""
    try:
        for p in client.get_page_list(20):
            if p.get("title") != title:
                continue
            content = client.get_page(p["path"]).get("content") or []
            if walk_img_srcs(content) == expected_srcs:
                return {"path": p["path"], "url": p["url"]}
    except TelegraphError:
        return None
    return None


def _create_page_safely(client: TelegraphClient, title: str, nodes: list, author: str, expected_srcs: list[str],
                        attempts: int, sleep: Callable[[float], None], may_exist: bool) -> dict:
    last = "unknown"
    for attempt in range(attempts):
        if may_exist or attempt:
            found = recover_page(client, title, expected_srcs)  # ambiguous result se pehle remote check
            if found:
                return found
        try:
            return client.create_page(title, nodes, author)
        except TelegraphAmbiguous as e:
            last, may_exist = str(e), True
            sleep(min(5 * 2 ** attempt, 120))
        except TelegraphError as e:
            m = re.search(r"FLOOD_WAIT_(\d+)", str(e))
            if not m or int(m.group(1)) > 300:
                raise  # definitive rejection (token invalid, content too big...) - nothing created
            last = str(e)
            sleep(int(m.group(1)) + 1)
    raise ZipArticleError(f"createPage {attempts} koshish ke baad bhi fail: {last}")


def publish_zip_article(*, articles: ArticleStore, store: DbImageStore, client: TelegraphClient, http,
                        archive_id: str, title: str, author: str = "", method: str = METHOD_EMBED,
                        limit: int = PAGE_BYTE_LIMIT, attempts: int = 4,
                        sleep: Callable[[float], None] = time.sleep) -> dict:
    """Article banao ya (pehle se bana ho to) wahi lautao. Returns article dict (urls, status, counts)."""
    units, src_by_id, ids = _plan(store, method)
    if not ids:
        raise ZipArticleError("Article ke liye koi uploaded image nahi hai.")
    chunks = split_units(units, limit)
    n = len(chunks)
    fp = _fingerprint(title, method, limit, ids, src_by_id)
    art = articles.get(archive_id)
    if art and art["fingerprint"] == fp and art["status"] in REUSABLE:
        return art  # pehle se bana hua article reuse - duplicate nahi
    if not art or art["fingerprint"] != fp:
        art = {"status": ST_CREATING, "fingerprint": fp, "title": title, "method": method,
               "total_pages": n, "pages": {}, "pending_index": None, "urls": []}
        articles.put(archive_id, art)

    orig_retries = client.max_retries
    client.max_retries = 0  # createPage ka internal retry band: ambiguous result hum khud reconcile karte hain
    try:
        next_url = None
        for idx in range(n - 1, -1, -1):
            if str(idx) in art["pages"]:
                next_url = art["pages"][str(idx)]["url"]
                continue
            chunk_ids = [u[1] for u in chunks[idx]]
            nodes = [u[0] for u in chunks[idx]]
            if next_url:
                nodes.append({"tag": "p", "children": [{"tag": "a", "attrs": {"href": next_url}, "children": ["Next page →"]}]})
            expected = [src_by_id[i] for i in chunk_ids]
            may_exist = art.get("pending_index") == idx  # pichli baar yahin crash hua tha
            art["pending_index"] = idx
            articles.put(archive_id, art)  # intent, API call se PEHLE
            try:
                page = _create_page_safely(client, title, nodes, author, expected, attempts, sleep, may_exist)
            except TelegraphAmbiguous as e:
                raise ZipArticleError(str(e)) from e
            except TelegraphError as e:
                raise ZipArticleError(f"Telegraph ne page reject kiya: {e}") from e
            art["pages"][str(idx)] = {"url": page["url"], "path": page["path"], "image_count": len(chunk_ids), "ids": chunk_ids}
            art["pending_index"] = None
            articles.put(archive_id, art)
            next_url = page["url"]
    finally:
        client.max_retries = orig_retries

    art["urls"] = [art["pages"][str(i)]["url"] for i in range(n)]
    cache: dict = {}
    counts = {T_VERIFIED: 0, T_UNVERIFIED: 0, T_FAILED: 0}
    for i in range(n):
        page = art["pages"][str(i)]
        results = verify_page(client, http, page["path"], {fid: src_by_id[fid] for fid in page["ids"]}, cache)
        for fid, (status, err) in results.items():
            store.update(store.get(fid), "telegraph", method=method, status=status, error=err,
                         page_url=page["url"], checked_at=utc_now())
            counts[status] += 1
    art["counts"] = counts
    art["status"] = ST_FAILURES if counts[T_FAILED] else (ST_UNVERIFIED if counts[T_UNVERIFIED] else ST_VERIFIED)
    articles.put(archive_id, art)
    return art
