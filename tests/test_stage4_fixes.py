"""Koyeb ke pehle real run se mile fixes."""
import asyncio
import contextlib
import io
import logging
import os
import tempfile
import unittest
from unittest import mock

from src import app
from src import db as D
from src.jobs import JobError, JobRepo
from src.worker import explain, needs_human
from tests.stage2_fakes import CH, FakeDest, Runtime, zip_entries
from tests.test_diskwala_uploader import FakeChannel, text_reply


class YouBlockedUserError(Exception):
    pass


class UserDeactivatedBanError(Exception):
    pass


class BlockedChannel(FakeChannel):
    def __init__(self, *a, exc=YouBlockedUserError, **kw):
        super().__init__(*a, **kw); self.exc, self.text_calls = exc, 0

    async def send_text(self, text):
        self.text_calls += 1
        raise self.exc("You blocked this user (caused by SendMessageRequest)")


class Base(unittest.IsolatedAsyncioTestCase):
    def make(self, *a, **kw):
        rt = Runtime(*a, **kw); self.addCleanup(rt.close); return rt


class BlockedBot(Base):
    async def test_blocked_bot_pauses_immediately_with_clear_instruction_not_crash_loop(self):
        rt = self.make({1: zip_entries(2, (30, 10), "a"), 2: zip_entries(1, (10,), "b")})
        ch = BlockedChannel(rt.clock, history=False)  # chat khaali => prepare() /start bhejta hai
        rt.worker = rt.new_worker(ch)
        self.assertTrue(await rt.worker.run_once())
        j = rt.repo.get(f"{CH}:1")
        self.assertEqual(j["status"], "PAUSED")
        self.assertEqual(ch.text_calls, 1)  # 5 baar retry nahi
        for want in ("YouBlockedUserError", "BLOCK", "Unblock", "resume", "TELEGRAM_SESSION_STRING"):
            self.assertIn(want, j["paused_reason"])
        self.assertNotIn("Unexpected", j["paused_reason"])
        self.assertEqual(rt.repo.get(f"{CH}:2")["status"], "QUEUED")  # queue ruki, ZIP2 shuru nahi
        self.assertFalse(await rt.worker.run_once())
        # images/article pehle ho chuke the: unblock + resume ke baad dobara nahi hote
        imgs_before = list(rt.imgbb_calls); n_imgs, n_pages = len(imgs_before), len(rt.tg.pages)
        ch2 = FakeChannel(rt.clock, [[(2, text_reply("Link: https://dw.example/1", to_current=True))],
                                      [(2, text_reply("Link: https://dw.example/2", to_current=True))],
                                      [(2, text_reply("Link: https://dw.example/3", to_current=True))]])
        rt.repo.resume(f"{CH}:1"); rt.worker = rt.new_worker(ch2)
        while await rt.worker.run_once():
            pass
        self.assertEqual(rt.repo.get(f"{CH}:1")["status"], "COMPLETED")
        self.assertEqual(rt.imgbb_calls[:n_imgs], imgs_before); self.assertEqual(rt.imgbb_calls[n_imgs:], ["b_1.png"])  # ZIP1 ki images dobara nahi, sirf ZIP2 ki
        self.assertEqual(len(rt.tg.pages), n_pages + 1)

    async def test_other_account_level_errors_also_pause_with_hint(self):
        rt = self.make({1: zip_entries(1, (10,), "a")})
        rt.worker = rt.new_worker(BlockedChannel(rt.clock, history=False, exc=UserDeactivatedBanError))
        await rt.worker.run_once()
        j = rt.repo.get(f"{CH}:1")
        self.assertEqual(j["status"], "PAUSED"); self.assertIn("banned", j["paused_reason"])

    async def test_unknown_errors_still_use_crash_guard_not_instant_pause(self):
        rt = self.make({1: zip_entries(1, (10,), "a")})
        class Weird(Exception): pass
        rt.worker = rt.new_worker(BlockedChannel(rt.clock, history=False, exc=Weird))
        await rt.worker.run_once()
        self.assertNotEqual(rt.repo.get(f"{CH}:1")["status"], "PAUSED")  # 5 koshish ke baad hi

    def test_helpers(self):
        self.assertTrue(needs_human(YouBlockedUserError()) and needs_human(type("PeerFloodError", (Exception,), {})()))
        self.assertFalse(needs_human(ValueError()))
        self.assertEqual(explain("plain"), "plain")
        self.assertIn("Unblock", explain("DiskWala: x.mp4: YouBlockedUserError: nope"))


