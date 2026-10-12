import asyncio
import shutil
import unittest

from src import db as D
from src.app import cmd_approve
from src.db_stores import DbImageStore, DbUploadStore, PostStore
from src.http_util import Response
from src.imgbb_uploader import ImgBBClient
from src.channel_reader import DownloadError
from tests.fakes_http import imgbb_ok, jresp
from tests.stage2_fakes import CH, FakeDest, Runtime, zip_entries
from tests.test_diskwala_uploader import FakeChannel, text_reply


def L(name):
    return [(2, text_reply(f"Link: https://dw.example/{name}", to_current=True))]


async def drain(rt, limit=12):
    n = 0
    while n < limit and await rt.worker.run_once():
        n += 1
    return n


class Base(unittest.IsolatedAsyncioTestCase):
    def make(self, *a, **kw):
        rt = Runtime(*a, **kw)
        self.addCleanup(rt.close)
        return rt

    def status(self, rt, mid):
        return rt.repo.get(f"{CH}:{mid}")["status"]


class Ordering(Base):
    async def test_two_zips_chronological_isolated_and_posts_match_spec(self):
        rt = self.make({1: zip_entries(2, (30,), "a"), 2: zip_entries(1, (20, 10), "b")})
        rt.discover_ids = [2, 1]  # discovery order ulta: processing phir bhi message_id se
        rt.channel.replies_per_send.extend([L("A1"), L("B1"), L("B2")])
        await drain(rt)
        self.assertEqual((self.status(rt, 1), self.status(rt, 2)), ("COMPLETED", "COMPLETED"))
        self.assertEqual(rt.downloads, [1, 2])
        self.assertEqual(rt.imgbb_calls, ["a_1.png", "a_2.png", "b_1.png"])
        self.assertEqual([n for n, _ in rt.channel.sent], ["a_v0.mp4", "b_v0.mp4", "b_v1.mp4"])  # 2nd ZIP me 20MB pehle
        (m1, t1), (m2, t2) = rt.dest.sent
        self.assertEqual(t1, "Articles Images https://telegra.ph/Page-1-10-11\n\nVideo links https://dw.example/A1")
        self.assertEqual(t2, "Articles Images https://telegra.ph/Page-2-10-11\n\nVideo links https://dw.example/B1\n\nhttps://dw.example/B2")
        self.assertNotIn("A1", t2); self.assertNotIn("a_", str(rt.tg.pages["Page-2-10-11"]))  # ZIP isolation
        self.assertEqual({p["title"] for p in rt.tg.pages.values()}, {"My Image Collection"})

    async def test_temp_files_cleaned_after_completion(self):
        rt = self.make({1: zip_entries(1, (30,), "a")})
        rt.channel.replies_per_send.extend([L("A1")])
        await drain(rt)
        self.assertFalse((rt.cfg.workspace / "downloads" / "1.zip").exists())
        self.assertEqual(list((rt.cfg.workspace / "jobs").rglob("*.png")), [])

    async def test_nine_videos_two_posts_same_article_order_preserved(self):
        sizes = tuple(100 + 10 * i for i in range(9))  # v8 sabse bada
        rt = self.make({1: zip_entries(1, sizes, "a")})
        rt.channel.replies_per_send.extend([L(f"L{i}") for i in range(1, 10)])
        await drain(rt)
        self.assertEqual([n for n, _ in rt.channel.sent], [f"a_v{i}.mp4" for i in range(8, -1, -1)])
        self.assertEqual(len(rt.dest.sent), 2)
        p1, p2 = (t for _, t in rt.dest.sent)
        art = "https://telegra.ph/Page-1-10-11"
        self.assertTrue(p1.startswith(f"Articles Images {art}\n\nVideo links https://dw.example/L1\n\nhttps://dw.example/L2"))
        self.assertIn("L8", p1); self.assertNotIn("L9", p1)
        self.assertEqual(p2, f"Articles Images {art}\n\nVideo links https://dw.example/L9")

    async def test_images_only_and_videos_only_policies(self):
        rt = self.make({1: zip_entries(3, (), "a"), 2: zip_entries(0, (20, 10), "b")})
        rt.channel.replies_per_send.extend([L("B1"), L("B2")])
        await drain(rt)
        self.assertEqual([t for _, t in rt.dest.sent],
                         ["Articles Images https://telegra.ph/Page-1-10-11", "Video links https://dw.example/B1\n\nhttps://dw.example/B2"])
        self.assertEqual(len(rt.channel.sent), 2)  # images-only ZIP ke liye bot ko kuch nahi gaya


