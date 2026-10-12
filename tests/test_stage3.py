import asyncio
import contextlib
import io
import os
import re
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dotenv import dotenv_values

from src import app
from src import db as D
from src.app_config import ALIASES, WORKER_DEFAULTS, load_app_config
from src.config import ConfigError
from src.health import HealthServer
from src.jobs import JobRepo
from src.preflight import ERROR, OK, WARN, format_checks, on_koyeb, preflight
from src.worker import Worker  # noqa: F401  (import check)

ROOT = Path(__file__).resolve().parent.parent
MB = 1024 * 1024


def env_names() -> set:
    src = (ROOT / "src/config.py").read_text() + (ROOT / "src/app_config.py").read_text()
    names = set(re.findall(r'(?:getenv|_num|_bool|env\.get)\(\s*(?:env,\s*)?"([A-Z][A-Z0-9_]+)"', src))
    return names | set(ALIASES) | set(ALIASES.values()) | set(WORKER_DEFAULTS)


class DeployFiles(unittest.TestCase):
    def test_env_example_covers_every_variable_the_code_reads(self):
        text = (ROOT / ".env.example").read_text()
        missing = [n for n in sorted(env_names()) if not re.search(rf"^\s*#?\s*{n}=|old: {n}\b", text, re.M)]
        self.assertEqual(missing, [])

    def test_env_example_has_no_real_secrets_and_is_loadable_config(self):
        vals = dotenv_values(ROOT / ".env.example")
        for k in ("TELEGRAM_API_HASH", "TELEGRAM_SESSION_STRING", "IMGBB_API_KEY", "TELEGRAPH_ACCESS_TOKEN", "DATABASE_URL"):
            self.assertIn(vals.get(k) or "", ("", None), k)
        self.assertEqual(vals["FINAL_POST_TEMPLATE"], "Articles Images {telegraph_url}\n\nVideo links {video_links}")  # asli newline
        for k, want in (("MAX_VIDEO_LINKS_PER_POST", "8"), ("IMGBB_UPLOAD_DELAY_SECONDS", "5"), ("IMGBB_MAX_RETRIES", "5"),
                        ("DISKWALA_RESPONSE_TIMEOUT_SECONDS", "1800"), ("DISKWALA_MAX_RETRIES", "3"),
                        ("WORKER_POLL_INTERVAL_SECONDS", "30"), ("MAX_CONCURRENT_ZIPS", "1"), ("LOG_LEVEL", "INFO"),
                        ("TELEGRAPH_ARTICLE_TITLE", "My Image Collection")):
            self.assertEqual(vals[k], want, k)
        active = {k: v for k, v in vals.items() if v not in (None, "")}
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {**active, "WORKSPACE_DIR": t}, clear=True):
            cfg = load_app_config(require_api=False)  # example khud valid config hai
        self.assertEqual(cfg.worker.max_links_per_post, 8)

    def test_dockerfile(self):
        d = (ROOT / "Dockerfile").read_text()
        self.assertRegex(d, r"(?m)^FROM python:3\.1[1-9]")
        self.assertRegex(d, r"(?m)^USER app")                       # non-root
        self.assertRegex(d, r'(?m)^CMD \["python", "-m", "src\.app", "worker"\]')
        self.assertIn("pip install -r requirements.txt", d)
        self.assertNotRegex(d, r"(?m)^(COPY|ADD) .*\.env")
        self.assertNotRegex(d, r"(?im)^ENV .*(API_HASH|SESSION|TOKEN|API_KEY|DATABASE_URL)")  # secrets image me nahi
        self.assertIn("STOPSIGNAL SIGTERM", d)
        self.assertIn("src.health_probe", d)

    def test_dockerignore_keeps_secrets_and_state_out(self):
        lines = {l.strip() for l in (ROOT / ".dockerignore").read_text().splitlines()}
        for must in (".env", "*.session", "workspace/", ".git", "tests/"):
            self.assertIn(must, lines)

    def test_gitignore_keeps_secrets_out(self):
        g = (ROOT / ".gitignore").read_text()
        self.assertIn(".env", g); self.assertIn("sessions", g)

    def test_requirements(self):
        r = (ROOT / "requirements.txt").read_text()
        self.assertIn("telethon", r); self.assertRegex(r, r"psycopg\[binary\]")

    def test_old_cli_commands_still_registered(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            from src import main
            main.main(["--help"])
        for cmd in ("login", "list", "process", "upload-videos", "imgbb-upload", "telegraph-publish", "final-report"):
            self.assertIn(cmd, out.getvalue())


class Preflight(unittest.TestCase):
    def cfg(self, **env):
        base = {"API_ID": "1", "API_HASH": "h", "IMGBB_API_KEY": "k", "FINAL_POST_DESTINATION_ID": "-100", "SESSION_STRING": "s",
                "WORKSPACE_DIR": tempfile.mkdtemp(), "DATABASE_URL": "sqlite:///" + tempfile.mkdtemp() + "/p.db"}
        with mock.patch.dict(os.environ, {**base, **env}, clear=True):
            return load_app_config(require_api=False)

    def levels(self, checks):
        return {c.name: c.level for c in checks}

    def test_all_good(self):
        lv = self.levels(preflight(self.cfg(), env={}, free_bytes=50_000 * MB))
        self.assertEqual({k: v for k, v in lv.items() if v == ERROR}, {})
        self.assertEqual(lv["database_connect"], OK); self.assertEqual(lv["disk"], OK)

    def test_missing_essentials_are_errors(self):
        cfg = self.cfg(IMGBB_API_KEY="", FINAL_POST_DESTINATION_ID="", SESSION_STRING="", API_ID="")
        lv = self.levels(preflight(cfg, env={"KOYEB_APP_NAME": "x"}, free_bytes=50_000 * MB, check_db=False))
        for name in ("telegram_api", "session", "destination", "imgbb"):
            self.assertEqual(lv[name], ERROR, name)

    def test_disk_too_small_for_a_max_zip_is_error_partial_is_warning(self):
        cfg = self.cfg()
        lim = cfg.worker.limits
        small = (lim.max_archive_bytes + lim.min_free_bytes) // 2
        mid = lim.max_archive_bytes + lim.min_free_bytes + MB
        self.assertEqual(self.levels(preflight(cfg, env={}, free_bytes=small, check_db=False))["disk"], ERROR)
        c = [x for x in preflight(cfg, env={}, free_bytes=mid, check_db=False) if x.name == "disk"][0]
        self.assertEqual(c.level, WARN); self.assertIn("MAX_ARCHIVE_MB", c.message)
        two_gb_free = 1900 * MB  # Koyeb free instance jaisa 2 GB disk
        self.assertEqual(self.levels(preflight(cfg, env={}, free_bytes=two_gb_free, check_db=False))["disk"], ERROR)

    def test_sqlite_is_error_on_koyeb_warning_locally(self):
        cfg = self.cfg()
        self.assertEqual(self.levels(preflight(cfg, env={"KOYEB_SERVICE_NAME": "w"}, free_bytes=50_000 * MB, check_db=False))["database"], ERROR)
        self.assertEqual(self.levels(preflight(cfg, env={}, free_bytes=50_000 * MB, check_db=False))["database"], WARN)

    def test_database_failure_reported_without_leaking_password(self):
        cfg = self.cfg()
        with mock.patch("src.preflight.Database", side_effect=RuntimeError("could not connect postgresql://u:HUNTER2@h/db")):
            checks = preflight(cfg, env={}, free_bytes=50_000 * MB)
        msg = [c for c in checks if c.name == "database_connect"][0]
        self.assertEqual(msg.level, ERROR); self.assertNotIn("HUNTER2", format_checks(checks))

    def test_on_koyeb_hint(self):
        self.assertTrue(on_koyeb({"KOYEB_APP_NAME": "a"})); self.assertFalse(on_koyeb({"HOME": "/x"}))


class Startup(unittest.IsolatedAsyncioTestCase):
    def cfg(self, **env):
        base = {"API_ID": "1", "API_HASH": "h", "IMGBB_API_KEY": "k", "FINAL_POST_DESTINATION_ID": "-100", "SESSION_STRING": "s",
                "WORKSPACE_DIR": tempfile.mkdtemp(), "DATABASE_URL": "sqlite:///" + tempfile.mkdtemp() + "/p.db",
                "MAX_ARCHIVE_MB": "1", "MAX_EXTRACTED_MB": "1", "MIN_FREE_DISK_MB": "1"}
        with mock.patch.dict(os.environ, {**base, **env}, clear=True):
            return load_app_config(require_api=False)

    async def test_connect_timeout_gives_clear_error(self):
        class Hang:
            async def connect(self): await asyncio.sleep(10)
        with self.assertRaises(ConfigError) as cm:
            await app.connect_client(self.cfg(), lambda cfg: Hang(), timeout=0.05)
        self.assertIn("connect nahi ho paya", str(cm.exception))

    async def test_unauthorized_session_gives_clear_error_and_disconnects(self):
        class Un:
            disconnected = False
            async def connect(self): pass
            async def is_user_authorized(self): return False
            async def disconnect(self): Un.disconnected = True
        with self.assertRaises(ConfigError) as cm:
            await app.connect_client(self.cfg(), lambda cfg: Un())
        self.assertIn("export-session", str(cm.exception)); self.assertTrue(Un.disconnected)

    async def test_authorized_client_returned(self):
        class Ok:
            async def connect(self): pass
            async def is_user_authorized(self): return True
        c = Ok()
        self.assertIs(await app.connect_client(self.cfg(), lambda cfg: c), c)

    async def test_run_worker_refuses_to_start_on_bad_config_and_starts_nothing(self):
        cfg = self.cfg(IMGBB_API_KEY="")
        with mock.patch("src.health.HealthServer") as hs:
            self.assertEqual(await app.run_worker(cfg), 2)
        hs.assert_not_called()

    async def test_health_starts_before_telegram_connect_and_is_stopped_on_failure(self):
        events = []

        class FakeHealth:
            def __init__(self, *a, **k): events.append("health_init"); self.port = 1
            def start(self): events.append("health_start")
            def stop(self): events.append("health_stop")

        class Boom:
            async def connect(self): events.append("connect"); raise ConnectionError("no network")
        with mock.patch("src.health.HealthServer", FakeHealth), self.assertRaises(ConnectionError):
            await app.run_worker(self.cfg(), client_factory=lambda c: Boom())
        self.assertEqual(events, ["health_init", "health_start", "connect", "health_stop"])

    async def test_sigterm_stops_worker_gracefully(self):
        if sys.platform == "win32":
            self.skipTest("POSIX signals")
        stopped = asyncio.Event()
        w = type("W", (), {"stop": lambda self: stopped.set()})()
        loop = asyncio.get_running_loop()
        done = app.install_signal_handlers(loop, w, (signal.SIGTERM,))
        self.addCleanup(loop.remove_signal_handler, signal.SIGTERM)
        self.assertEqual(done, [signal.SIGTERM])
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stopped.wait(), 2)

    async def test_telegraph_token_env_then_db_then_new_account_saved_once(self):
        db = D.Database(":memory:"); D.migrate(db); repo = JobRepo(db)
        made = []

        class C:
            token = None
            def create_account(self, name, author=""): made.append(1); self.token = "NEWTOKEN"; return {}
        c1 = C(); app.ensure_token_db(c1, "", repo)
        self.assertEqual((c1.token, len(made)), ("NEWTOKEN", 1))
        c2 = C(); app.ensure_token_db(c2, "", repo)  # redeploy: wahi account
        self.assertEqual((c2.token, len(made)), ("NEWTOKEN", 1))
        c3 = C(); app.ensure_token_db(c3, "ENVTOKEN", repo)
        self.assertEqual(c3.token, "ENVTOKEN")


