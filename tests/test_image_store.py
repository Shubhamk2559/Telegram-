import json
import tempfile
import unittest
from pathlib import Path

from src.image_store import ImageStore, ImageStoreError, collect_images, natural_key


def arch(mid, name, files, status="completed"):
    return {"archive_id": f"-1:{mid}", "archive_filename": name, "message_id": mid, "status": status,
            "extraction_dir": f"extracted/{mid}",
            "files": [{"file_id": fid, "type": t, "filename": rp.split("/")[-1], "size": 10,
                       "relative_path": rp, "path": f"extracted/{mid}/{rp}"} for fid, t, rp in files]}


class Collect(unittest.TestCase):
    def test_order_filter_dedupe(self):
        data = {"archives": [
            arch(20, "b.zip", [("h", "image", "images/1.jpg"), ("v", "video", "videos/a.mp4")]),
            arch(10, "a.zip", [("x", "image", "images/img10.jpg"), ("y", "image", "images/img2.jpg"),
                               ("h", "image", "images/dup.jpg")]),
            arch(5, "bad.zip", [("z", "image", "images/z.jpg")], status="failed"),
        ]}
        imgs = collect_images(data)
        self.assertEqual([i["file_id"] for i in imgs], ["h", "y", "x"])  # archive 10 pehle: dup < img2 < img10; archive 20 ka "h" dup skip
        self.assertEqual([i["order"] for i in imgs], [0, 1, 2])

    def test_natural_key(self):
        self.assertLess(natural_key("a2"), natural_key("a10"))


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "m" / "image_manifest.json"
        self.imgs = collect_images({"archives": [arch(1, "a.zip", [("a", "image", "images/1.jpg"), ("b", "image", "images/2.jpg")])]})

    def test_sync_is_additive_and_preserves_results(self):
        s = ImageStore(self.path)
        self.assertEqual(s.sync(self.imgs), 2)
        s.update(s.get("a"), "imgbb", status="uploaded", url="https://i.ibb.co/1/a.jpg")
        self.assertEqual(ImageStore(self.path).get("a")["imgbb"]["url"], "https://i.ibb.co/1/a.jpg")  # disk se
        more = self.imgs + collect_images({"archives": [arch(2, "b.zip", [("c", "image", "images/3.jpg")])]})[:1]
        more[-1]["order"] = 2
        s2 = ImageStore(self.path)
        self.assertEqual(s2.sync(more), 1)
        self.assertEqual(s2.get("a")["imgbb"]["status"], "uploaded")

    def test_html_embed_original_is_none_with_note(self):
        s = ImageStore(self.path); s.sync(self.imgs)
        he = s.get("a")["html_embed"]
        self.assertIsNone(he["original_from_imgbb"]); self.assertIn("return nahi", he["note"])

    def test_select_for_upload(self):
        s = ImageStore(self.path); s.sync(self.imgs)
        s.update(s.get("a"), "imgbb", status="failed", error="x")
        self.assertEqual([r["file_id"] for r in s.select_for_upload()], ["b"])
        self.assertEqual([r["file_id"] for r in s.select_for_upload(retry_failed=True)], ["a", "b"])
        self.assertEqual(len(s.select_for_upload(retry_failed=True, limit=1)), 1)

    def test_corrupt_manifest_backed_up(self):
        self.path.parent.mkdir(parents=True); self.path.write_text("{not json")
        with self.assertRaises(ImageStoreError):
            ImageStore(self.path)
        self.assertTrue(list(self.path.parent.glob("*.corrupt-*")))


if __name__ == "__main__":
    unittest.main()
