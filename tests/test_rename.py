import shutil
import unittest

from src.renamer import clean_base, rename_media
from tests.stage2_fakes import CH, Runtime, zip_entries
from tests.test_diskwala_uploader import FakeChannel, text_reply


def L(name):
    return [(2, text_reply(f"Link: https://dw.example/{name}", to_current=True))]


async def drain(rt, limit=12):
    n = 0
    while n < limit and await rt.worker.run_once():
        n += 1


class Base(unittest.IsolatedAsyncioTestCase):
    def make(self, *a, **kw):
        rt = Runtime(*a, **kw)
        self.addCleanup(rt.close)
        return rt


class Pipeline(Base):
    async def test_videos_renamed_by_size_and_images_by_order_before_sending(self):
        rt = self.make({1: zip_entries(2, (30, 10, 20), "a")},
                       cfg_kw={"video_name_base": "Trader Master", "image_name_base": "Pic"})
        rt.channel.replies_per_send.extend([L("1"), L("2"), L("3")])
        while await rt.worker.run_once():
            pass
        self.assertEqual(rt.repo.get(f"{CH}:1")["status"], "COMPLETED")
        # a_v0=130B, a_v1=110B, a_v2=120B  => bada pehle: 01=v0, 02=v2, 03=v1
        self.assertEqual([n for n, _ in rt.channel.sent], ["Trader Master 01.mp4", "Trader Master 02.mp4", "Trader Master 03.mp4"])
        self.assertEqual(rt.imgbb_calls, ["Pic 01.png", "Pic 02.png"])

    async def test_no_base_means_no_rename(self):
        rt = self.make({1: zip_entries(1, (30,), "a")})
        rt.channel.replies_per_send.extend([L("1")])
        while await rt.worker.run_once():
            pass
        self.assertEqual([n for n, _ in rt.channel.sent], ["a_v0.mp4"])
        self.assertEqual(rt.imgbb_calls, ["a_1.png"])

    async def test_redeploy_reextract_keeps_recorded_names(self):
        rt = self.make({1: zip_entries(1, (30, 20), "a")},
                       cfg_kw={"video_name_base": "Trader Master", "image_name_base": "Pic"})
        rt.channel.replies_per_send.extend([L("1"), []])  # 2nd video timeout => PAUSED
        while await rt.worker.run_once():
            pass
        self.assertEqual(rt.repo.get(f"{CH}:1")["status"], "PAUSED")
        shutil.rmtree(rt.cfg.workspace / "jobs"); shutil.rmtree(rt.cfg.workspace / "downloads")
        ch2 = FakeChannel(rt.clock, [L("2")])
        rt.repo.resume(f"{CH}:1"); rt.worker = rt.new_worker(ch2)
        while await rt.worker.run_once():
            pass
        self.assertEqual(rt.repo.get(f"{CH}:1")["status"], "COMPLETED")
        self.assertEqual([n for n, _ in ch2.sent], ["Trader Master 02.mp4"])  # 01 dobara nahi, 02 mila

    async def test_job_registered_without_rename_stays_original_after_reextract(self):
        rt = self.make({1: zip_entries(1, (30, 20), "a")})
        rt.channel.replies_per_send.extend([L("1"), []])
        while await rt.worker.run_once():
            pass
        shutil.rmtree(rt.cfg.workspace / "jobs"); shutil.rmtree(rt.cfg.workspace / "downloads")
        rt.cfg.video_name_base = "Trader Master"  # ab base set kiya, par ye job purani hai
        ch2 = FakeChannel(rt.clock, [L("2")])
        rt.repo.resume(f"{CH}:1"); rt.worker = rt.new_worker(ch2)
        while await rt.worker.run_once():
            pass
        self.assertEqual(rt.repo.get(f"{CH}:1")["status"], "COMPLETED")
        self.assertEqual([n for n, _ in ch2.sent], ["a_v1.mp4"])


class Unit(unittest.TestCase):
    def test_clean_base_and_collision_with_existing_names(self):
        import tempfile
        from pathlib import Path
        self.assertEqual(clean_base('  Tra/der: "Master" '), 'Tra_der_ _Master_')
        with tempfile.TemporaryDirectory() as d:
            d = Path(d); (d / "videos").mkdir()
            files = []
            for name, size, fid in (("Trader Master 02.mp4", 10, "x"), ("Trader Master 01.mp4", 30, "y")):
                (d / "videos" / name).write_bytes(b"1" * size)
                files.append({"file_id": fid, "type": "video", "filename": name, "size": size, "relative_path": f"videos/{name}"})
            rename_media(files, d, "Trader Master")
            self.assertEqual({f["file_id"]: f["filename"] for f in files}, {"y": "Trader Master 01.mp4", "x": "Trader Master 02.mp4"})
            self.assertEqual((d / "videos/Trader Master 01.mp4").stat().st_size, 30)


if __name__ == "__main__":
    unittest.main()
