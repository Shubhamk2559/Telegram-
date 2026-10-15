"""Flezen jaisa bot (late reply, "Downloading..." message, video ko reply nahi, link ke baad delete) + bot switch."""
import unittest
from dataclasses import replace

from src.config import ConfigError
from src.diskwala_uploader import BotReply, DiskWalaUploader, UploaderConfig
from src.strict_videos import StrictVideoUploader, VideoPolicy
from src.upload_bots import DISKWALA, FLEZEN, active_key, build_profiles, set_active
from src.upload_store import AWAITING, COMPLETED, FAILED, PENDING
from tests.test_diskwala_uploader import Base, FakeChannel, text_reply

PROGRESS = "Downloading the file, please wait..."
LINK = "Here is your link: https://flezen.example/f/abc123"


class DeletingChannel(FakeChannel):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.deleted = []

    async def delete_messages(self, ids):
        self.deleted.append(list(ids))


class FakeRepo:
    def __init__(self):
        self.kv = {}

    def get_kv(self, key, default=None):
        return self.kv.get(key, default)

    def set_kv(self, key, value):
        self.kv[key] = value


def flezen_cfg(**kw):
    base = dict(bot_username="FlezenUploadBot", response_timeout=900, poll_interval=3, delay_between=0,
                progress_re=r"downloading (the )?file", reply_to_video=False)
    base.update(kw)
    return UploaderConfig(**base)


class FlezenFlow(Base):
    def uploader(self, ch, **kw):
        return DiskWalaUploader(ch, self.store, flezen_cfg(**kw), sleep=self.clock.sleep, now=self.clock.now)

    async def test_progress_then_link_not_replying_to_video(self):
        ch = DeletingChannel(self.clock, [[(5, text_reply(PROGRESS, to_current=True)), (120, text_reply(LINK))]])
        rec = self.video()
        status = await self.uploader(ch).upload_one(rec)
        self.assertEqual(status, COMPLETED)
        self.assertEqual(rec["url"], "https://flezen.example/f/abc123")
        self.assertEqual(rec["match"], "progress")          # hamari file -> Downloading -> link
        self.assertFalse(rec["needs_review"])
        self.assertEqual(rec["bot"], "FlezenUploadBot")

    async def test_cleanup_deletes_video_progress_and_link(self):
        ch = DeletingChannel(self.clock, [[(5, text_reply(PROGRESS)), (60, text_reply(LINK))]])
        rec = self.video()
        await self.uploader(ch).upload_one(rec)
        sent_id = ch.sent[0][1]
        self.assertEqual(ch.deleted, [[sent_id, sent_id + 1, sent_id + 2]])

    async def test_slow_reply_needs_long_timeout(self):
        slow = [[(5, text_reply(PROGRESS)), (400, text_reply(LINK))]]
        ok = await self.uploader(DeletingChannel(self.clock, slow)).upload_one(self.video("ok.mp4"))
        self.assertEqual(ok, COMPLETED)

    async def test_progress_message_restarts_timeout(self):
        # timeout 100s: progress 90s par aaya, link 170s par => progress se naapne par 80s hi laga
        ch = DeletingChannel(self.clock, [[(90, text_reply(PROGRESS)), (170, text_reply(LINK))]])
        rec = self.video()
        self.assertEqual(await self.uploader(ch, response_timeout=100).upload_one(rec), COMPLETED)

    async def test_without_progress_the_same_delay_times_out(self):
        ch = DeletingChannel(self.clock, [[(170, text_reply(LINK))]])
        rec = self.video()
        self.assertEqual(await self.uploader(ch, response_timeout=100).upload_one(rec), FAILED)

    async def test_progress_only_is_not_a_link_and_not_an_error(self):
        ch = DeletingChannel(self.clock, [[(5, text_reply(PROGRESS))]])
        rec = self.video()
        status = await self.uploader(ch, response_timeout=60).upload_one(rec)
        self.assertEqual(status, FAILED)
        self.assertIn("link", rec["error"])
        self.assertEqual(ch.deleted, [])  # fail hua to chat se kuch delete nahi

    async def test_link_edited_into_the_progress_message(self):
        ch = DeletingChannel(self.clock, [[(5, text_reply(PROGRESS))]])
        rec = self.video()
        up = self.uploader(ch, response_timeout=300)
        orig = ch.fetch_replies

        async def edited(after_id):  # 50s ke baad wahi message edit hoke link ban jata hai
            out = await orig(after_id)
            if self.clock.now() >= 50:
                out = [BotReply(id=r.id, text=LINK, reply_to_id=None) for r in out]
            return out
        ch.fetch_replies = edited
        self.assertEqual(await up.upload_one(rec), COMPLETED)
        self.assertEqual(rec["match"], "progress")

    async def test_real_chat_21_minute_link(self):
        ch = DeletingChannel(self.clock, [[(1, text_reply(PROGRESS, to_current=True)), (1260, text_reply(LINK))]])
        rec = self.video()
        self.assertEqual(await self.uploader(ch, response_timeout=3600).upload_one(rec), COMPLETED)

    async def test_size_limit_message_is_a_bot_error(self):
        msg = "I can only upload files smaller than 1024.0 MB, please use website to upload larger files."
        ch = DeletingChannel(self.clock, [[(2, text_reply(msg))]])
        rec = self.video()
        self.assertEqual(await self.uploader(ch).upload_one(rec), FAILED)
        self.assertLess(self.clock.now(), 60)  # ghante bhar wait nahi kiya

    async def test_file_over_limit_not_even_sent(self):
        ch = DeletingChannel(self.clock, [])
        rec = self.video(size=2000)
        self.assertEqual(await self.uploader(ch, max_upload_bytes=1000).upload_one(rec), FAILED)
        self.assertEqual(ch.sent, [])

    async def test_diskwala_style_bot_keeps_old_cleanup(self):
        """reply_to_video=True (DiskWala): sirf is video ke reply delete hote hain, bina-reply messages nahi."""
        ch = DeletingChannel(self.clock, [[(5, text_reply("promo message")), (10, text_reply(LINK, to_current=True))]])
        cfg = UploaderConfig(bot_username="DiskWalaFileUploaderBot", response_timeout=60, delay_between=0)
        rec = self.video()
        up = DiskWalaUploader(ch, self.store, cfg, sleep=self.clock.sleep, now=self.clock.now)
        self.assertEqual(await up.upload_one(rec), COMPLETED)
        sent_id = ch.sent[0][1]
        self.assertEqual(ch.deleted, [[sent_id, sent_id + 2]])


