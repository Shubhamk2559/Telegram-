import json
import tempfile
import unittest
from pathlib import Path

from src.diskwala_uploader import (
    BotReply, ControlFile, DiskWalaUploader, UploaderConfig,
)
from src.upload_store import UploadStore, collect_videos


class FloodWaitError(Exception):  # naam Telethon jaisa => flood_seconds() pehchanta hai
    def __init__(self, seconds): super().__init__(f"wait {seconds}"); self.seconds = seconds


class PeerFloodError(Exception):
    pass


class Clock:
    def __init__(self): self.t = 0.0; self.sleeps = []
    def now(self): return self.t
    async def sleep(self, s): self.sleeps.append(s); self.t += s


class FakeChannel:
    """Mocked bot chat. script: har send ke baad kya replies aayenge."""
    def __init__(self, clock, replies_per_send=None, send_errors=None, already_sent=None, history=True):
        self.clock = clock
        self.replies_per_send = list(replies_per_send or [])  # list of lists of (delay, reply-factory)
        self.send_errors = list(send_errors or [])
        self.already_sent = already_sent
        self.next_id = 100 if history else 0
        self.sent = []; self.texts = []
        self.visible = []  # (visible_at, BotReply)

    async def latest_id(self): return self.next_id
    async def send_text(self, text):
        self.texts.append(text); self.next_id += 1; return self.next_id

    async def send_video(self, path, progress):
        if self.send_errors:
            err = self.send_errors.pop(0)
            if err: raise err
        self.next_id += 1; sid = self.next_id; self.sent.append((Path(path).name, sid))
        script = self.replies_per_send.pop(0) if self.replies_per_send else []
        for delay, make in script:
            self.next_id += 1
            self.visible.append((self.clock.now() + delay, make(self.next_id, sid)))
        return sid

    async def fetch_replies(self, after_id):
        return [r for at, r in self.visible if at <= self.clock.now() and r.id > after_id]

    async def find_recent_outgoing(self, after_id, size):
        return self.already_sent


def text_reply(text, to_current=False, to=None):
    return lambda rid, sid: BotReply(id=rid, text=text, reply_to_id=(sid if to_current else to))


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clock = Clock()
        self.store = UploadStore(self.tmp / "video_uploads.json", "TestBot")

    def tearDown(self):
        self._tmp.cleanup()

    def video(self, name="a.mp4", size=10, file_id=None, make=True):
        p = self.tmp / name
        if make:
            p.write_bytes(b"x" * size)
        v = {"file_id": file_id or name, "filename": name, "size": size, "path": str(p), "archive_id": "c:1"}
        self.store.sync([v])
        return self.store.get(v["file_id"])

    def uploader(self, channel, **kw):
        cfg = UploaderConfig(response_timeout=kw.pop("timeout", 30), poll_interval=2, delay_between=0,
                             max_retries=kw.pop("max_retries", 2), max_upload_bytes=kw.pop("max_bytes", 10_000),
                             link_domains=kw.pop("domains", ()))
        return DiskWalaUploader(channel, self.store, cfg, sleep=self.clock.sleep, now=self.clock.now, **kw)

    def reload(self):
        return UploadStore(self.tmp / "video_uploads.json", "TestBot")