class Logging(Base):
    async def test_progress_numbering_is_per_zip_and_stage_logs_exist(self):
        rt = self.make({1: zip_entries(7, (30,), "a")}, cfg_kw={"image_chunk": 5})
        rt.channel.replies_per_send.extend([[(2, text_reply("Link: https://dw.example/1", to_current=True))]])
        with self.assertLogs("worker", level="INFO") as cm:
            await rt.worker.run_once()
        out = "\n".join(cm.output)
        for k in range(1, 8):
            self.assertIn(f"[{k}/7]", out)  # [1/5] baar-baar nahi
        self.assertNotIn("/5]", out)
        for want in ("Telegraph article (verified): https://telegra.ph/", "1 video(s), bada pehle: a_v0.mp4", "1 post(s) tayyar", "1 post(s) publish ho gaye"):
            self.assertIn(want, out)



class SlowChannel(FakeChannel):
    """Reply nahi aata aur har poll asli time leta hai: video-wait ke beech job cancel karne ke liye."""
    async def fetch_replies(self, after_id):
        await asyncio.sleep(0.2)
        return []


class Cancel(Base):
    def add(self, repo, *mids):
        for m in mids:
            repo.discover(CH, m, f"z{m}.zip", 1)
        repo.queue_discovered()

    def test_repo_cancel_rules(self):
        db = D.Database(":memory:"); D.migrate(db); repo = JobRepo(db); self.add(repo, 1, 2, 3)
        self.assertEqual(repo.cancel(f"{CH}:1"), "QUEUED")
        j = repo.get(f"{CH}:1")
        self.assertEqual((j["status"], j["stage"], j["error"]), ("FAILED", "CANCELLED", "Cancelled by user"))
        self.assertEqual(repo.next_job().job["message_id"], 2)  # queue aage
        repo.pause(f"{CH}:2", "stuck")  # PAUSED ZIP queue rok rahi thi
        self.assertIsNone(repo.next_job().job)
        repo.cancel(f"{CH}:2")
        self.assertEqual(repo.next_job().job["message_id"], 3)  # cancel se queue khuli
        repo.complete(f"{CH}:3")
        for bad in (f"{CH}:1", f"{CH}:3", f"{CH}:99"):
            with self.assertRaises(JobError):
                repo.cancel(bad)
        repo.requeue(f"{CH}:1")  # wapas aa sakti hai
        self.assertEqual(repo.next_job().job["message_id"], 1)

    async def test_cancel_running_job_stops_it_mid_video_wait_and_queue_moves_on(self):
        rt = self.make({1: zip_entries(1, (30,), "a"), 2: zip_entries(1, (10,), "b")}, cfg_kw={"lease_ttl": 0.3})
        rt.worker = rt.new_worker(SlowChannel(rt.clock))
        task = asyncio.create_task(rt.worker.run_once())
        for _ in range(100):  # job video stage (wait) me pahunche
            await asyncio.sleep(0.05)
            j = rt.repo.get(f"{CH}:1")
            if j and j["status"] == "UPLOADING_VIDEOS":
                break
        await asyncio.sleep(0.3)
        self.assertFalse(task.done())  # abhi wait chal raha hai
        rt.repo.cancel(f"{CH}:1")
        self.assertTrue(await asyncio.wait_for(task, 5))  # worker crash nahi hua, loop chalta rahega
        j = rt.repo.get(f"{CH}:1")
        self.assertEqual((j["status"], j["error"]), ("FAILED", "Cancelled by user"))
        self.assertIsNone(rt.worker.current)
        self.assertEqual(len(rt.dest.sent), 0)  # adhoora post publish nahi hua
        vid = rt.db.query_one("SELECT data FROM videos WHERE archive_id=?", (f"{CH}:1",))
        import json
        self.assertEqual(json.loads(vid["data"])["status"], "interrupted")  # sent id saved: requeue par dobara nahi bhejega
        # agla ZIP ab chalta hai
        rt.worker = rt.new_worker(FakeChannel(rt.clock, [[(2, text_reply("Link: https://dw.example/b", to_current=True))]]))
        while await rt.worker.run_once():
            pass
        self.assertEqual(rt.repo.get(f"{CH}:2")["status"], "COMPLETED")
        self.assertEqual(rt.repo.get(f"{CH}:1")["status"], "FAILED")

    def test_cli_cancel(self):
        tmp = tempfile.mkdtemp()
        env = {"WORKSPACE_DIR": tmp, "DATABASE_URL": f"sqlite:///{tmp}/c.db"}
        with mock.patch.dict(os.environ, env, clear=True):
            db = D.Database(env["DATABASE_URL"]); D.migrate(db); repo = JobRepo(db); self.add(repo, 1); db.close()

            def run(*argv):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    return app.main(list(argv)), out.getvalue()
            code, out = run("cancel", f"{CH}:1")  # -100... wala ID positional bhi chalta hai
            self.assertEqual(code, 0); self.assertIn("cancel ho gayi", out); self.assertIn("delete nahi", out)
            self.assertEqual(run("cancel", f"{CH}:1")[0], 2)  # dobara: clean error
            self.assertEqual(run("cancel", f"--archive-id={CH}:77")[0], 2)
            self.assertEqual(run("requeue", f"{CH}:1")[0], 0)


if __name__ == "__main__":
    unittest.main()