class Switching(Base):
    def strict(self, ch, name):
        cfg = flezen_cfg(bot_username=name, response_timeout=60)
        return StrictVideoUploader(ch, self.store, cfg, policy=VideoPolicy(max_attempts=1),
                                   sleep=self.clock.sleep, now=self.clock.now)

    async def test_switch_resets_video_stuck_on_other_bot(self):
        rec = self.video()
        self.store.update(rec, status=AWAITING, sent_message_id=555, bot="DiskWalaFileUploaderBot")
        ch = DeletingChannel(self.clock, [[(5, text_reply(PROGRESS)), (20, text_reply(LINK))]])
        up = self.strict(ch, "FlezenUploadBot")
        await up.process_all([rec])
        self.assertEqual(rec["status"], COMPLETED)
        self.assertEqual(len(ch.sent), 1)                      # naye bot ko fresh bheja
        self.assertEqual(rec["bot"], "FlezenUploadBot")
        self.assertNotEqual(rec["sent_message_id"], 555)

    async def test_same_bot_still_recovers_without_resend(self):
        rec = self.video()
        sent = 110
        self.store.update(rec, status=AWAITING, sent_message_id=sent, bot="FlezenUploadBot")
        ch = DeletingChannel(self.clock, [])
        ch.visible.append((0, BotReply(id=sent + 1, text=LINK)))
        await self.strict(ch, "FlezenUploadBot").process_all([rec])
        self.assertEqual(rec["status"], COMPLETED)
        self.assertEqual(ch.sent, [])                          # dobara nahi bheja

    async def test_completed_video_is_never_touched_by_switch(self):
        rec = self.video()
        self.store.update(rec, status=COMPLETED, url="https://old/x", urls=["https://old/x"], bot="DiskWalaFileUploaderBot")
        ch = DeletingChannel(self.clock, [])
        await self.strict(ch, "FlezenUploadBot").process_all([rec])
        self.assertEqual((rec["status"], rec["url"], ch.sent), (COMPLETED, "https://old/x", []))


class Profiles(unittest.TestCase):
    def build(self, env=None):
        base = UploaderConfig(bot_username="DiskWalaFileUploaderBot", response_timeout=1800, link_domains=("diskwala.com",))
        return build_profiles(env or {}, base, VideoPolicy())

    def test_defaults(self):
        profiles, default = self.build()
        self.assertEqual(default, DISKWALA)
        f = profiles[FLEZEN]
        self.assertEqual(f.username, "FlezenUploadBot")
        self.assertFalse(f.uploader.reply_to_video)
        self.assertEqual(f.uploader.response_timeout, 3600.0)
        self.assertEqual(f.uploader.max_upload_bytes, 1000 * 1048576)
        self.assertFalse(f.policy.resend_on_timeout)
        self.assertEqual(f.uploader.link_domains, ())            # DiskWala ka domain filter Flezen par nahi
        self.assertTrue(profiles[DISKWALA].uploader.reply_to_video)
        self.assertEqual(profiles[DISKWALA].uploader.link_domains, ("diskwala.com",))

    def test_env_overrides(self):
        profiles, default = self.build({"FLEZEN_BOT_USERNAME": "@MyFlezen", "FLEZEN_RESPONSE_TIMEOUT_SECONDS": "1200",
                                        "UPLOAD_BOT_DEFAULT": "flezen", "FLEZEN_LINK_DOMAINS": "a.com, b.com"})
        self.assertEqual(default, FLEZEN)
        self.assertEqual(profiles[FLEZEN].username, "MyFlezen")
        self.assertEqual(profiles[FLEZEN].uploader.response_timeout, 1200.0)
        self.assertEqual(profiles[FLEZEN].uploader.link_domains, ("a.com", "b.com"))

    def test_bad_values(self):
        with self.assertRaises(ConfigError):
            self.build({"UPLOAD_BOT_DEFAULT": "nope"})
        with self.assertRaises(ConfigError):
            self.build({"FLEZEN_RESPONSE_TIMEOUT_SECONDS": "abc"})

    def test_active_bot_persisted(self):
        profiles, default = self.build()
        repo = FakeRepo()
        self.assertEqual(active_key(repo, profiles, default), DISKWALA)
        set_active(repo, profiles, "Flezen")
        self.assertEqual(active_key(repo, profiles, default), FLEZEN)
        repo.kv["upload_bot"] = "garbage"
        self.assertEqual(active_key(repo, profiles, default), DISKWALA)  # kharab value => default
        with self.assertRaises(ConfigError):
            set_active(repo, profiles, "nope")


if __name__ == "__main__":
    unittest.main()