class UploadFlowTests(Base):
    async def test_success_saved_immediately_and_survives_reload(self):
        rec = self.video()
        ch = FakeChannel(self.clock, [[(4, text_reply("Link: https://dw.example/abc", to_current=True))]])
        await self.uploader(ch).upload_one(rec)
        self.assertEqual(rec["status"], "completed")
        again = self.reload().get("a.mp4")
        self.assertEqual((again["status"], again["url"], again["match"]), ("completed", "https://dw.example/abc", "reply_to"))
        self.assertIsNone(again["error"])

    async def test_progress_message_then_link(self):
        rec = self.video()
        ch = FakeChannel(self.clock, [[(1, text_reply("Processing...")), (9, text_reply("Done https://dw.example/v1"))]])
        await self.uploader(ch).upload_one(rec)
        self.assertEqual(rec["url"], "https://dw.example/v1")
        self.assertEqual(rec["match"], "sequential")
        self.assertFalse(rec["needs_review"])

    async def test_timeout_no_reply(self):
        rec = self.video()
        await self.uploader(FakeChannel(self.clock, [[]]), timeout=20).upload_one(rec)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("Timeout", rec["error"])
        self.assertGreaterEqual(self.clock.t, 20)

    async def test_reply_without_link_is_explained(self):
        rec = self.video()
        ch = FakeChannel(self.clock, [[(1, text_reply("Your file is being processed"))]])
        await self.uploader(ch, timeout=10).upload_one(rec)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("valid http(s) link nahi mila", rec["error"])
        self.assertIn("being processed", rec["error"])

    async def test_reply_to_other_message_is_never_attached(self):
        rec = self.video()
        ch = FakeChannel(self.clock, [[(1, text_reply("old https://wrong.example/old", to=5))]])
        await self.uploader(ch, timeout=10).upload_one(rec)
        self.assertEqual(rec["status"], "failed")
        self.assertIsNone(rec["url"])

    async def test_bot_error_text_fails_without_waiting_full_timeout(self):
        rec = self.video()
        ch = FakeChannel(self.clock, [[(1, text_reply("Error: unsupported file format"))]])
        await self.uploader(ch, timeout=100).upload_one(rec)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("unsupported", rec["error"])
        self.assertLess(self.clock.t, 10)

    async def test_late_reply_after_timeout_flags_review(self):
        r1, r2 = self.video("a.mp4"), self.video("b.mp4")
        ch = FakeChannel(self.clock, [[], [(1, text_reply("https://dw.example/late"))]])
        up = self.uploader(ch, timeout=6)
        await up.run([r1, r2])
        self.assertEqual(r1["status"], "failed")
        self.assertEqual(r2["status"], "completed")
        self.assertTrue(r2["needs_review"])

    async def test_domain_filter_rejects_promo_links(self):
        rec = self.video()
        ch = FakeChannel(self.clock, [[(1, text_reply("Join https://t.me/promo"))]])
        await self.uploader(ch, timeout=6, domains=("dw.example",)).upload_one(rec)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("DISKWALA_LINK_DOMAINS", rec["error"])


class RetryTests(Base):
    async def test_floodwait_waits_then_sends_once(self):
        rec = self.video()
        ch = FakeChannel(self.clock, [[(1, text_reply("https://dw.example/f"))]], send_errors=[FloodWaitError(5)])
        await self.uploader(ch).upload_one(rec)
        self.assertEqual(rec["status"], "completed")
        self.assertIn(6, self.clock.sleeps)
        self.assertEqual(len(ch.sent), 1)

    async def test_floodwait_too_long_stops_batch(self):
        r1, r2 = self.video("a.mp4"), self.video("b.mp4")
        ch = FakeChannel(self.clock, send_errors=[FloodWaitError(5000)])
        summary = await self.uploader(ch).run([r1, r2])
        self.assertEqual(r1["status"], "failed")
        self.assertEqual(r2["status"], "pending")
        self.assertIn("FloodWait", summary.stopped_reason)

    async def test_transient_error_but_file_already_arrived_no_duplicate(self):
        rec = self.video()
        ch = FakeChannel(self.clock, send_errors=[ConnectionError("reset")], already_sent=150)
        ch.visible.append((0, BotReply(id=151, text="https://dw.example/ok", reply_to_id=150)))
        await self.uploader(ch).upload_one(rec)
        self.assertEqual(ch.sent, [])  # dobara send nahi hua
        self.assertEqual(rec["status"], "completed")

    async def test_transient_error_not_sent_retries(self):
        rec = self.video()
        ch = FakeChannel(self.clock, [[(1, text_reply("https://dw.example/r"))]], send_errors=[ConnectionError("x")])
        await self.uploader(ch).upload_one(rec)
        self.assertEqual(rec["status"], "completed")
        self.assertEqual(len(ch.sent), 1)

    async def test_retries_exhausted(self):
        rec = self.video()
        ch = FakeChannel(self.clock, send_errors=[ConnectionError("x")] * 5)
        await self.uploader(ch, max_retries=2).upload_one(rec)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("retry", rec["error"])
        self.assertEqual(len(ch.sent), 0)

    async def test_peerflood_is_fatal_and_not_retried(self):
        r1, r2 = self.video("a.mp4"), self.video("b.mp4")
        ch = FakeChannel(self.clock, send_errors=[PeerFloodError()])
        s = await self.uploader(ch).run([r1, r2])
        self.assertEqual((r1["status"], r2["status"]), ("failed", "pending"))
        self.assertTrue(s.stopped_reason)


