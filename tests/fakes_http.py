"""Fake HTTP transport: asli ImgBB / Telegraph se baat kiye bina tests."""
import json
from urllib.parse import parse_qs

from src.http_util import NetworkError, Response


class FakeHttp:
    def __init__(self):
        self.calls = []      # (method, url, headers, data)
        self.handlers = []   # (method, url_prefix, fn)

    def on(self, method, prefix, fn):
        self.handlers.insert(0, (method, prefix, fn))  # naya handler pehle match hota hai

    def queue(self, method, prefix, items):
        items = list(items)
        self.on(method, prefix, lambda url, h, d: items.pop(0))

    def request(self, method, url, *, headers=None, data=None, timeout=60, max_bytes=None):
        self.calls.append((method, url, headers or {}, data))
        for m, pref, fn in self.handlers:
            if m == method and url.startswith(pref):
                r = fn(url, headers or {}, data)
                if isinstance(r, Exception):
                    raise r
                return r
        raise AssertionError(f"unexpected request: {method} {url}")


def jresp(obj, status=200, headers=None):
    return Response(status, headers or {}, json.dumps(obj).encode())


def imgbb_ok(name="a.jpg", n=1):
    return jresp({
        "data": {
            "id": f"id{n}", "title": name.rsplit(".", 1)[0],
            "url_viewer": f"https://ibb.co/id{n}",
            "url": f"https://i.ibb.co/full{n}/{name}",
            "display_url": f"https://i.ibb.co/disp{n}/{name}",
            "width": "10", "height": "10", "size": "42",
            "thumb": {"url": f"https://i.ibb.co/thumb{n}/{name}"},
            "medium": {"url": f"https://i.ibb.co/disp{n}/{name}"},
            "delete_url": f"https://ibb.co/id{n}/secretdelete",
        },
        "success": True, "status": 200,
    })


def form(data: bytes) -> dict:
    return {k: v[0] for k, v in parse_qs(data.decode()).items()}


class FakeTelegraph:
    """createAccount / createPage / getPage / page HTML ko simulate karta hai."""

    def __init__(self, http, *, strip_img=False, render_img=True, fail_create_for=(), image_ok=True,
                 image_ctype="image/jpeg"):
        self.pages = {}
        self.account_calls = 0
        self.strip_img = strip_img
        self.render_img = render_img
        self.fail_create_for = set(fail_create_for)
        self.titles = []
        http.on("POST", "https://api.telegra.ph/createAccount", self._acct)
        http.on("POST", "https://api.telegra.ph/createPage", self._create)
        http.on("POST", "https://api.telegra.ph/getPageList", self._list)
        http.on("GET", "https://api.telegra.ph/getPage/", self._get)
        http.on("GET", "https://telegra.ph/", self._html)
        http.on("GET", "https://i.ibb.co/", lambda u, h, d: Response(
            206 if image_ok else 404, {"content-type": image_ctype if image_ok else "text/html"},
            b"\xff\xd8\xff" + b"0" * 50 if image_ok else b"nope"))

    def _acct(self, url, h, d):
        self.account_calls += 1
        return jresp({"ok": True, "result": {"short_name": "gallery", "access_token": "TOKEN123"}})

    def _create(self, url, h, d):
        f = form(d)
        assert f["access_token"], "token missing"
        self.titles.append(f["title"])
        if f["title"] in self.fail_create_for:
            return jresp({"ok": False, "error": "CONTENT_TOO_BIG"})
        content = json.loads(f["content"])
        if self.strip_img:
            content = [{"tag": "p", "children": ["stripped"]}]
        path = f"Page-{len(self.pages) + 1}-10-11"
        self.pages[path] = {"title": f["title"], "content": content}
        return jresp({"ok": True, "result": {"path": path, "url": f"https://telegra.ph/{path}", "title": f["title"]}})

    def _list(self, url, h, d):
        pages = [{"path": p, "url": f"https://telegra.ph/{p}", "title": v["title"]} for p, v in reversed(list(self.pages.items()))]
        return jresp({"ok": True, "result": {"total_count": len(pages), "pages": pages}})

    def _get(self, url, h, d):
        path = url.split("/getPage/")[1].split("?")[0]
        if path not in self.pages:
            return jresp({"ok": False, "error": "PAGE_NOT_FOUND"})
        return jresp({"ok": True, "result": {"path": path, "content": self.pages[path]["content"]}})

    def _html(self, url, h, d):
        path = url.rsplit("/", 1)[1]
        if path not in self.pages:
            return Response(404, {}, b"")
        parts = []

        def rec(n):
            if isinstance(n, str):
                parts.append(n)
            elif n.get("tag") == "img" and self.render_img:
                parts.append(f'<img src="{n["attrs"]["src"]}">')
            else:
                for c in n.get("children", []):
                    rec(c)
        for n in self.pages[path]["content"]:
            rec(n)
        return Response(200, {"content-type": "text/html"}, ("<html>" + "".join(parts)).encode())
