"""File Manager me seedhe bheje image(s)+video(s) (ZIP ke bina): batching + poora pipeline."""
import json
import shutil
import unittest
from datetime import datetime, timedelta, timezone

from src import db as D
from src.jobs import JobRepo
from src.scanner import media_info, scan_channel
from tests.helpers import MP4, PNG
from tests.stage2_fakes import CH, FakeTgClient, Msg, Runtime
from tests.test_diskwala_uploader import FakeChannel, text_reply

DATE = datetime(2026, 10, 1, tzinfo=timezone.utc)
LATER = DATE + timedelta(hours=1)       # quiet period beet chuka
JUST_AFTER = DATE + timedelta(seconds=30)


def img(mid, name=None):
    return Msg(mid, name or f"i{mid}.png", 10, "image/png")


def vid(mid, name=None):
    return Msg(mid, name or f"v{mid}.mp4", 10, "video/mp4")


def L(name):
    return [(2, text_reply(f"Link: https://dw.example/{name}", to_current=True))]


class Scanner(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = D.Database(":memory:"); D.migrate(self.db); self.repo = JobRepo(self.db)
        self.addCleanup(self.db.close)

    async def seeded(self, *msgs):
        c = FakeTgClient([Msg(1, "old.zip")])
        await scan_channel(c, None, CH, self.repo)  # first run: history skip
        c.msgs += list(msgs)
        return c

    def batches(self):
        return [(r["message_id"], [m["id"] for m in r["media"]], r["kind"]) for r in self.repo.list()]

    def test_media_info(self):
        self.assertEqual(media_info(img(5))["kind"], "image")
        self.assertEqual(media_info(vid(6))["kind"], "video")
        self.assertIsNone(media_info(Msg(7, "a.zip")))
        self.assertIsNone(media_info(Msg(8, "n.txt", mime="text/plain")))
        self.assertEqual(media_info(Msg(9, None)), None)

    async def test_image_video_then_next_image_starts_new_folder(self):
        c = await self.seeded(img(2), vid(3), img(4), img(5), vid(6), vid(7), img(8), vid(9))
        info = await scan_channel(c, None, CH, self.repo, now=lambda: LATER)
        self.assertEqual(info["new"], 3)
        self.assertEqual(self.batches(), [(2, [2, 3], "media"), (4, [4, 5, 6, 7], "media"), (8, [8, 9], "media")])
        self.assertEqual({r["status"] for r in self.repo.list()}, {"QUEUED"})
        self.assertEqual(self.repo.get(f"{CH}:4")["filename"], "Media batch #4 (2 img, 2 vid)")
        self.assertEqual(self.repo.last_scanned(CH), 9)
        self.assertEqual((await scan_channel(c, None, CH, self.repo, now=lambda: LATER))["new"], 0)  # duplicate nahi

    async def test_open_batch_waits_for_quiet_period_and_checkpoint_holds(self):
        c = await self.seeded(img(2), vid(3), img(4), vid(5))
        info = await scan_channel(c, None, CH, self.repo, now=lambda: JUST_AFTER)
        self.assertEqual(self.batches(), [(2, [2, 3], "media")])  # aakhri (4,5) abhi khula hai
        self.assertTrue(info["open_batch"]); self.assertEqual(self.repo.last_scanned(CH), 3)
        c.msgs.append(vid(6))  # user ne aur video bheja: usi batch me aayega
        await scan_channel(c, None, CH, self.repo, now=lambda: JUST_AFTER)
        self.assertEqual(len(self.repo.list()), 1)
        await scan_channel(c, None, CH, self.repo, now=lambda: LATER)
        self.assertEqual(self.batches()[-1], (4, [4, 5, 6], "media"))
        self.assertEqual(self.repo.last_scanned(CH), 6)

    async def test_zip_closes_open_batch_and_order_by_message_id(self):
        c = await self.seeded(img(2), vid(3), Msg(4, "a.zip"))
        await scan_channel(c, None, CH, self.repo, now=lambda: JUST_AFTER)
        self.assertEqual([(r["message_id"], r["kind"]) for r in self.repo.list()], [(2, "media"), (4, None)])
        self.assertEqual(self.repo.next_job().job["message_id"], 2)

    async def test_videos_only_and_images_only_batches_and_text_ignored(self):
        c = await self.seeded(vid(2), vid(3), Msg(4, "n.txt", mime="text/plain"), vid(5))
        await scan_channel(c, None, CH, self.repo, now=lambda: LATER)
        self.assertEqual(self.batches(), [(2, [2, 3, 5], "media")])
        c2 = await self.seeded(img(10), img(11))
        await scan_channel(c2, None, CH, self.repo, now=lambda: LATER)
        self.assertEqual(self.batches()[-1], (10, [10, 11], "media"))

    async def test_disabled_flag_ignores_media(self):
        c = await self.seeded(img(2), vid(3))
        await scan_channel(c, None, CH, self.repo, media_batches=False, now=lambda: LATER)
        self.assertEqual(self.repo.list(), [])


class Pipeline(unittest.IsolatedAsyncioTestCase):
    def make(self, **kw):
        rt = Runtime({}, **kw)
        self.addCleanup(rt.close)
        self.calls = 0
        data = {10: PNG + b"1", 11: PNG + b"2", 12: (MP4 + b"v").ljust(130, b"\0"), 13: (MP4 + b"w").ljust(110, b"\0")}

        async def dl(job, dest):
            self.calls += 1
            out = []
            for sp in job["media"]:
                folder = "images" if sp["kind"] == "image" else "videos"
                p = dest / folder / f"{sp['id']}_{sp['name']}"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data[sp["id"]])
                out.append({"type": sp["kind"], "path": p, "name": p.name, "msg_id": sp["id"]})
            return out
        rt.worker.ports.download_media = dl
        items = [{"id": 10, "kind": "image", "name": "a.png", "size": 9}, {"id": 11, "kind": "image", "name": "b.png", "size": 9},
                 {"id": 12, "kind": "video", "name": "x.mp4", "size": 130}, {"id": 13, "kind": "video", "name": "y.mp4", "size": 110}]
        rt.repo.discover_batch(CH, items, "2026-10-01T00:00:00+00:00"); rt.repo.queue_discovered()
        return rt

    async def test_batch_goes_through_imgbb_telegraph_diskwala_and_publishes(self):
        rt = self.make(cfg_kw={"video_name_base": "Trader Master", "image_name_base": "Pic"})
        rt.channel.replies_per_send.extend([L("1"), L("2")])
        while await rt.worker.run_once():
            pass
        self.assertEqual(rt.repo.get(f"{CH}:10")["status"], "COMPLETED")
        self.assertEqual(rt.imgbb_calls, ["Pic 01.png", "Pic 02.png"])
        self.assertEqual([n for n, _ in rt.channel.sent], ["Trader Master 01.mp4", "Trader Master 02.mp4"])  # bada pehle
        self.assertEqual(len(rt.dest.sent), 1)
        self.assertEqual(rt.dest.sent[0][1],
                         "Articles Images https://telegra.ph/Page-1-10-11\n\nVideo links https://dw.example/1\n\nhttps://dw.example/2")
        self.assertEqual(self.calls, 1)

    async def test_redownload_after_redeploy_restores_recorded_names(self):
        rt = self.make(cfg_kw={"video_name_base": "Trader Master", "image_name_base": "Pic"})
        rt.channel.replies_per_send.extend([L("1"), []])  # 2nd video timeout => PAUSED
        while await rt.worker.run_once():
            pass
        self.assertEqual(rt.repo.get(f"{CH}:10")["status"], "PAUSED")
        shutil.rmtree(rt.cfg.workspace / "jobs")  # ephemeral disk gayi
        ch2 = FakeChannel(rt.clock, [L("2")])
        rt.repo.resume(f"{CH}:10")
        dl = rt.worker.ports.download_media
        rt.worker = rt.new_worker(ch2); rt.worker.ports.download_media = dl
        while await rt.worker.run_once():
            pass
        self.assertEqual(rt.repo.get(f"{CH}:10")["status"], "COMPLETED")
        self.assertEqual(self.calls, 2)
        self.assertEqual([n for n, _ in ch2.sent], ["Trader Master 02.mp4"])  # 01 dobara nahi

    async def test_all_messages_deleted_fails_job_not_loop(self):
        from src.worker import ArchiveGone
        rt = self.make()

        async def gone(job, dest):
            raise ArchiveGone("saare messages delete")
        rt.worker.ports.download_media = gone
        await rt.worker.run_once()
        self.assertEqual(rt.repo.get(f"{CH}:10")["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
