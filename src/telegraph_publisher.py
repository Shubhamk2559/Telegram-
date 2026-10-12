"""Telegraph publishing (official docs: https://telegra.ph/api).

Docs se mile facts (investigation result):
- createPage `content` = JSON ARRAY OF NODES (max 64 KB). Arbitrary HTML string accept NAHI hota.
- NodeElement tags: a, aside, b, blockquote, br, code, em, figcaption, figure, h3, h4, hr, i,
  iframe, img, li, ol, p, pre, s, strong, u, ul, video.
- NodeElement attributes: sirf `href` aur `src`. Baaki sab (alt, border, style, class...) drop hote hain.
- `img` tag allowed hai, docs me nahi likha ki src sirf Telegraph ka hona chahiye; external URL
  chalega ya nahi ye docs se confirm nahi hota => publish ke baad verify_page() se check karte hain.
- `iframe` tag list me hai, par docs ye nahi batate ki kaunsa src accept/render hota hai. Isliye
  hum iframe emit hi nahi karte (ImgBB ke snippets me iframe hota bhi nahi).

Isliye ImgBB ka HTML snippet parse karke supported Nodes me convert hota hai (html_to_nodes).
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Callable

from .http_util import Http, NetworkError
from .image_store import T_FAILED, T_UNVERIFIED, T_VERIFIED

API = "https://api.telegra.ph"
SITE = "https://telegra.ph"
PAGE_BYTE_LIMIT = 60_000  # docs: 64 KB; thoda margin
ALLOWED_TAGS = frozenset({
    "a", "aside", "b", "blockquote", "br", "code", "em", "figcaption", "figure", "h3", "h4",
    "hr", "i", "img", "li", "ol", "p", "pre", "s", "strong", "u", "ul",
})  # iframe/video jaanbujhkar nahi (upar dekho)
ALLOWED_ATTRS = frozenset({"href", "src"})
VOID_TAGS = frozenset({"img", "br", "hr"})

METHOD_EMBED = "html_embed_converted_to_nodes"
METHOD_PLAIN = "plain_img_node"
METHODS = (METHOD_EMBED, METHOD_PLAIN)


class TelegraphError(Exception):
    pass


class TelegraphAmbiguous(TelegraphError):
    """Request gayi par result pata nahi (network/5xx/kharab JSON): page ban chuka ho sakta hai.
    Retry se pehle remote state check karo, warna duplicate page banega."""


# ---------------------------------------------------------------- HTML -> Nodes
def _safe_url(value: str) -> bool:
    return urllib.parse.urlparse(value.strip()).scheme in ("http", "https")


class _NodeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root: list = []
        self.stack: list = []  # (tag, node|None, children)
        self.dropped: list[str] = []

    def _children(self) -> list:
        return self.stack[-1][2] if self.stack else self.root

    def _make(self, tag, attrs):
        node = {"tag": tag}
        kept = {}
        for name, value in attrs:
            if name in ALLOWED_ATTRS and value:
                if _safe_url(value):
                    kept[name] = value
                else:
                    self._drop(f"attr:{name}(unsafe_url)")
            else:
                self._drop(f"attr:{name}")
        if kept:
            node["attrs"] = kept
        return node

    def _drop(self, what: str) -> None:
        if what not in self.dropped:
            self.dropped.append(what)

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self._drop(f"tag:{tag}")
            self.stack.append((tag, None, self._children()))  # bachhe parent me chale jaate hain
            return
        node = self._make(tag, attrs)
        if tag == "img" and "src" not in node.get("attrs", {}):
            self._drop("tag:img(no_valid_src)")
            return
        self._children().append(node)
        if tag not in VOID_TAGS:
            node["children"] = []
            self.stack.append((tag, node, node["children"]))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data.strip():
            self._children().append(data)


def _prune(nodes: list) -> list:
    for n in nodes:
        if isinstance(n, dict) and "children" in n:
            n["children"] = _prune(n["children"])
            if not n["children"]:
                del n["children"]
    return nodes


def html_to_nodes(html_text: str) -> tuple[list, list[str]]:
    """HTML snippet -> (Telegraph Nodes, drop ki gayi cheezon ki list)."""
    b = _NodeBuilder()
    b.feed(html_text)
    b.close()
    return _prune(b.root), b.dropped


def walk_img_srcs(nodes) -> list[str]:
    out: list[str] = []

    def rec(n):
        if isinstance(n, dict):
            if n.get("tag") == "img" and (n.get("attrs") or {}).get("src"):
                out.append(n["attrs"]["src"])
            for c in n.get("children", []):
                rec(c)
        elif isinstance(n, list):
            for c in n:
                rec(c)

    rec(nodes)
    return out


# ---------------------------------------------------------------- article banana
def image_figure(rec: dict, method: str) -> tuple[dict, list[str]]:
    """Ek image ke liye <figure> node. Returns (node, dropped_from_embed)."""
    if method == METHOD_EMBED:
        embed = ((rec.get("html_embed") or {}).get("constructed") or {}).get("full_linked")
        if not embed:
            raise TelegraphError("html_embed.constructed.full_linked missing")
        nodes, dropped = html_to_nodes(embed)
        if not walk_img_srcs(nodes):
            raise TelegraphError("HTML embed se koi <img src> nahi nikla")
        return {"tag": "figure", "children": nodes}, dropped
    if method == METHOD_PLAIN:
        url = rec["imgbb"]["url"]
        return {"tag": "figure", "children": [{"tag": "img", "attrs": {"src": url}}]}, []
    raise TelegraphError(f"Unknown method: {method}")


def _size(node) -> int:
    return len(json.dumps(node, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1


def split_units(units: list[tuple[dict, str | None]], limit: int = PAGE_BYTE_LIMIT) -> list[list]:
    """(node, file_id|None) units ko 64 KB se chhote pages me todo. Heading akeli page ke end me nahi rehti."""
    pages: list[list] = []
    cur: list = []
    size = 2
    for unit in units:
        s = _size(unit[0])
        if cur and size + s > limit:
            carry = []
            if cur[-1][1] is None:  # heading
                carry = [cur.pop()]
            pages.append(cur)
            cur, size = carry, 2 + sum(_size(u[0]) for u in carry)
        cur.append(unit)
        size += s
    if cur:
        pages.append(cur)
    return pages


# ---------------------------------------------------------------- API client
class TelegraphClient:
    def __init__(self, http: Http, token: str | None = None, *, max_retries: int = 3,
                 sleep: Callable[[float], None] = time.sleep, max_flood_wait: int = 120):
        self.http = http
        self.token = token
        self.max_retries = max_retries
        self.sleep = sleep
        self.max_flood_wait = max_flood_wait

    def _redact(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    def _call(self, method: str, params: dict, *, path: str = "", get: bool = False):
        url = f"{API}/{method}" + (f"/{urllib.parse.quote(path, safe='-/')}" if path else "")
        body = urllib.parse.urlencode(params).encode("utf-8")
        last = "unknown error"
        for attempt in range(self.max_retries + 1):
            if attempt:
                self.sleep(min(2 ** attempt, 30))
            try:
                if get:
                    resp = self.http.request("GET", f"{url}?{body.decode()}" if params else url, timeout=60)
                else:
                    resp = self.http.request("POST", url, timeout=60, data=body,
                                             headers={"Content-Type": "application/x-www-form-urlencoded"})
            except NetworkError as e:
                last = self._redact(str(e))
                continue
            if resp.status >= 500:
                last = f"HTTP {resp.status}"
                continue
            try:
                payload = json.loads(resp.text())
            except ValueError as e:
                raise TelegraphAmbiguous(f"{method}: valid JSON nahi mila (HTTP {resp.status})") from e
            if payload.get("ok") is True:
                return payload.get("result")
            err = str(payload.get("error", "unknown"))
            m = re.match(r"FLOOD_WAIT_(\d+)", err)
            if m:
                wait = int(m.group(1))
                if wait <= self.max_flood_wait and attempt < self.max_retries:
                    self.sleep(wait)
                    last = err
                    continue
                raise TelegraphError(f"{method}: {err} (rate limit; {wait}s baad dobara chalao)")
            raise TelegraphError(f"{method}: {self._redact(err)}")
        raise TelegraphAmbiguous(f"{method}: retries ke baad bhi fail: {last}")

    def create_account(self, short_name: str, author_name: str = "") -> dict:
        params = {"short_name": short_name[:32] or "gallery"}
        if author_name:
            params["author_name"] = author_name[:128]
        result = self._call("createAccount", params)
        if not (isinstance(result, dict) and result.get("access_token")):
            raise TelegraphError("createAccount ne access_token nahi diya")
        self.token = result["access_token"]
        return result

    def create_page(self, title: str, nodes: list, author_name: str = "", author_url: str = "") -> dict:
        if not self.token:
            raise TelegraphError("Telegraph access token nahi hai")
        content = json.dumps(nodes, ensure_ascii=False, separators=(",", ":"))
        if len(content.encode("utf-8")) > 64 * 1024:
            raise TelegraphError("Page content 64 KB se bada hai (split bug)")
        params = {"access_token": self.token, "title": title[:256] or "Gallery", "content": content,
                  "return_content": "false"}
        if author_name:
            params["author_name"] = author_name[:128]
        if author_url:
            params["author_url"] = author_url[:512]
        result = self._call("createPage", params)
        if not (isinstance(result, dict) and result.get("url") and result.get("path")):
            raise TelegraphError("createPage ne url/path nahi diya")
        return result

    def get_page_list(self, limit: int = 20) -> list[dict]:
        if not self.token:
            raise TelegraphError("Telegraph access token nahi hai")
        result = self._call("getPageList", {"access_token": self.token, "limit": str(limit)})
        return list(result.get("pages", [])) if isinstance(result, dict) else []

    def get_page(self, path: str) -> dict:
        result = self._call("getPage", {"return_content": "true"}, path=path, get=True)
        if not isinstance(result, dict):
            raise TelegraphError("getPage ka result samajh nahi aaya")
        return result

    def fetch_page_html(self, path: str) -> str:
        url = f"{SITE}/{urllib.parse.quote(path, safe='-/')}"
        try:
            resp = self.http.request("GET", url, timeout=60)
        except NetworkError as e:
            raise TelegraphError(f"page fetch: {e}") from e
        if resp.status != 200:
            raise TelegraphError(f"page fetch: HTTP {resp.status}")
        return resp.text()


# ---------------------------------------------------------------- verification
class _ImgSrcCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.srcs: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            src = dict(attrs).get("src")
            if src:
                self.srcs.add(src)

    handle_startendtag = handle_starttag


def html_img_srcs(html_text: str) -> set[str]:
    c = _ImgSrcCollector()
    c.feed(html_text)
    c.close()
    return c.srcs


def check_image_url(http: Http, url: str, cache: dict) -> tuple[bool, str]:
    """Kya ye URL sach me image bytes serve karta hai? (sirf pehle 2 KB padhta hai)"""
    if url in cache:
        return cache[url]
    try:
        r = http.request("GET", url, headers={"Range": "bytes=0-2047"}, timeout=60, max_bytes=4096)
        ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if r.status not in (200, 206):
            res = (False, f"HTTP {r.status}")
        elif not ctype.startswith("image/"):
            res = (False, f"content-type image nahi hai ({ctype or 'missing'})")
        elif not r.body:
            res = (False, "khaali body")
        else:
            res = (True, "")
    except NetworkError as e:
        res = (False, f"network: {e}")
    cache[url] = res
    return res


def verify_page(client: TelegraphClient, http: Http, path: str, expected: dict[str, str],
                cache: dict | None = None) -> dict[str, tuple[str, str | None]]:
    """expected = {file_id: img_src}. Returns {file_id: (status, error|None)}.

    VERIFIED tabhi jab (1) getPage ke stored content me src hai, (2) rendered page HTML me <img src>
    hai, (3) image URL ek asli image serve karta hai. Ye server-side checks hain; browser me pixels
    dekhna alag baat hai (link khol ke ek baar dekh lo).
    """
    cache = cache if cache is not None else {}
    stored: set[str] | None
    rendered: set[str] | None
    stored_err = rendered_err = ""
    try:
        stored = set(walk_img_srcs(client.get_page(path).get("content") or []))
    except TelegraphError as e:
        stored, stored_err = None, str(e)
    try:
        rendered = html_img_srcs(client.fetch_page_html(path))
    except TelegraphError as e:
        rendered, rendered_err = None, str(e)

    out: dict[str, tuple[str, str | None]] = {}
    for fid, src in expected.items():
        if stored is not None and src not in stored:
            out[fid] = (T_FAILED, "Article ke stored content me ye image nahi mili (Telegraph ne hata di).")
            continue
        ok, why = check_image_url(http, src, cache)
        if not ok:
            out[fid] = (T_FAILED, f"Image URL image serve nahi kar raha: {why}")
        elif stored is None:
            out[fid] = (T_UNVERIFIED, f"getPage se content verify nahi ho paya: {stored_err}")
        elif rendered is None:
            out[fid] = (T_UNVERIFIED, f"Page HTML fetch nahi ho paya: {rendered_err}")
        elif src not in rendered:
            out[fid] = (T_UNVERIFIED, "Stored content me hai, par render hue HTML me exact <img src> nahi mila.")
        else:
            out[fid] = (T_VERIFIED, None)
    return out