class Failures(Base):
    async def test_video_failure_pauses_blocks_queue_then_resume_does_not_redo_work(self):
        rt = self.make({1: zip_entries(2, (30, 20, 10), "a"), 2: zip_entries(1, (10,), "b")})
        rt.channel.replies_per_send.extend([L("A1"), [], []])  # 2nd video timeout, resend off
        await drain(rt)
        j = rt.repo.get(f"{CH}:1")
        self.assertEqual(j["status"], "PAUSED"); self.assertIn("a_v1.mp4", j["paused_reason"])
        self.assertEqual(self.status(rt, 2), "QUEUED")  # ZIP2 ko haath nahi lagaya
        self.assertEqual(rt.downloads, [1]); self.assertEqual(len(rt.dest.sent), 0)
        self.assertFalse(await rt.worker.run_once())  # queue ruki hai, skip nahi
        imgs_before, pages_before = list(rt.imgbb_calls), len(rt.tg.pages)

        ch2 = FakeChannel(rt.clock, [L("A2"), L("A3"), L("B1")])
        rt.repo.resume(f"{CH}:1")
        rt.worker = rt.new_worker(ch2)  # restart
        await drain(rt)
        self.assertEqual(rt.imgbb_calls[:len(imgs_before)], imgs_before)
        self.assertEqual(rt.imgbb_calls[len(imgs_before):], ["b_1.png"])  # ZIP1 ki images dobara nahi gayi
        self.assertEqual(len(rt.tg.pages), pages_before + 1)  # ZIP1 ka article dobara nahi bana; sirf ZIP2 ka
        self.assertEqual([n for n, _ in ch2.sent], ["a_v1.mp4", "a_v2.mp4", "b_v0.mp4"])  # v0 (pehla) dobara nahi
        self.assertEqual((self.status(rt, 1), self.status(rt, 2)), ("COMPLETED", "COMPLETED"))
        self.assertEqual(rt.downloads, [1, 2])  # media disk par tha: ZIP1 dobara download nahi
        self.assertIn("https://dw.example/A1\n\nhttps://dw.example/A2\n\nhttps://dw.example/A3", rt.dest.sent[0][1])
        self.assertEqual(len(rt.dest.sent), 2)

    async def test_redeploy_wipes_disk_then_redownload_only_when_media_needed(self):
        rt = self.make({1: zip_entries(2, (30, 20), "a")})
        rt.channel.replies_per_send.extend([L("A1"), []])
        await drain(rt)
        self.assertEqual(self.status(rt, 1), "PAUSED")
        shutil.rmtree(rt.cfg.workspace / "jobs"); shutil.rmtree(rt.cfg.workspace / "downloads")  # ephemeral FS gaya
        ch2 = FakeChannel(rt.clock, [L("A2")])
        rt.repo.resume(f"{CH}:1"); rt.worker = rt.new_worker(ch2)
        await drain(rt)
        self.assertEqual(rt.downloads, [1, 1])  # video baaki tha: dobara download
        self.assertEqual(self.status(rt, 1), "COMPLETED"); self.assertEqual(len(rt.imgbb_calls), 2)

        # publish stage par wipe: kuch media chahiye hi nahi => download nahi
        class Forbidden(Exception): pass
        Forbidden.__name__ = "ChatWriteForbiddenError"
        rt2 = self.make({1: zip_entries(1, (30,), "a")}, dest=FakeDest(errors=[Forbidden("x")]))
        rt2.channel.replies_per_send.extend([L("A1")])
        await drain(rt2)
        self.assertEqual(self.status(rt2, 1), "PAUSED"); self.assertEqual(rt2.dest.sent, [])
        shutil.rmtree(rt2.cfg.workspace / "jobs")
        rt2.repo.resume(f"{CH}:1"); rt2.worker = rt2.new_worker()
        await drain(rt2)
        self.assertEqual(rt2.downloads, [1]); self.assertEqual(len(rt2.dest.sent), 1)
        self.assertEqual(self.status(rt2, 1), "COMPLETED")

    async def test_corrupt_zip_fails_but_queue_continues(self):
        rt = self.make({1: b"this is not a zip", 2: zip_entries(1, (10,), "b")})
        rt.channel.replies_per_send.extend([L("B1")])
        await drain(rt)
        self.assertEqual((self.status(rt, 1), self.status(rt, 2)), ("FAILED", "COMPLETED"))
        self.assertIn("Corrupted", rt.repo.get(f"{CH}:1")["error"])

    async def test_zip_without_supported_media_fails(self):
        rt = self.make({1: {"notes.txt": b"hi"}})
        await drain(rt)
        self.assertEqual(self.status(rt, 1), "FAILED")

    async def test_needs_review_blocks_posts_until_approved(self):
        rt = self.make({1: zip_entries(1, (30,), "a")})
        rt.channel.replies_per_send.extend([L("A1")])
        orig = rt.worker._stage_videos

        async def patched(aid):
            await orig(aid)
            s = DbUploadStore(rt.db, aid, "Bot", rt.cfg.workspace)
            s.update(s.files[0], needs_review=True)
        rt.worker._stage_videos = patched
        await drain(rt)
        self.assertEqual(self.status(rt, 1), "PAUSED"); self.assertIn("needs_review", rt.repo.get(f"{CH}:1")["paused_reason"])
        self.assertEqual(rt.dest.sent, [])
        rt.worker._stage_videos = orig
        cmd_approve(rt.db, f"{CH}:1"); rt.repo.resume(f"{CH}:1")
        await drain(rt)
        self.assertEqual(self.status(rt, 1), "COMPLETED"); self.assertEqual(len(rt.dest.sent), 1)

    async def test_failed_image_pauses_unless_skip_policy(self):
        for policy, expect in (("pause", "PAUSED"), ("skip", "COMPLETED")):
            rt = self.make({1: zip_entries(2, (), "a")}, cfg_kw={"image_failure_policy": policy})
            rt.http.on("POST", "https://api.imgbb.com/", lambda u, h, d, rt=rt: jresp({"error": {"message": "Invalid image"}}, 400)
                       if b"a_1.png" in d else rt._imgbb(u, h, d))
            await drain(rt)
            self.assertEqual(self.status(rt, 1), expect, policy)
            if policy == "skip":
                self.assertEqual(len(rt.tg.pages["Page-1-10-11"]["content"]), 1)  # sirf successful image article me
                self.assertIn("a_1.png", __import__("src.reports_zip", fromlist=["x"]).zip_report(rt.db, f"{CH}:1"))


