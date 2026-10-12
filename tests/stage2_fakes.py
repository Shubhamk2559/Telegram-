"""Stage 2 ke tests ke fakes: Telegram client, destination, aur poora mocked runtime. Koi real upload/publish nahi."""
import asyncio
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src import db as D
from src.db_stores import PostStore
from src.http_util import NetworkError
from src.imgbb_uploader import ImgBBClient
from src.jobs import JobRepo
from src.strict_videos import StrictVideoUploader, VideoPolicy
from src.telegraph_publisher import TelegraphClient
from src.tg_publisher import PostPublisher, PublishPolicy
from src.diskwala_uploader import UploaderConfig
from src.worker import Ports, Worker, WorkerConfig
from src.zip_processor import Limits
from tests.fakes_http import FakeHttp, FakeTelegraph, imgbb_ok
from tests.helpers import MP4, PNG, make_zip
from tests.test_diskwala_uploader import Clock, FakeChannel, text_reply

CH = -1004316335869


class Msg:
    def __init__(self, mid, name=None, size=10, mime="application/zip"):
        self.id, self.date = mid, datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.file = type("F", (), {"name": name, "mime_type": mime, "size": size})() if name is not None else None


class FakeTgClient:
    def __init__(self, msgs):
        self.msgs = sorted(msgs, key=lambda m: m.id)

    async def get_messages(self, entity, limit=1, **kw):
        return list(reversed(self.msgs))[:limit]

    async def iter_messages(self, entity, min_id=0, reverse=False, **kw):
        for m in (self.msgs if reverse else reversed(self.msgs)):
            if m.id > min_id:
                yield m


class FakeDest:
    def __init__(self, errors=None):
        self.sent, self.errors, self.adopt = [], list(errors or []), {}
        self.lost_after_send = 0  # itne sends "bheje" par caller ko error dikhega

    async def send(self, text):
        if self.errors:
            e = self.errors.pop(0)
            if e:
                raise e
        mid = 500 + len(self.sent)
        self.sent.append((mid, text))
        if self.lost_after_send:
            self.lost_after_send -= 1
            raise ConnectionError("response lost")
        return mid

    async def find_recent(self, text, since_iso):
        return next((m for m, t in self.sent if t.strip() == text.strip()), None)


def zip_entries(images=2, videos=(30, 10, 20), tag="z"):
    e = {}
    for i in range(images):
        e[f"album/{tag}_{i + 1}.png"] = PNG + f"{tag}{i}".encode()
    for i, size in enumerate(videos):
        e[f"clips/{tag}_v{i}.mp4"] = (MP4 + f"{tag}{i}".encode()).ljust(size + 100, b"\0")
    return e


class Runtime:
    """Poora mocked system: sqlite DB, fake ImgBB/Telegraph HTTP, fake DiskWala bot, fake destination."""

    def __init__(self, zips: dict, *, bot_scripts=None, dest=None, cfg_kw=None, policy=None, tg_kw=None):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.zips = zips  # message_id -> entries dict | bytes (corrupt)
        self.db = D.Database(f"sqlite:///{self.tmp}/app.db")
        D.migrate(self.db)
        self.repo = JobRepo(self.db)
        self.http = FakeHttp()
        self.imgbb_calls = []
        self.http.on("POST", "https://api.imgbb.com/", self._imgbb)
        self.tg = FakeTelegraph(self.http, **(tg_kw or {}))
        self.clock = Clock()
        self.bot_scripts = bot_scripts if bot_scripts is not None else []
        self.channel = FakeChannel(self.clock, self.bot_scripts)
        self.dest = dest or FakeDest()
        self.downloads = []
        self.policy = policy or VideoPolicy(max_attempts=2, resend_on_timeout=False, late_reply_grace=5)
        kw = dict(workspace=self.tmp / "workspace", limits=Limits(min_free_bytes=0), poll_interval=0.01,
                  backoff_base=0.001, backoff_cap=0.01, imgbb_delay=0, stage_max_retries=2,
                  telegraph_title="My Image Collection", lease_ttl=3600)
        kw.update(cfg_kw or {})
        self.cfg = WorkerConfig(**kw)
        self.discover_ids = list(zips)
        self.worker = self.new_worker()

    def _imgbb(self, url, headers, data):
        name = re.search(rb'filename="([^"]+)"', data).group(1).decode()
        self.imgbb_calls.append(name)
        return imgbb_ok(name, len(self.imgbb_calls))

    async def _scan(self):
        new = 0
        for mid in self.discover_ids:
            new += self.repo.discover(CH, mid, f"zip{mid}.zip", 10)
        self.discover_ids = []
        self.repo.queue_discovered()
        return {"new": new}

    async def _download(self, job):
        self.downloads.append(job["message_id"])
        path = self.cfg.workspace / "downloads" / f"{job['message_id']}.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.zips[job["message_id"]]
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            make_zip(path, data)
        return path

    def new_worker(self, channel=None):
        old = getattr(self, "worker", None)
        if old:
            old.lease.release()  # graceful shutdown: run_forever ka finally yahi karta hai
        ch = channel or self.channel
        ports = Ports(
            scan=self._scan, download=self._download,
            imgbb=ImgBBClient("KEY", self.http, max_retries=1, sleep=lambda s: None),
            telegraph=TelegraphClient(self.http, "TOK", sleep=lambda s: None), http=self.http,
            make_uploader=lambda store: StrictVideoUploader(
                ch, store, UploaderConfig(response_timeout=30, poll_interval=2, delay_between=0, max_retries=2,
                                          max_upload_bytes=10_000_000), policy=self.policy,
                sleep=self.clock.sleep, now=self.clock.now),
            publisher=PostPublisher(self.dest, PostStore(self.db), PublishPolicy(base_delay=0.001, max_delay=0.01, gap_between_posts=0),
                                    sleep=lambda s: asyncio.sleep(0)))
        return Worker(self.db, self.repo, self.cfg, ports, sleep=asyncio.sleep)

    def links(self, n, prefix="https://dw.example/v"):
        """n videos ke liye bot scripts: har send ka reply us send ke reply_to ke saath."""
        return [[(2, text_reply(f"Link: {prefix}{i}", to_current=True))] for i in range(1, n + 1)]

    def close(self):
        self.db.close()
        self._tmp.cleanup()