class GuardTests(Base):
    async def test_too_big_and_missing_file_never_sent(self):
        big = self.video("big.mp4", size=500)
        gone = self.video("gone.mp4", make=False)
        ch = FakeChannel(self.clock)
        await self.uploader(ch, max_bytes=100).run([big, gone])
        self.assertEqual((big["status"], gone["status"]), ("failed", "failed"))
        self.assertIn("limit", big["error"])
        self.assertEqual(ch.sent, [])

    async def test_rate_limit_text_stops_batch(self):
        r1, r2 = self.video("a.mp4"), self.video("b.mp4")
        ch = FakeChannel(self.clock, [[(1, text_reply("Daily limit reached"))]])
        s = await self.uploader(ch).run([r1, r2])
        self.assertEqual((r1["status"], r2["status"]), ("failed", "pending"))
        self.assertIn("bypass nahi", s.stopped_reason)

    async def test_manual_step_then_link(self):
        rec = self.video()
        ch = FakeChannel(self.clock, [[(1, text_reply("Please solve the captcha")), (16, text_reply("https://dw.example/m"))]])
        calls = []
        async def manual(r): calls.append(r.text); return "continue"
        await self.uploader(ch, on_manual=manual, timeout=15).upload_one(rec)
        self.assertEqual(len(calls), 1)
        self.assertEqual(rec["status"], "completed")

    async def test_manual_quit_marks_interrupted(self):
        rec = self.video()
        ch = FakeChannel(self.clock, [[(1, text_reply("Join our channel first"))]])
        await self.uploader(ch).upload_one(rec)  # default handler = quit
        self.assertEqual(rec["status"], "interrupted")

    async def test_manual_with_link_user_can_accept(self):
        rec = self.video()
        ch = FakeChannel(self.clock, [[(1, text_reply("Link https://dw.example/z . Join our channel for updates"))]])
        async def manual(r): return "accept"
        await self.uploader(ch, on_manual=manual).upload_one(rec)
        self.assertEqual((rec["status"], rec["match"]), ("completed", "user_confirmed"))

    async def test_prepare_sends_start_only_when_chat_empty(self):
        ch = FakeChannel(self.clock, history=False)
        ch.visible.append((0, BotReply(id=1, text="Welcome! Send me a video")))
        await self.uploader(ch).prepare()
        self.assertEqual(ch.texts, ["/start"])
        ch2 = FakeChannel(self.clock, history=True)
        await self.uploader(ch2).prepare()
        self.assertEqual(ch2.texts, [])


class ControlTests(Base):
    async def test_stop_prevents_any_upload(self):
        rec = self.video()
        ctl = ControlFile(self.tmp / "ctl.txt"); ctl.write("stop")
        ch = FakeChannel(self.clock)
        s = await self.uploader(ch, control=ctl).run([rec])
        self.assertEqual(ch.sent, [])
        self.assertEqual(rec["status"], "pending")
        self.assertIn("stop", s.stopped_reason)

    async def test_pause_then_resume(self):
        rec = self.video()
        ctl = ControlFile(self.tmp / "ctl.txt"); ctl.write("pause")
        ch = FakeChannel(self.clock, [[(1, text_reply("https://dw.example/p"))]])
        up = self.uploader(ch, control=ctl)
        orig = up.sleep
        async def sleep_and_resume(s):
            await orig(s)
            if self.clock.t >= 6:
                ctl.write("run")
        up.sleep = sleep_and_resume
        await up.run([rec])
        self.assertEqual(rec["status"], "completed")
        self.assertGreaterEqual(self.clock.t, 6)


