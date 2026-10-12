import json
import unittest

from src import telegraph_publisher as tp
from src.http_util import NetworkError, Response
from src.image_store import T_FAILED, T_UNVERIFIED, T_VERIFIED
from tests.fakes_http import FakeHttp, FakeTelegraph, form, jresp

EMBED = '<a href="https://ibb.co/id1"><img src="https://i.ibb.co/full1/a.jpg" alt="a" border="0"></a>'


class Convert(unittest.TestCase):
    def test_imgbb_embed_becomes_supported_nodes(self):
        nodes, dropped = tp.html_to_nodes(EMBED)
        self.assertEqual(nodes, [{"tag": "a", "attrs": {"href": "https://ibb.co/id1"},
                                  "children": [{"tag": "img", "attrs": {"src": "https://i.ibb.co/full1/a.jpg"}}]}])
        self.assertEqual(sorted(dropped), ["attr:alt", "attr:border"])  # Telegraph sirf href/src rakhta hai

    def test_unsafe_and_unsupported_are_dropped_not_emitted(self):
        nodes, dropped = tp.html_to_nodes(
            '<div><img src="javascript:alert(1)"><iframe src="https://evil"></iframe>'
            '<a href="data:text/html,x">t</a><script>x</script></div>')
        text = json.dumps(nodes)
        self.assertNotIn("javascript", text); self.assertNotIn("iframe", text); self.assertNotIn("data:", text)
        self.assertIn("tag:iframe", dropped)

    def test_figure_methods(self):
        rec = {"html_embed": {"constructed": {"full_linked": EMBED}}, "imgbb": {"url": "https://i.ibb.co/full1/a.jpg"}}
        node, dropped = tp.image_figure(rec, tp.METHOD_EMBED)
        self.assertEqual(node["tag"], "figure"); self.assertEqual(tp.walk_img_srcs(node), ["https://i.ibb.co/full1/a.jpg"])
        self.assertTrue(dropped)
        node, dropped = tp.image_figure(rec, tp.METHOD_PLAIN)
        self.assertEqual(node["children"][0]["tag"], "img"); self.assertEqual(dropped, [])
        with self.assertRaises(tp.TelegraphError):
            tp.image_figure({"html_embed": {"constructed": None}, "imgbb": {}}, tp.METHOD_EMBED)
        with self.assertRaises(tp.TelegraphError):
            tp.image_figure({"html_embed": {"constructed": {"full_linked": "<p>no image</p>"}}, "imgbb": {}}, tp.METHOD_EMBED)


class Split(unittest.TestCase):
    def fig(self, i):
        return {"tag": "figure", "children": [{"tag": "img", "attrs": {"src": f"https://i.ibb.co/{i}/x.jpg"}}]}

    def test_split_respects_limit_and_keeps_order(self):
        units = [(self.fig(i), f"f{i}") for i in range(20)]
        pages = tp.split_units(units, limit=400)
        self.assertGreater(len(pages), 1)
        self.assertEqual([u[1] for p in pages for u in p], [f"f{i}" for i in range(20)])
        for p in pages:
            self.assertLessEqual(len(json.dumps([u[0] for u in p], separators=(",", ":")).encode()), 400)

    def test_heading_never_orphaned_at_page_end(self):
        h = ({"tag": "h3", "children": ["Album"]}, None)
        units = [(self.fig(0), "f0"), (self.fig(1), "f1"), h, (self.fig(2), "f2")]
        size = lambda ns: len(json.dumps([u[0] for u in ns], separators=(",", ":")).encode())
        pages = tp.split_units(units, limit=size(units[:3]) + 1)
        self.assertIsNotNone(pages[0][-1][1])
        self.assertIsNone(pages[1][0][1])


