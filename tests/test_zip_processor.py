import tempfile
import unittest
from pathlib import Path

from src.zip_processor import (
    ArchiveCorrupted, ArchiveRejected, Limits, extract_archive, safe_name, unique_dir,
)
from tests.helpers import MP4, PNG, make_symlink_zip, make_zip, sample_entries

LIM = Limits(min_free_bytes=0)


class ZipProcessorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_extracts_and_separates_images_videos(self):
        z = make_zip(self.tmp / "ok.zip", sample_entries())
        res = extract_archive(z, self.tmp / "out", LIM)
        types = sorted(f["type"] for f in res.files)
        self.assertEqual(types, ["image", "image", "video", "video"])
        paths = {f["relative_path"] for f in res.files}
        self.assertIn("images/album/a.JPG", paths)
        self.assertIn("images/album/sub/b.png", paths)
        self.assertIn("videos/clips/c.mp4", paths)
        for f in res.files:
            self.assertTrue((self.tmp / "out" / f["relative_path"]).is_file())
            self.assertEqual(len(f["file_id"]), 64)
            self.assertGreater(f["size"], 0)
        self.assertEqual(res.skipped_count, 3)  # txt, sh, __MACOSX
        self.assertFalse((self.tmp / "out" / "notes.txt").exists())
        self.assertTrue(z.exists())  # source archive delete nahi hota

    def test_path_traversal_rejected_and_nothing_written(self):
        for bad in ("../evil.png", "a/../../evil.png", "/abs/evil.png", "..\\evil.png", "C:\\evil.png"):
            z = make_zip(self.tmp / "bad.zip", {"ok.png": PNG, bad: PNG})
            out = self.tmp / "out"
            with self.assertRaises(ArchiveRejected, msg=bad):
                extract_archive(z, out, LIM)
            self.assertFalse(any(out.rglob("*.png")) if out.exists() else False)
            self.assertFalse((self.tmp / "evil.png").exists())

    def test_symlink_rejected(self):
        z = make_symlink_zip(self.tmp / "sym.zip")
        with self.assertRaises(ArchiveRejected):
            extract_archive(z, self.tmp / "out", LIM)

    def test_zip_bomb_ratio_rejected(self):
        z = make_zip(self.tmp / "bomb.zip", {"big.png": b"\x00" * (5 * 1024 * 1024)})
        with self.assertRaises(ArchiveRejected):
            extract_archive(z, self.tmp / "out", Limits(min_free_bytes=0, max_ratio=100))

    def test_file_count_limit(self):
        z = make_zip(self.tmp / "many.zip", {f"{i}.png": PNG for i in range(6)})
        with self.assertRaises(ArchiveRejected):
            extract_archive(z, self.tmp / "out", Limits(min_free_bytes=0, max_files=5))

    def test_extracted_size_limit(self):
        z = make_zip(self.tmp / "s.zip", {"a.png": PNG * 100, "b.png": PNG * 100})
        with self.assertRaises(ArchiveRejected):
            extract_archive(z, self.tmp / "out", Limits(min_free_bytes=0, max_extracted_bytes=5000))

    def test_archive_size_limit(self):
        z = make_zip(self.tmp / "s.zip", {"a.png": PNG})
        with self.assertRaises(ArchiveRejected):
            extract_archive(z, self.tmp / "out", Limits(min_free_bytes=0, max_archive_bytes=10))

    def test_corrupted_zip(self):
        p = self.tmp / "broken.zip"
        p.write_bytes(b"PK\x03\x04 this is not a real zip")
        with self.assertRaises(ArchiveCorrupted):
            extract_archive(p, self.tmp / "out", LIM)

    def test_truncated_zip(self):
        z = make_zip(self.tmp / "t.zip", sample_entries())
        z.write_bytes(z.read_bytes()[:-40])
        with self.assertRaises(ArchiveCorrupted):
            extract_archive(z, self.tmp / "out", LIM)

    def test_insufficient_disk_space(self):
        from src.zip_processor import InsufficientSpace
        z = make_zip(self.tmp / "ok.zip", {"a.png": PNG})
        with self.assertRaises(InsufficientSpace):
            extract_archive(z, self.tmp / "out", Limits(min_free_bytes=10**18))

    def test_name_collision_does_not_overwrite(self):
        z = make_zip(self.tmp / "ok.zip", {"a.png": PNG})
        r1 = extract_archive(z, self.tmp / "out", LIM)
        r2 = extract_archive(z, self.tmp / "out", LIM)
        self.assertNotEqual(r1.files[0]["relative_path"], r2.files[0]["relative_path"])

    def test_helpers(self):
        self.assertEqual(safe_name("a/b:c*.zip"), "a_b_c_.zip")
        (self.tmp / "d").mkdir()
        (self.tmp / "d" / "x").write_text("1")
        self.assertEqual(unique_dir(self.tmp / "d").name, "d_retry2")


if __name__ == "__main__":
    unittest.main()