class Doctor(unittest.TestCase):
    def test_doctor_exit_codes(self):
        base = {"WORKSPACE_DIR": tempfile.mkdtemp(), "DATABASE_URL": "sqlite:///" + tempfile.mkdtemp() + "/d.db"}
        with mock.patch.dict(os.environ, base, clear=True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = app.main(["doctor"])
            self.assertEqual(code, 1); self.assertIn("[FAIL] imgbb", out.getvalue()); self.assertIn("problem(s)", out.getvalue())
        full = {**base, "API_ID": "1", "API_HASH": "h", "IMGBB_API_KEY": "k", "FINAL_POST_DESTINATION_ID": "-100", "SESSION_STRING": "s",
                "MAX_ARCHIVE_MB": "1", "MAX_EXTRACTED_MB": "1", "MIN_FREE_DISK_MB": "1"}
        with mock.patch.dict(os.environ, full, clear=True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = app.main(["doctor"])
            self.assertEqual(code, 0, out.getvalue()); self.assertIn("Sab theek hai", out.getvalue())


class HealthProbe(unittest.TestCase):
    def run_probe(self, port):
        return subprocess.run([sys.executable, "-m", "src.health_probe"], cwd=ROOT, env={**os.environ, "PORT": str(port)},
                              capture_output=True, timeout=30).returncode

    def test_probe_exit_codes(self):
        db = D.Database(":memory:"); D.migrate(db)
        h = HealthServer(0, db, host="127.0.0.1"); h.start()
        self.assertEqual(self.run_probe(h.port), 0)
        self.assertEqual(h.snapshot()[1]["worker"], "starting")  # Telegram connect se pehle bhi healthy
        h.stop()
        self.assertEqual(self.run_probe(h.port), 1)


if __name__ == "__main__":
    unittest.main()