class Client(unittest.TestCase):
    def test_create_page_posts_nodes_json_not_html(self):
        http = FakeHttp(); fake = FakeTelegraph(http)
        c = tp.TelegraphClient(http, "TOK", sleep=lambda s: None)
        nodes = [{"tag": "figure", "children": [{"tag": "img", "attrs": {"src": "https://i.ibb.co/1/a.jpg"}}]}]
        page = c.create_page("T", nodes, "Me")
        f = form(http.calls[0][3])
        self.assertEqual(json.loads(f["content"]), nodes)
        self.assertEqual((f["title"], f["author_name"], f["access_token"]), ("T", "Me", "TOK"))
        self.assertEqual(page["url"], "https://telegra.ph/Page-1-10-11")

    def test_errors_and_flood_wait(self):
        http = FakeHttp(); sleeps = []
        http.queue("POST", "https://api.telegra.ph/createPage",
                   [jresp({"ok": False, "error": "FLOOD_WAIT_5"}),
                    jresp({"ok": True, "result": {"path": "p", "url": "https://telegra.ph/p"}})])
        c = tp.TelegraphClient(http, "S3CR3T-T0KEN", sleep=sleeps.append)
        self.assertEqual(c.create_page("T", ["x"])["path"], "p"); self.assertIn(5, sleeps)
        http = FakeHttp(); http.queue("POST", "https://api.telegra.ph/createPage", [jresp({"ok": False, "error": "FLOOD_WAIT_9999"})])
        with self.assertRaises(tp.TelegraphError):
            tp.TelegraphClient(http, "S3CR3T-T0KEN", sleep=lambda s: None).create_page("T", ["x"])
        http = FakeHttp(); http.queue("POST", "https://api.telegra.ph/createPage", [jresp({"ok": False, "error": "ACCESS_TOKEN_INVALID"})])
        with self.assertRaises(tp.TelegraphError) as cm:
            tp.TelegraphClient(http, "S3CR3T-T0KEN", sleep=lambda s: None).create_page("T", ["x"])
        self.assertIn("ACCESS_TOKEN_INVALID", str(cm.exception))

    def test_network_retry_and_token_redaction(self):
        http = FakeHttp(); http.queue("POST", "https://api.telegra.ph/createPage", [NetworkError("x S3CR3T-T0KEN y")] * 4)
        with self.assertRaises(tp.TelegraphError) as cm:
            tp.TelegraphClient(http, "S3CR3T-T0KEN", sleep=lambda s: None).create_page("T", ["x"])
        self.assertNotIn("S3CR3T-T0KEN", str(cm.exception).replace("TelegraphError", ""))

    def test_oversize_refused_locally(self):
        http = FakeHttp()
        with self.assertRaises(tp.TelegraphError):
            tp.TelegraphClient(http, "TOK").create_page("T", ["x" * 70000])
        self.assertEqual(http.calls, [])

    def test_create_account(self):
        http = FakeHttp(); FakeTelegraph(http)
        c = tp.TelegraphClient(http); c.create_account("gallery", "me")
        self.assertEqual(c.token, "TOKEN123")


class Verify(unittest.TestCase):
    SRC = "https://i.ibb.co/full1/a.jpg"

    def run_verify(self, **kw):
        http = FakeHttp(); fake = FakeTelegraph(http, **{k: v for k, v in kw.items() if k != "tamper"})
        c = tp.TelegraphClient(http, "TOK", sleep=lambda s: None)
        nodes = [{"tag": "figure", "children": [{"tag": "img", "attrs": {"src": self.SRC}}]}]
        page = c.create_page("T", nodes)
        return tp.verify_page(c, http, page["path"], {"f1": self.SRC})["f1"]

    def test_verified_requires_all_three_checks(self):
        self.assertEqual(self.run_verify(), (T_VERIFIED, None))

    def test_stripped_by_telegraph_is_failed(self):
        st, err = self.run_verify(strip_img=True)
        self.assertEqual(st, T_FAILED); self.assertIn("stored content", err)

    def test_dead_or_non_image_url_is_failed(self):
        st, err = self.run_verify(image_ok=False)
        self.assertEqual(st, T_FAILED); self.assertIn("HTTP 404", err)
        st, err = self.run_verify(image_ctype="text/html")
        self.assertEqual(st, T_FAILED); self.assertIn("content-type", err)

    def test_not_in_rendered_html_is_unverified_not_success(self):
        st, err = self.run_verify(render_img=False)
        self.assertEqual(st, T_UNVERIFIED); self.assertIn("render", err)

    def test_api_unreachable_is_unverified(self):
        http = FakeHttp()
        http.on("GET", "https://api.telegra.ph/getPage/", lambda u, h, d: NetworkError("down"))
        http.on("GET", "https://telegra.ph/", lambda u, h, d: Response(500, {}, b""))
        http.on("GET", "https://i.ibb.co/", lambda u, h, d: Response(200, {"content-type": "image/png"}, b"x"))
        c = tp.TelegraphClient(http, "TOK", sleep=lambda s: None)
        st, err = tp.verify_page(c, http, "P-1", {"f1": self.SRC})["f1"]
        self.assertEqual(st, T_UNVERIFIED)


if __name__ == "__main__":
    unittest.main()
