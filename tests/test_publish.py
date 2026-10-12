import json
import tempfile
import unittest
from pathlib import Path

from src import publish as pub
from src import telegraph_publisher as tp
from src.http_util import Response
from src.image_store import ImageStore, collect_images
from src.imgbb_uploader import ImgBBClient
from src.upload_store import UploadStore
from tests.fakes_http import FakeHttp, FakeTelegraph, imgbb_ok, jresp
from tests.test_image_store import arch


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "workspace"
        (self.ws / "extracted/1/images").mkdir(parents=True)
        for n in ("1.jpg", "2.jpg", "3.jpg"):
            (self.ws / "extracted/1/images" / n).write_bytes(b"\xff\xd8\xff" + b"x" * 30)
        self.manifest = {"archives": [arch(1, "a.zip", [("a", "image", "images/1.jpg"), ("b", "image", "images/2.jpg"),
                                                       ("c", "image", "images/3.jpg")])]}
        self.store = ImageStore(self.ws / "manifests/image_manifest.json")
        self.store.sync(collect_images(self.manifest))
        self.out = []
        self.say = self.out.append

    def upload_all(self, http, **kw):
        c = ImgBBClient("KEY", http, sleep=lambda s: None)
        return pub.upload_images(self.store, c, self.store.select_for_upload(**kw), self.ws,
                                 delay=0, out=self.say, sleep=lambda s: None)


class ImgBBStage(Base):
    def test_uploads_record_urls_and_constructed_embed(self):
        http = FakeHttp(); http.queue("POST", "https://api.imgbb.com/", [imgbb_ok("1.jpg", 1), imgbb_ok("2.jpg", 2), imgbb_ok("3.jpg", 3)])
        run = self.upload_all(http)
        self.assertEqual((run.uploaded, run.failed), (3, 0))
        r = self.store.get("a")
        self.assertEqual(r["imgbb"]["url"], "https://i.ibb.co/full1/1.jpg")
        self.assertEqual(r["imgbb"]["display_url"], "https://i.ibb.co/disp1/1.jpg")
        self.assertIn('<img src="https://i.ibb.co/full1/1.jpg"', r["html_embed"]["constructed"]["full_linked"])
        self.assertIsNone(r["html_embed"]["original_from_imgbb"])
        self.assertEqual(r["filename"], "1.jpg")
        # disk par bhi hai
        self.assertEqual(ImageStore(self.store.path).get("b")["imgbb"]["status"], "uploaded")

    def test_one_failure_does_not_stop_others_and_retry_works(self):
        http = FakeHttp()
        http.queue("POST", "https://api.imgbb.com/", [imgbb_ok("1.jpg", 1), jresp({"error": {"message": "Invalid image"}}, 400), imgbb_ok("3.jpg", 3)])
        run = self.upload_all(http)
        self.assertEqual((run.uploaded, run.failed), (2, 1))
        self.assertEqual(self.store.get("b")["imgbb"]["status"], "failed")
        self.assertEqual(self.store.select_for_upload(), [])  # failed ko apne aap dobara nahi bhejta
        http.queue("POST", "https://api.imgbb.com/", [imgbb_ok("2.jpg", 2)])
        self.assertEqual(self.upload_all(http, retry_failed=True).uploaded, 1)

    def test_bad_key_stops_batch_and_keeps_images_pending(self):
        http = FakeHttp(); http.queue("POST", "https://api.imgbb.com/", [jresp({"error": {"message": "Invalid API v1 key."}}, 400)])
        run = self.upload_all(http)
        self.assertIn("API key", run.stopped_reason)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(self.store.counts(), {"pending": 3})

    def test_path_outside_workspace_refused(self):
        self.store.get("a")["path"] = "../../etc/passwd"
        http = FakeHttp()
        run = self.upload_all(http, limit=1)
        self.assertEqual(run.failed, 1); self.assertEqual(len(http.calls), 0)