class Retries(Base):
    async def test_stage_transient_download_retries_then_succeeds_and_gives_up_into_pause(self):
        rt = self.make({1: zip_entries(1, (), "a")})
        real, fails = rt._download, [DownloadError("partial"), DownloadError("partial")]

        async def flaky(job):
            if fails:
                raise fails.pop(0)
            return await real(job)
        rt.worker.ports.download = flaky
        await drain(rt)
        self.assertEqual(self.status(rt, 1), "COMPLETED"); self.assertEqual(rt.repo.get(f"{CH}:1")["retry_count"], 2)

        rt2 = self.make({1: zip_entries(1, (), "a")})
        async def always(job): raise DownloadError("partial")
        rt2.worker.ports.download = always
        await drain(rt2)
        j = rt2.repo.get(f"{CH}:1")
        self.assertEqual(j["status"], "PAUSED"); self.assertIn("retries ke baad", j["paused_reason"])

    async def test_imgbb_transient_http_500_retried_by_client(self):
        rt = self.make({1: zip_entries(1, (), "a")})
        calls = {"n": 0}
        def flaky(u, h, d):
            calls["n"] += 1
            return Response(500, {}, b"oops") if calls["n"] == 1 else rt._imgbb(u, h, d)
        rt.http.on("POST", "https://api.imgbb.com/", flaky)
        await drain(rt)
        self.assertEqual(self.status(rt, 1), "COMPLETED"); self.assertEqual(calls["n"], 2)

    async def test_imgbb_rate_limit_waits_without_bypass_then_continues(self):
        rt = self.make({1: zip_entries(2, (), "a")})
        naps = []
        async def nap(s): naps.append(s)
        rt.worker._nap = nap
        n = {"c": 0}
        def limited(u, h, d):
            n["c"] += 1
            return Response(429, {}, b"{}") if n["c"] <= 2 else rt._imgbb(u, h, d)
        rt.http.on("POST", "https://api.imgbb.com/", limited)
        await drain(rt)
        self.assertEqual(self.status(rt, 1), "COMPLETED")
        self.assertTrue(any(s >= 60 for s in naps))  # service ne roka => lamba wait
        self.assertEqual(rt.imgbb_calls, ["a_1.png", "a_2.png"])

    async def test_bad_imgbb_key_pauses_not_loops(self):
        rt = self.make({1: zip_entries(1, (), "a")})
        rt.http.on("POST", "https://api.imgbb.com/", lambda u, h, d: jresp({"error": {"message": "Invalid API v1 key."}}, 400))
        await drain(rt)
        self.assertEqual(self.status(rt, 1), "PAUSED"); self.assertIn("API key", rt.repo.get(f"{CH}:1")["paused_reason"])


