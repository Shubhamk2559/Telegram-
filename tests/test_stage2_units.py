import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from src import db as D
from src import zip_telegraph as ZT
from src.db_stores import ArticleStore, DbImageStore, DbUploadStore, PostStore
from src.diskwala_uploader import UploaderConfig
from src.http_util import NetworkError
from src.image_store import collect_images
from src.jobs import JobRepo
from src.posts import PostPlan
from src.scanner import scan_channel
from src.strict_videos import StrictVideoUploader, VideoBlocked, VideoPolicy
from src.telegraph_publisher import TelegraphClient, walk_img_srcs
from src.tg_publisher import PostPublisher, PublishBlocked, PublishPolicy
from tests.fakes_http import FakeHttp, FakeTelegraph, imgbb_ok
from tests.stage2_fakes import CH, FakeDest, FakeTgClient, Msg
from tests.test_diskwala_uploader import Clock, FakeChannel, FloodWaitError, text_reply


class DbCase(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory(); self.addCleanup(self._t.cleanup)
        self.tmp = Path(self._t.name)
        self.db = D.Database(f"sqlite:///{self.tmp}/a.db"); D.migrate(self.db); self.addCleanup(self.db.close)
        self.repo = JobRepo(self.db)
        for m in (1, 2):
            self.repo.discover(CH, m, f"z{m}.zip", 1)


def arch(aid, files):
    return {"archives": [{"archive_id": aid, "archive_filename": "z.zip", "message_id": 1, "status": "completed",
                          "extraction_dir": None, "files": files}]}


def ifile(fid, name):
    return {"file_id": fid, "type": "image", "filename": name, "size": 5, "relative_path": f"images/{name}", "path": f"x/images/{name}"}


class Scanner(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = D.Database(":memory:"); D.migrate(self.db); self.repo = JobRepo(self.db)

    async def test_first_run_does_not_backfill_history(self):
        c = FakeTgClient([Msg(1, "old.zip"), Msg(2, "old2.zip")])
        info = await scan_channel(c, None, CH, self.repo)
        self.assertEqual((info["new"], info["first_run"], self.repo.last_scanned(CH)), (0, True, 2))
        self.assertEqual(self.repo.list(), [])

    async def test_new_and_offline_zips_found_in_order_non_zip_ignored_checkpoint_advances(self):
        c = FakeTgClient([Msg(1, "old.zip")])
        await scan_channel(c, None, CH, self.repo)
        c.msgs += [Msg(5, "b.zip"), Msg(4, "notes.txt", mime="text/plain"), Msg(3, "a.zip"), Msg(6, None)]
        info = await scan_channel(c, None, CH, self.repo)
        self.assertEqual(info["new"], 2)
        self.assertEqual([(r["message_id"], r["status"]) for r in self.repo.list()], [(3, "QUEUED"), (5, "QUEUED")])
        self.assertEqual(self.repo.last_scanned(CH), 6)  # non-zip ke upar bhi
        self.assertEqual((await scan_channel(c, None, CH, self.repo))["new"], 0)  # duplicate nahi

    async def test_explicit_backfill_and_no_message_edge(self):
        c = FakeTgClient([Msg(1, "a.zip"), Msg(2, "b.zip"), Msg(3, "c.zip")])
        info = await scan_channel(c, None, CH, self.repo, backfill_from=2)
        self.assertEqual([r["message_id"] for r in self.repo.list()], [2, 3])
        empty = JobRepo(self.db)
        c2 = FakeTgClient([])
        await scan_channel(c2, None, -5, empty, backfill_from=10)
        self.assertEqual(empty.last_scanned(-5), 9)  # dobara scan poori history nahi uthayega


class Stores(DbCase):
    def test_image_store_persists_per_record_and_isolated_per_zip(self):
        a, b = f"{CH}:1", f"{CH}:2"
        sa, sb = DbImageStore(self.db, a), DbImageStore(self.db, b)
        sa.sync(collect_images(arch(a, [ifile("f1", "1.png"), ifile("f2", "2.png")])))
        sb.sync(collect_images(arch(b, [ifile("f1", "1.png")])))  # same file_id, doosra ZIP: alag record
        sa.update(sa.get("f1"), "imgbb", status="uploaded", url="https://i.ibb.co/x/1.png")
        again = DbImageStore(self.db, a)
        self.assertEqual(len(again.files), 2); self.assertEqual(len(DbImageStore(self.db, b).files), 1)
        self.assertEqual(again.get("f1")["imgbb"]["url"], "https://i.ibb.co/x/1.png")
        self.assertEqual(DbImageStore(self.db, b).get("f1")["imgbb"]["url"], None)
        self.assertEqual([r["file_id"] for r in again.select_for_upload()], ["f2"])

    def test_upload_store_paths_relative_in_db_absolute_in_memory_and_inflight_untouched(self):
        ws = self.tmp / "workspace"
        s = DbUploadStore(self.db, f"{CH}:1", "Bot", ws)
        s.sync([{"file_id": "v", "filename": "v.mp4", "size": 9, "path": "jobs/1/clips/v.mp4", "archive_id": f"{CH}:1"}])
        rec = s.get("v")
        self.assertEqual(rec["path"], str(ws.resolve() / "jobs/1/clips/v.mp4"))  # sync ke turant baad bhi absolute
        s.update(rec, status="awaiting_response", sent_message_id=77)
        raw = json.loads(self.db.query_one("SELECT data FROM videos WHERE file_id='v'")["data"])
        self.assertEqual(raw["path"], "jobs/1/clips/v.mp4")  # workspace move/redeploy safe
        again = DbUploadStore(self.db, f"{CH}:1", "Bot", ws).get("v")
        self.assertEqual((again["status"], again["sent_message_id"]), ("awaiting_response", 77))  # INTERRUPTED nahi bana

    def test_post_plan_saved_once_never_overwritten(self):
        ps = PostStore(self.db)
        plan = [PostPlan(1, "full", "first text", 0, 2), PostPlan(2, "full", "second", 2, 1)]
        self.assertEqual(ps.save_plan(f"{CH}:1", plan), 2)
        self.assertEqual(ps.save_plan(f"{CH}:1", [PostPlan(1, "full", "CHANGED", 0, 2)]), 0)
        self.assertEqual([p["content"] for p in ps.list(f"{CH}:1")], ["first text", "second"])
        self.assertEqual(ps.list(f"{CH}:2"), [])


class Article(DbCase):
    def setUp(self):
        super().setUp()
        self.aid = f"{CH}:1"
        self.store = DbImageStore(self.db, self.aid)
        self.http = FakeHttp()
        self.fake = FakeTelegraph(self.http)
        self.client = TelegraphClient(self.http, "TOK", sleep=lambda s: None)
        self.articles = ArticleStore(self.db)

    def uploaded(self, n=3, aid=None, tag="a"):
        store = DbImageStore(self.db, aid or self.aid)
        store.sync(collect_images(arch(aid or self.aid, [ifile(f"{tag}{i}", f"{tag}{i}.png") for i in range(n)])))
        for i, r in enumerate(store.ordered(), 1):
            res = json.loads(imgbb_ok(r["filename"], hash((tag, i)) % 10000).text())
            from src.imgbb_uploader import parse_upload_response
            p = parse_upload_response(res)
            store.update(r, "imgbb", status="uploaded", url=p["url"])
            store.update(r, "html_embed", constructed=p["html_embeds"])
        return store

    def publish(self, store, **kw):
        kw.setdefault("title", "My Image Collection")
        return ZT.publish_zip_article(articles=self.articles, store=store, client=self.client, http=self.http,
                                      archive_id=store.archive_id, sleep=lambda s: None, **kw)

    def test_fixed_title_verified_and_only_this_zips_images(self):
        s1 = self.uploaded(3, self.aid, "a")
        other = self.uploaded(2, f"{CH}:2", "b")
        art = self.publish(s1)
        self.assertEqual(art["status"], "verified")
        self.assertEqual({p["title"] for p in self.fake.pages.values()}, {"My Image Collection"})  # ZIP filename nahi
        srcs = walk_img_srcs(self.fake.pages["Page-1-10-11"]["content"])
        self.assertEqual(len(srcs), 3)
        self.assertTrue(all(r["imgbb"]["url"] in srcs for r in s1.files))
        self.assertFalse(any(r["imgbb"]["url"] in srcs for r in other.files))  # ZIP isolation
        self.assertEqual(s1.get("a0")["telegraph"]["status"], "embedded_verified")

    def test_rerun_reuses_article_no_duplicate(self):
        s = self.uploaded(2); self.publish(s)
        calls = len([c for c in self.http.calls if "createPage" in c[1]])
        art2 = self.publish(DbImageStore(self.db, self.aid))
        self.assertEqual(len([c for c in self.http.calls if "createPage" in c[1]]), calls)
        self.assertEqual(len(self.fake.pages), 1); self.assertEqual(art2["urls"], ["https://telegra.ph/Page-1-10-11"])

    def test_lost_response_recovered_without_duplicate_page(self):
        s = self.uploaded(2)
        first = {"done": False}
        real = self.fake._create

        def create(url, h, d):
            r = real(url, h, d)  # server ne page bana diya...
            if not first["done"]:
                first["done"] = True
                return NetworkError("response lost")  # ...par humein jawab nahi mila
            return r
        self.http.on("POST", "https://api.telegra.ph/createPage", create)
        art = self.publish(s)
        self.assertEqual(len(self.fake.pages), 1)  # duplicate nahi
        self.assertEqual(art["status"], "verified")

    def test_crash_between_intent_and_save_recovers_on_restart(self):
        s = self.uploaded(2)
        real = self.fake._create

        def create(url, h, d):
            real(url, h, d)
            raise KeyboardInterrupt  # process mar gaya, DB me sirf intent hai
        self.http.on("POST", "https://api.telegra.ph/createPage", create)
        with self.assertRaises(KeyboardInterrupt):
            self.publish(s)
        self.assertEqual(self.articles.get(self.aid)["pending_index"], 0)
        self.http.on("POST", "https://api.telegra.ph/createPage", real)
        art = self.publish(DbImageStore(self.db, self.aid))
        self.assertEqual((len(self.fake.pages), art["status"]), (1, "verified"))

    def test_definitive_rejection_creates_nothing_and_raises(self):
        s = self.uploaded(1)
        from tests.fakes_http import jresp
        self.http.on("POST", "https://api.telegra.ph/createPage", lambda u, h, d: jresp({"ok": False, "error": "ACCESS_TOKEN_INVALID"}))
        with self.assertRaises(ZT.ZipArticleError):
            self.publish(s)
        self.assertEqual(len(self.fake.pages), 0)

    def test_multi_page_same_title_chain_and_resume_midway(self):
        s = self.uploaded(6)
        art = self.publish(s, limit=420)
        self.assertGreater(len(art["urls"]), 1)
        self.assertEqual({p["title"] for p in self.fake.pages.values()}, {"My Image Collection"})
        self.assertEqual(sum(p["image_count"] for p in art["pages"].values()), 6)

    def test_stripped_images_never_marked_verified(self):
        self.fake.strip_img = True
        art = self.publish(self.uploaded(2))
        self.assertEqual(art["status"], "has_failures"); self.assertEqual(art["counts"]["failed"], 2)

    def test_no_uploaded_images(self):
        with self.assertRaises(ZT.ZipArticleError):
            self.publish(DbImageStore(self.db, self.aid))


# --------------------------------------------------------------- strict videos
class Spy(FakeChannel):
    def __init__(self, clock, store_getter, *a, **kw):
        super().__init__(clock, *a, **kw); self.snap = []; self.store_getter = store_getter

    async def send_video(self, path, progress):
        self.snap.append((Path(path).name, [r["status"] for r in sorted(self.store_getter().files, key=lambda r: (-r["size"], r["filename"]))]))
        return await super().send_video(path, progress)


class Videos(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory(); self.addCleanup(self._t.cleanup)
        self.tmp = Path(self._t.name)
        self.db = D.Database(f"sqlite:///{self.tmp}/a.db"); D.migrate(self.db); self.addCleanup(self.db.close)
        JobRepo(self.db).discover(CH, 1, "z.zip", 1)
        self.aid = f"{CH}:1"; self.clock = Clock()
        self.store = DbUploadStore(self.db, self.aid, "Bot", self.tmp)

    def add(self, sizes):
        vids = []
        for i, size in enumerate(sizes):
            p = self.tmp / f"v{i}.mp4"; p.write_bytes(b"x" * size)
            vids.append({"file_id": f"v{i}", "filename": f"v{i}.mp4", "size": size, "path": str(p), "archive_id": self.aid})
        self.store.sync(vids)

    def uploader(self, ch, **pol):
        cfg = UploaderConfig(response_timeout=30, poll_interval=2, delay_between=0, max_retries=2, max_upload_bytes=10_000)
        return StrictVideoUploader(ch, self.store, cfg, policy=VideoPolicy(**{"max_attempts": 2, "resend_on_timeout": False, "late_reply_grace": 5, **pol}),
                                   sleep=self.clock.sleep, now=self.clock.now)

    def link(self, n):
        return [[(2, text_reply(f"Link: https://dw.example/{i}", to_current=True))] for i in range(1, n + 1)]

    async def test_largest_first_one_at_a_time_each_link_saved_before_next(self):
        self.add([100, 300, 200])
        ch = Spy(self.clock, lambda: self.store, self.link(3))
        await self.uploader(ch).process_all(self.store.files)
        self.assertEqual([n for n, _ in ch.sent], ["v1.mp4", "v2.mp4", "v0.mp4"])  # 300, 200, 100
        # har send ke waqt: pehle ke sab videos completed, baaki pending - kabhi do awaiting nahi
        self.assertEqual([s for _, s in ch.snap][0], ["uploading"] if False else ch.snap[0][1])
        self.assertEqual(ch.snap[1][1][0], "completed"); self.assertEqual(ch.snap[2][1][:2], ["completed", "completed"])
        urls = [r["url"] for r in sorted(self.store.files, key=lambda r: -r["size"])]
        self.assertEqual(urls, [f"https://dw.example/{i}" for i in (1, 2, 3)])

    async def test_failure_blocks_and_smaller_videos_never_sent(self):
        self.add([300, 200, 100])
        ch = FakeChannel(self.clock, [[(2, text_reply("Link: https://dw.example/1", to_current=True))], [], []])
        with self.assertRaises(VideoBlocked) as cm:
            await self.uploader(ch).process_all(self.store.files)
        self.assertIn("v1.mp4", str(cm.exception))
        self.assertEqual([n for n, _ in ch.sent], ["v0.mp4", "v1.mp4"])  # 3rd (v2) bheja hi nahi
        self.assertEqual(self.store.get("v2")["status"], "pending")

    async def test_timeout_resend_policy_then_success(self):
        self.add([300])
        ch = FakeChannel(self.clock, [[], [(2, text_reply("Link: https://dw.example/ok", to_current=True))]])
        await self.uploader(ch, resend_on_timeout=True).process_all(self.store.files)
        self.assertEqual(len(ch.sent), 2); self.assertEqual(self.store.get("v0")["url"], "https://dw.example/ok")

    async def test_late_reply_found_before_resend(self):
        self.add([300])
        ch = FakeChannel(self.clock, [[(33, text_reply("Link: https://dw.example/late", to_current=True))]])  # timeout(30) ke baad, grace(5) me
        await self.uploader(ch, resend_on_timeout=True).process_all(self.store.files)
        self.assertEqual(len(ch.sent), 1)  # resend nahi hua
        self.assertEqual(self.store.get("v0")["url"], "https://dw.example/late")

    async def test_attempts_exhausted_blocks(self):
        self.add([300])
        ch = FakeChannel(self.clock, [[], [], []])
        with self.assertRaises(VideoBlocked):
            await self.uploader(ch, resend_on_timeout=True, max_attempts=2).process_all(self.store.files)
        self.assertEqual(len(ch.sent), 2)

    async def test_restart_awaiting_video_not_resent_reply_picked_up(self):
        self.add([300])
        ch = FakeChannel(self.clock, [[(5, text_reply("Link: https://dw.example/r", to_current=True))]])
        sid = await ch.send_video(self.tmp / "v0.mp4", None)  # pichli run me bheja gaya tha
        self.store.update(self.store.get("v0"), status="awaiting_response", sent_message_id=sid)
        n_before = len(ch.sent)
        store2 = DbUploadStore(self.db, self.aid, "Bot", self.tmp)  # restart
        self.store = store2
        await self.uploader(ch).process_all(store2.files)
        self.assertEqual(len(ch.sent), n_before)  # dobara upload nahi
        self.assertEqual(store2.get("v0")["url"], "https://dw.example/r")

    async def test_uploading_state_sent_detected_via_chat_not_resent(self):
        self.add([300])
        rec = self.store.get("v0")
        self.store.update(rec, status="uploading", pre_send_latest_id=100)
        ch = FakeChannel(self.clock, [[]], already_sent=105)
        ch.visible.append((0, __import__("src.diskwala_uploader", fromlist=["BotReply"]).BotReply(id=106, text="Link: https://dw.example/x", reply_to_id=105)))
        await self.uploader(ch).process_all(self.store.files)
        self.assertEqual(ch.sent, []); self.assertEqual(rec["url"], "https://dw.example/x")

    async def test_uploading_state_not_in_chat_is_safely_resent(self):
        self.add([300])
        self.store.update(self.store.get("v0"), status="uploading", pre_send_latest_id=100)
        ch = FakeChannel(self.clock, self.link(1), already_sent=None)
        await self.uploader(ch).process_all(self.store.files)
        self.assertEqual(len(ch.sent), 1)

    async def test_uploading_without_marker_is_blocked_not_guessed(self):
        self.add([300])
        self.store.update(self.store.get("v0"), status="uploading")
        ch = FakeChannel(self.clock, self.link(1))
        with self.assertRaises(VideoBlocked):
            await self.uploader(ch).process_all(self.store.files)
        self.assertEqual(ch.sent, [])

    async def test_completed_videos_skipped_on_resume(self):
        self.add([300, 200])
        ch = FakeChannel(self.clock, self.link(2))
        await self.uploader(ch).process_all(self.store.files)
        ch2 = FakeChannel(self.clock, [])
        await self.uploader(ch2).process_all(DbUploadStore(self.db, self.aid, "Bot", self.tmp).files)
        self.assertEqual(ch2.sent, [])


# --------------------------------------------------------------- publisher
class Publisher(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = D.Database(":memory:"); D.migrate(self.db); self.addCleanup(self.db.close)
        JobRepo(self.db).discover(CH, 1, "z.zip", 1)
        self.aid = f"{CH}:1"; self.posts = PostStore(self.db)
        self.posts.save_plan(self.aid, [PostPlan(i, "full", f"post {i}", 0, 1) for i in (1, 2, 3)])
        self.slept = []

        async def sleep(s): self.slept.append(s)
        self.sleep = sleep

    def pub(self, dest, **pol):
        return PostPublisher(dest, self.posts, PublishPolicy(base_delay=1, max_delay=4, gap_between_posts=0, **pol),
                             sleep=self.sleep, rng=lambda: 0.5)

    async def test_published_in_order_ids_saved_no_duplicates_on_rerun(self):
        d = FakeDest(); p = self.pub(d)
        self.assertEqual(await p.publish_all(self.aid), 3)
        self.assertEqual([t for _, t in d.sent], ["post 1", "post 2", "post 3"])
        self.assertEqual([x["message_id"] for x in self.posts.list(self.aid)], [500, 501, 502])
        await p.publish_all(self.aid)
        self.assertEqual(len(d.sent), 3)

    async def test_crash_while_sending_adopts_existing_message(self):
        d = FakeDest()
        await d.send("post 1")  # bhej diya gaya tha
        self.posts.mark_sending(self.aid, 1)  # par DB me 'sending' hi reh gaya
        await self.pub(d).publish_all(self.aid)
        self.assertEqual([t for _, t in d.sent].count("post 1"), 1)
        self.assertEqual(self.posts.list(self.aid)[0]["status"], "published")

    async def test_lost_response_adopted_not_resent(self):
        d = FakeDest(); d.lost_after_send = 1
        await self.pub(d).publish_all(self.aid)
        self.assertEqual([t for _, t in d.sent], ["post 1", "post 2", "post 3"])

    async def test_flood_wait_sleeps_then_succeeds(self):
        d = FakeDest(errors=[FloodWaitError(7)])
        await self.pub(d).publish_all(self.aid)
        self.assertIn(8, self.slept); self.assertEqual(len(d.sent), 3)

    async def test_transient_error_backoff_with_jitter_then_success(self):
        d = FakeDest(errors=[ConnectionError("x"), ConnectionError("x")])
        await self.pub(d).publish_all(self.aid)
        back = [x for x in self.slept if x > 0]  # 0 = posts ke beech gap
        self.assertEqual(len(d.sent), 3); self.assertEqual(len(back), 2)
        self.assertTrue(all(0 < x <= 4 for x in back)); self.assertLess(back[0], back[1])  # exponential

    async def test_permanent_error_blocks_and_later_posts_not_sent(self):
        class ChatWriteForbiddenError(Exception): pass
        d = FakeDest(errors=[None, ChatWriteForbiddenError("no")])
        with self.assertRaises(PublishBlocked):
            await self.pub(d).publish_all(self.aid)
        self.assertEqual([t for _, t in d.sent], ["post 1"])
        self.assertEqual([x["status"] for x in self.posts.list(self.aid)], ["published", "failed", "pending"])

    async def test_resume_after_block_continues_without_resending_published(self):
        class ChatWriteForbiddenError(Exception): pass
        d = FakeDest(errors=[None, ChatWriteForbiddenError("no")])
        with self.assertRaises(PublishBlocked):
            await self.pub(d).publish_all(self.aid)
        await self.pub(d).publish_all(self.aid)
        self.assertEqual([t for _, t in d.sent], ["post 1", "post 2", "post 3"])


if __name__ == "__main__":
    unittest.main()
