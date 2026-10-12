import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import imgbb_uploader as ib
from src.http_util import NetworkError, Response, encode_multipart
from tests.fakes_http import FakeHttp, imgbb_ok, jresp


def img_file(d, name="a.jpg", data=b"\xff\xd8\xff" + b"1" * 40):
    p = Path(d) / name
    p.write_bytes(data)
    return p


def client(http, key="SECRETKEY", sleeps=None, retries=3):
    s = sleeps if sleeps is not None else []
    return ib.ImgBBClient(key, http, max_retries=retries, sleep=s.append)


class Embeds(unittest.TestCase):
    def test_constructed_embeds_use_api_urls_and_escape(self):
        e = ib.build_html_embeds({"url_viewer": "https://ibb.co/x", "url": "https://i.ibb.co/a/b.jpg",
                                  "thumb": {"url": "https://i.ibb.co/t/b.jpg"}, "title": 'a"b<c'})
        self.assertEqual(e["full_linked"],
                         '<a href="https://ibb.co/x"><img src="https://i.ibb.co/a/b.jpg" alt="a&quot;b&lt;c" border="0"></a>')
        self.assertIn("https://i.ibb.co/t/b.jpg", e["thumb_linked"])

    def test_parse_response(self):
        r = ib.parse_upload_response(__import__("json").loads(imgbb_ok("a.jpg").text()))
        self.assertEqual(r["url"], "https://i.ibb.co/full1/a.jpg")
        self.assertEqual(r["display_url"], "https://i.ibb.co/disp1/a.jpg")
        self.assertEqual(r["viewer_url"], "https://ibb.co/id1")
        self.assertIn("full_linked", r["html_embeds"])

    def test_parse_rejects_incomplete(self):
        for bad in ({}, {"success": True}, {"success": False, "data": {"url": "x"}}, {"success": True, "data": {}}):
            with self.assertRaises(ib.ImgBBError):
                ib.parse_upload_response(bad)


class Upload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.f = img_file(self.tmp.name)

    def test_success_sends_multipart_with_key_only_in_query(self):
        http = FakeHttp(); http.queue("POST", ib.IMGBB_ENDPOINT, [imgbb_ok()])
        res = client(http).upload(self.f, "a.jpg")
        self.assertEqual(res["id"], "id1")
        method, url, headers, data = http.calls[0]
        self.assertTrue(url.startswith(ib.IMGBB_ENDPOINT + "?key=SECRETKEY"))
        self.assertTrue(headers["Content-Type"].startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="image"; filename="a.jpg"', data)
        self.assertNotIn(b"SECRETKEY", data)

    def test_retries_5xx_then_ok(self):
        http = FakeHttp(); sleeps = []
        http.queue("POST", ib.IMGBB_ENDPOINT, [Response(502, {}, b"bad gateway"), NetworkError("timeout"), imgbb_ok()])
        self.assertEqual(client(http, sleeps=sleeps).upload(self.f)["id"], "id1")
        self.assertEqual(len(http.calls), 3); self.assertEqual(len(sleeps), 2)

    def test_400_is_per_image_error_no_retry(self):
        http = FakeHttp(); http.queue("POST", ib.IMGBB_ENDPOINT, [jresp({"error": {"message": "Invalid image"}}, 400)])
        with self.assertRaises(ib.ImgBBError) as cm:
            client(http).upload(self.f)
        self.assertNotIsInstance(cm.exception, ib.ImgBBFatal)
        self.assertIn("Invalid image", str(cm.exception)); self.assertEqual(len(http.calls), 1)

    def test_bad_key_is_fatal(self):
        for resp in (jresp({"error": {"message": "Invalid API v1 key."}}, 400), jresp({"error": "x"}, 403)):
            http = FakeHttp(); http.queue("POST", ib.IMGBB_ENDPOINT, [resp])
            with self.assertRaises(ib.ImgBBFatal):
                client(http).upload(self.f)

    def test_rate_limit_after_retries_is_fatal_not_bypassed(self):
        http = FakeHttp(); http.queue("POST", ib.IMGBB_ENDPOINT, [Response(429, {}, b"{}")] * 4)
        with self.assertRaises(ib.ImgBBFatal):
            client(http).upload(self.f)
        self.assertEqual(len(http.calls), 4)

    def test_key_never_leaks_into_errors(self):
        http = FakeHttp(); http.queue("POST", ib.IMGBB_ENDPOINT, [NetworkError("failed https://x?key=SECRETKEY")] * 4)
        with self.assertRaises(ib.ImgBBError) as cm:
            client(http).upload(self.f)
        self.assertNotIn("SECRETKEY", str(cm.exception))
        http = FakeHttp(); http.queue("POST", ib.IMGBB_ENDPOINT, [jresp({"error": {"message": "bad SECRETKEY"}}, 400)])
        with self.assertRaises(ib.ImgBBError) as cm:
            client(http).upload(self.f)
        self.assertNotIn("SECRETKEY", str(cm.exception))

    def test_local_checks(self):
        http = FakeHttp()
        with self.assertRaises(ib.ImgBBError):
            client(http).upload(img_file(self.tmp.name, "empty.jpg", b""))
        with mock.patch.object(ib, "MAX_IMAGE_BYTES", 10):
            with self.assertRaises(ib.ImgBBError):
                client(http).upload(self.f)
        with self.assertRaises(ib.ImgBBError):
            client(http).upload(Path(self.tmp.name) / "missing.jpg")
        self.assertEqual(http.calls, [])  # network tak gaye hi nahi

    def test_empty_key(self):
        with self.assertRaises(ib.ImgBBFatal):
            ib.ImgBBClient("", FakeHttp())


class Multipart(unittest.TestCase):
    def test_encode(self):
        body, ctype = encode_multipart({"a": "1"}, {"image": ('x".jpg', b"BIN", "image/jpeg")})
        b = ctype.split("boundary=")[1].encode()
        self.assertTrue(body.endswith(b"--" + b + b"--\r\n"))
        self.assertIn(b"BIN", body); self.assertNotIn(b'x".jpg', body)


if __name__ == "__main__":
    unittest.main()