class TelegraphStage(Base):
    def ready(self):
        http = FakeHttp(); http.queue("POST", "https://api.imgbb.com/", [imgbb_ok(f"{i}.jpg", i) for i in (1, 2, 3)])
        self.upload_all(http)

    def client(self, **kw):
        http = FakeHttp(); fake = FakeTelegraph(http, **kw)
        c = tp.TelegraphClient(http, "TOK", sleep=lambda s: None)
        return http, fake, c

    def test_happy_path_verified_and_in_order(self):
        self.ready(); http, fake, c = self.client()
        info = pub.publish_article(self.store, c, http, title="Album", out=self.say)
        self.assertEqual(info["main_url"], "https://telegra.ph/Page-1-10-11")
        srcs = tp.walk_img_srcs(fake.pages["Page-1-10-11"]["content"])
        self.assertEqual(srcs, [f"https://i.ibb.co/full{i}/{i}.jpg" for i in (1, 2, 3)])
        for fid in "abc":
            t = self.store.get(fid)["telegraph"]
            self.assertEqual((t["status"], t["method"], t["error"]), ("embedded_verified", tp.METHOD_EMBED, None))
            self.assertEqual(t["dropped_from_embed"], ["attr:alt", "attr:border"])
        rep = pub.build_report(self.store, UploadStore(self.ws / "manifests/video_uploads.json", "bot"))
        self.assertEqual((rep["images_uploaded_to_imgbb"], rep["images_embedded_verified"], rep["images_not_embedded"]), (3, 3, []))

    def test_republish_blocked_without_force(self):
        self.ready(); http, fake, c = self.client()
        pub.publish_article(self.store, c, http, title="A", out=self.say)
        with self.assertRaises(pub.PublishError):
            pub.publish_article(self.store, c, http, title="A", out=self.say)
        pub.publish_article(self.store, c, http, title="A", force=True, out=self.say)
        self.assertEqual(len(fake.pages), 2)

    def test_telegraph_stripping_images_is_never_reported_as_success(self):
        self.ready(); http, fake, c = self.client(strip_img=True)
        pub.publish_article(self.store, c, http, title="A", out=self.say)
        rep = pub.build_report(self.store, UploadStore(self.ws / "manifests/video_uploads.json", "bot"))
        self.assertEqual(rep["images_embedded_verified"], 0)
        self.assertEqual(len(rep["images_not_embedded"]), 3)
        self.assertEqual(self.store.get("a")["telegraph"]["status"], "failed")

    def test_unverified_counted_separately(self):
        self.ready(); http, fake, c = self.client(render_img=False)
        pub.publish_article(self.store, c, http, title="A", out=self.say)
        rep = pub.build_report(self.store, UploadStore(self.ws / "manifests/video_uploads.json", "bot"))
        self.assertEqual((rep["images_embedded_verified"], rep["images_embedded_unverified"]), (0, 3))
        self.assertIn("verify nahi hui", pub.format_report(rep))

    def test_failed_imgbb_images_are_skipped_with_reason(self):
        http = FakeHttp()
        http.queue("POST", "https://api.imgbb.com/", [imgbb_ok("1.jpg", 1), jresp({"error": {"message": "Invalid image"}}, 400), imgbb_ok("3.jpg", 3)])
        self.upload_all(http)
        http2, fake, c = self.client()
        pub.publish_article(self.store, c, http2, title="A", out=self.say)
        t = self.store.get("b")["telegraph"]
        self.assertEqual(t["status"], "skipped"); self.assertIn("Invalid image", t["error"])
        rep = pub.build_report(self.store, UploadStore(self.ws / "manifests/video_uploads.json", "bot"))
        self.assertEqual((rep["images_uploaded_to_imgbb"], rep["images_embedded_verified"]), (2, 2))
        self.assertEqual(rep["images_not_embedded"][0]["filename"], "2.jpg")

    def test_multi_page_split_chain_links(self):
        self.ready(); http, fake, c = self.client()
        orig = tp.PAGE_BYTE_LIMIT
        import src.publish as p
        real = p.split_units
        p.split_units = lambda units, limit=orig: real(units, limit=330)
        self.addCleanup(lambda: setattr(p, "split_units", real))
        info = pub.publish_article(self.store, c, http, title="Album", out=self.say)
        self.assertGreater(len(info["pages"]), 1)
        self.assertTrue(all("Part" in pg["title"] for pg in info["pages"]))
        first = fake.pages[info["pages"][0]["path"]]["content"]
        self.assertEqual(first[-1]["children"][0]["attrs"]["href"], info["pages"][1]["url"])  # Next page link
        self.assertEqual(sum(pg["image_count"] for pg in info["pages"]), 3)

    def test_page_creation_failure_marks_images_failed(self):
        self.ready(); http, fake, c = self.client(fail_create_for={"A"})
        info = pub.publish_article(self.store, c, http, title="A", out=self.say)
        self.assertIsNone(info["main_url"])
        self.assertEqual({r["telegraph"]["status"] for r in self.store.files}, {"failed"})
        self.assertIn("CONTENT_TOO_BIG", self.store.get("a")["telegraph"]["error"])

    def test_plain_method(self):
        self.ready(); http, fake, c = self.client()
        pub.publish_article(self.store, c, http, title="A", method=tp.METHOD_PLAIN, out=self.say)
        self.assertEqual(self.store.get("a")["telegraph"]["method"], tp.METHOD_PLAIN)
        self.assertEqual(fake.pages["Page-1-10-11"]["content"][0]["children"][0]["tag"], "img")

    def test_nothing_uploaded_is_clear_error(self):
        http, fake, c = self.client()
        with self.assertRaises(pub.PublishError):
            pub.publish_article(self.store, c, http, title="A", out=self.say)

    def test_multi_archive_gets_headings(self):
        self.manifest["archives"].append(arch(2, "second.zip", [("d", "image", "images/9.jpg")]))
        (self.ws / "extracted/2/images").mkdir(parents=True); (self.ws / "extracted/2/images/9.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 9)
        self.store.sync(collect_images(self.manifest))
        http = FakeHttp(); http.queue("POST", "https://api.imgbb.com/", [imgbb_ok(f"{i}.jpg", i) for i in (1, 2, 3, 4)])
        self.upload_all(http)
        http2, fake, c = self.client()
        pub.publish_article(self.store, c, http2, title="A", out=self.say)
        heads = [n["children"][0] for n in fake.pages["Page-1-10-11"]["content"] if n["tag"] == "h3"]
        self.assertEqual(heads, ["a.zip", "second.zip"])


class Token(unittest.TestCase):
    def test_account_created_once_and_reused(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sessions" / "telegraph_account.json"
            http = FakeHttp(); fake = FakeTelegraph(http)
            c = tp.TelegraphClient(http); pub.ensure_telegraph_token(c, "", path, out=lambda m: None)
            self.assertEqual((c.token, fake.account_calls), ("TOKEN123", 1))
            c2 = tp.TelegraphClient(http); pub.ensure_telegraph_token(c2, "", path, out=lambda m: None)
            self.assertEqual((c2.token, fake.account_calls), ("TOKEN123", 1))
            c3 = tp.TelegraphClient(http); pub.ensure_telegraph_token(c3, "ENVTOKEN", path, out=lambda m: None)
            self.assertEqual(c3.token, "ENVTOKEN")


class Report(Base):
    def test_report_lists_only_completed_diskwala_links(self):
        up = UploadStore(self.ws / "manifests/video_uploads.json", "bot")
        up.sync([{"file_id": "v1", "filename": "one.mp4", "size": 1, "path": "p1"},
                 {"file_id": "v2", "filename": "two.mp4", "size": 1, "path": "p2"}])
        up.update(up.get("v1"), status="completed", url="https://dw.example/abc", urls=["https://dw.example/abc"])
        up.update(up.get("v2"), status="failed", error="x")
        rep = pub.build_report(self.store, up)
        self.assertEqual([d["url"] for d in rep["diskwala_links"]], ["https://dw.example/abc"])
        self.assertEqual(rep["diskwala_not_completed"], {"failed": 1})
        self.assertIsNone(rep["telegraph_article_url"])
        text = pub.format_report(rep)
        self.assertIn("(abhi nahi bana)", text); self.assertIn("https://dw.example/abc", text)
        self.assertNotIn("two.mp4: ", text.split("2)")[1].split("3)")[0].split("Baaki")[0])


if __name__ == "__main__":
    unittest.main()