class Runtime_(Base):
    async def test_second_worker_blocked_by_lease(self):
        rt = self.make({1: zip_entries(1, (), "a")})
        self.assertTrue(D.Lease(rt.db, owner="other", ttl=3600).acquire())
        self.assertFalse(await rt.worker.run_once())
        self.assertEqual(rt.downloads, []); self.assertEqual(rt.repo.list(), [])

    async def test_unexpected_crash_never_kills_loop_pauses_after_limit(self):
        rt = self.make({1: zip_entries(1, (), "a")})
        async def boom(aid): raise ValueError("bug")
        rt.worker._stage_images = boom
        for _ in range(5):
            self.assertTrue(await rt.worker.run_once())
        j = rt.repo.get(f"{CH}:1")
        self.assertEqual(j["status"], "PAUSED"); self.assertIn("Unexpected error", j["paused_reason"])

    async def test_scan_failure_does_not_stop_processing(self):
        rt = self.make({1: zip_entries(1, (10,), "a")})
        rt.channel.replies_per_send.extend([L("A1")])
        await rt.worker.run_once()  # discover + process
        rt2 = self.make({1: zip_entries(1, (10,), "a")})
        rt2.channel.replies_per_send.extend([L("A1")])
        async def broken(): raise ConnectionError("telegram down")
        rt2.repo.discover(CH, 1, "z.zip", 1); rt2.repo.queue_discovered()
        rt2.worker.ports.scan = broken
        self.assertTrue(await rt2.worker.run_once())
        self.assertEqual(self.status(rt2, 1), "COMPLETED")

    async def test_graceful_stop_keeps_state_and_next_worker_resumes(self):
        rt = self.make({1: zip_entries(1, (10,), "a")})
        rt.channel.replies_per_send.extend([L("A1")])
        orig = rt.worker._stage_extract
        async def then_stop(job):
            await orig(job); rt.worker.stop()
        rt.worker._stage_extract = then_stop
        await rt.worker.run_once()
        self.assertEqual(self.status(rt, 1), "EXTRACTING"); self.assertEqual(rt.dest.sent, [])
        rt.worker = rt.new_worker()
        await drain(rt)
        self.assertEqual(self.status(rt, 1), "COMPLETED"); self.assertEqual(rt.downloads, [1])

    async def test_run_forever_stops_on_event_and_releases_lease(self):
        rt = self.make({})
        task = asyncio.create_task(rt.worker.run_forever())
        await asyncio.sleep(0.05)
        rt.worker.stop()
        await asyncio.wait_for(task, 2)
        self.assertIsNone(rt.worker.lease.holder())


if __name__ == "__main__":
    unittest.main()