class PersistenceTests(Base):
    def test_collect_videos_only_completed_archives_videos_dedup(self):
        data = {"archives": [
            {"archive_id": "c:1", "status": "completed", "extraction_dir": "extracted/x", "files": [
                {"file_id": "v1", "type": "video", "filename": "a.mp4", "size": 5, "path": "extracted/x/videos/a.mp4", "relative_path": "videos/a.mp4"},
                {"file_id": "i1", "type": "image", "filename": "a.jpg", "size": 5, "path": "p", "relative_path": "images/a.jpg"}]},
            {"archive_id": "c:2", "status": "completed", "extraction_dir": "extracted/y", "files": [
                {"file_id": "v1", "type": "video", "filename": "a.mp4", "size": 5, "relative_path": "videos/a.mp4"},
                {"file_id": "v2", "type": "video", "filename": "b.mp4", "size": 6, "relative_path": "videos/b.mp4"}]},
            {"archive_id": "c:3", "status": "failed", "files": [
                {"file_id": "v9", "type": "video", "filename": "z.mp4", "size": 1, "relative_path": "videos/z.mp4"}]},
        ]}
        vids = collect_videos(data)
        self.assertEqual([v["file_id"] for v in vids], ["v1", "v2"])
        self.assertEqual(vids[1]["path"], "extracted/y/videos/b.mp4")

    async def test_restart_resumes_pending_only_and_flags_inflight(self):
        done, pend, mid = self.video("done.mp4"), self.video("pend.mp4"), self.video("mid.mp4")
        self.store.update(done, status="completed", url="https://dw.example/d")
        self.store.update(mid, status="awaiting_response")  # crash ke waqt
        s2 = self.reload()
        self.assertEqual(s2.get("mid.mp4")["status"], "interrupted")
        self.assertEqual(s2.recovered, ["mid.mp4"])
        self.assertEqual([r["filename"] for r in s2.select_work()], ["pend.mp4"])
        self.assertEqual([r["filename"] for r in s2.select_work(retry_interrupted=True)], ["pend.mp4", "mid.mp4"])
        self.assertEqual(s2.get("done.mp4")["url"], "https://dw.example/d")

    def test_sync_does_not_reset_existing_records(self):
        rec = self.video()
        self.store.update(rec, status="completed", url="https://dw.example/k")
        self.assertEqual(self.store.sync([{"file_id": "a.mp4", "filename": "a.mp4", "size": 10, "path": "p"}]), 0)
        self.assertEqual(self.reload().get("a.mp4")["status"], "completed")

    def test_json_file_valid_and_corrupt_is_backed_up(self):
        self.video()
        data = json.loads((self.tmp / "video_uploads.json").read_text())
        self.assertEqual(data["files"][0]["status"], "pending")
        (self.tmp / "video_uploads.json").write_text("{bad")
        from src.upload_store import UploadStoreError
        with self.assertRaises(UploadStoreError):
            self.reload()

    async def test_each_video_saved_before_next_starts(self):
        r1, r2 = self.video("a.mp4"), self.video("b.mp4")
        ch = FakeChannel(self.clock, [[(1, text_reply("https://dw.example/1", to_current=True))], []])
        snap = {}
        up = self.uploader(ch, timeout=4)
        orig_send = ch.send_video
        async def spy(path, progress):
            if Path(path).name == "b.mp4":
                snap["a"] = self.reload().get("a.mp4")["status"]
            return await orig_send(path, progress)
        ch.send_video = spy
        await up.run([r1, r2])
        self.assertEqual(snap["a"], "completed")  # b shuru hone se pehle a disk par saved tha


if __name__ == "__main__":
    unittest.main()
