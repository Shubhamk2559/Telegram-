import contextlib
import io
import json
import logging
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from src import app
from src import db as D
from src.app_config import apply_env_aliases, load_app_config
from src.config import ConfigError
from src.health import HealthServer
from src.image_store import _blank as img_blank
from src.jobs import JobRepo
from src.legacy_import import import_legacy
from src.logging_setup import RedactingFilter, redact, secret_values
from src.reports_zip import overall_report, zip_report
from src.upload_store import _blank as vid_blank
from tests.stage2_fakes import CH, Runtime, zip_entries
from tests.test_diskwala_uploader import text_reply


# ------------------------------------------------------------ legacy import
def legacy_dir(tmp: Path) -> Path:
    d = tmp / "manifests"; d.mkdir()
    def arch(mid, status="completed"):
        return {"archive_id": f"{CH}:{mid}", "archive_filename": f"z{mid}.zip", "message_id": mid, "channel_id": CH,
                "archive_size": 100, "status": status, "extraction_dir": f"extracted/z{mid}", "archive_path": f"downloads/z{mid}.zip",
                "counts": {"images": 2, "videos": 1}, "files": []}
    (d / "manifest.json").write_text(json.dumps({"archives": [arch(3), arch(4), arch(5, "failed")]}))

    def image(aid, fid, order, uploaded=True, tg=True):
        r = img_blank({"file_id": fid, "filename": f"{fid}.png", "path": f"extracted/{fid}.png", "size": 5, "archive_id": aid, "order": order})
        if uploaded:
            r["imgbb"].update(status="uploaded", url=f"https://i.ibb.co/{fid}/{fid}.png", delete_url="https://ibb.co/x/secret")
        if tg:
            r["telegraph"].update(status="embedded_verified", page_url="https://telegra.ph/Old-Article-10-01", method="html_embed_converted_to_nodes")
        return r
    a, b = f"{CH}:3", f"{CH}:4"
    (d / "image_manifest.json").write_text(json.dumps({"files": [image(a, "i1", 0), image(a, "i2", 1), image(b, "i3", 0), image("zzz:9", "o1", 0)]}))

    def video(aid, fid, done):
        v = vid_blank({"file_id": fid, "filename": f"{fid}.mp4", "path": f"extracted/{fid}.mp4", "size": 9, "archive_id": aid})
        if done:
            v.update(status="completed", url=f"https://www.diskwala.com/app/{fid}", urls=[f"https://www.diskwala.com/app/{fid}"])
        return v
    (d / "video_uploads.json").write_text(json.dumps({"files": [video(a, "v1", True), video(b, "v2", False)]}))
    return d


class Legacy(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory(); self.addCleanup(self._t.cleanup)
        self.tmp = Path(self._t.name)
        self.db = D.Database(f"sqlite:///{self.tmp}/a.db"); D.migrate(self.db); self.addCleanup(self.db.close)
        self.dir = legacy_dir(self.tmp)

    def test_import_preserves_successful_records_and_is_safe_by_default(self):
        before = {f.name: f.read_bytes() for f in self.dir.iterdir()}
        s = import_legacy(self.db, self.dir)
        self.assertEqual((s["archives"], s["images"], s["videos"], s["articles"]), (2, 3, 2, 2))  # B ke images ka article bhi legacy me ban chuka tha
        self.assertEqual((s["completed"], s["paused"], s["skipped_archives"], s["orphan_images"]), (1, 1, 1, 1))
        repo = JobRepo(self.db)
        self.assertEqual(repo.get(f"{CH}:3")["status"], "COMPLETED")   # poora tha: auto-publish nahi hoga
        self.assertEqual(repo.get(f"{CH}:4")["status"], "PAUSED")      # adhura: queue ruki, insaan decide kare
        self.assertIsNone(repo.get(f"{CH}:5"))
        url = json.loads(self.db.query_one("SELECT data FROM images WHERE file_id='i1'")["data"])["imgbb"]["url"]
        self.assertEqual(url, "https://i.ibb.co/i1/i1.png")
        art = json.loads(self.db.query_one("SELECT data FROM articles WHERE archive_id=?", (f"{CH}:3",))["data"])
        self.assertEqual((art["status"], art["urls"]), ("verified", ["https://telegra.ph/Old-Article-10-01"]))
        self.assertEqual(self.db.query_one("SELECT value FROM kv")["value"], "4")  # scanner purani history dobara queue nahi karega
        self.assertEqual({f.name: f.read_bytes() for f in self.dir.iterdir()}, before)  # JSON files chhui nahi

    def test_repeatable_never_overwrites(self):
        import_legacy(self.db, self.dir)
        self.db.execute("UPDATE images SET data = ? WHERE file_id='i1'", (json.dumps({"changed": True}),))
        s2 = import_legacy(self.db, self.dir)
        self.assertEqual((s2["archives"], s2["images"], s2["videos"], s2["articles"]), (0, 0, 0, 0))
        self.assertEqual(json.loads(self.db.query_one("SELECT data FROM images WHERE file_id='i1'")["data"]), {"changed": True})

    def test_missing_files_ok(self):
        empty = self.tmp / "none"; empty.mkdir()
        self.assertEqual(import_legacy(self.db, empty)["archives"], 0)


# ------------------------------------------------------------ reports
class Reports(unittest.IsolatedAsyncioTestCase):
    async def test_zip_and_overall_report(self):
        rt = Runtime({1: zip_entries(2, (30, 20, 10), "a"), 2: zip_entries(1, (10,), "b")})
        self.addCleanup(rt.close)
        rt.channel.replies_per_send.extend([[(2, text_reply(f"Link: https://dw.example/{i}", to_current=True))] for i in (1, 2, 3)] + [[]])
        for _ in range(4):
            await rt.worker.run_once()
        r = zip_report(rt.db, f"{CH}:1")
        for want in ("===== ZIP PROCESSING REPORT =====", "ZIP: zip1.zip", "Images found: 2", "Images uploaded: 2",
                     "https://telegra.ph/Page-1-10-11", "Videos found: 3", "Videos uploaded: 3", "DiskWala links:\n3",
                     "Final Telegram posts:\n1", "Post 1:\nVideo links 1–3 [published (msg 500)]", "Status:\nCOMPLETED"):
            self.assertIn(want, r)
        self.assertIn("Status:\nPAUSED", zip_report(rt.db, f"{CH}:2")); self.assertIn("Reason:", zip_report(rt.db, f"{CH}:2"))
        o = overall_report(rt.db)
        self.assertIn("Total ZIP files: 2", o); self.assertIn("Completed: 1", o); self.assertIn("Paused: 1", o)
        self.assertIn("Unknown archive", zip_report(rt.db, "nope"))


# ------------------------------------------------------------ config
class Config(unittest.TestCase):
    def cfg(self, **env):
        base = {"API_ID": "1", "API_HASH": "hash", "WORKSPACE_DIR": tempfile.gettempdir() + "/ws_cfg"}
        with mock.patch.dict(os.environ, {**base, **env}, clear=True):
            return load_app_config()

    def test_spec_names_are_aliases_old_names_win(self):
        c = self.cfg(TELEGRAM_API_ID="", FILE_MANAGER_CHANNEL_ID="-100123", DISKWALA_BOT_USERNAME="@MyBot",
                     IMGBB_UPLOAD_DELAY_SECONDS="7", IMGBB_MAX_RETRIES="4", DISKWALA_RESPONSE_TIMEOUT_SECONDS="999",
                     DISKWALA_MAX_RETRIES="6", TELEGRAPH_ARTICLE_TITLE="Spec Title", UPLOAD_RETRIES="2", FINAL_POST_DESTINATION_ID="-100777")
        s = c.settings
        self.assertEqual((s.channel_id, s.diskwala_bot, s.imgbb_delay, s.imgbb_retries, s.upload_timeout, s.telegraph_title),
                         (-100123, "MyBot", 7.0, 4, 999, "Spec Title"))
        self.assertEqual(s.upload_retries, 2)  # purana naam set tha: wahi jeeta
        self.assertEqual(c.dest_id, -100777)

    def test_worker_defaults_and_template_escapes(self):
        c = self.cfg(FINAL_POST_TEMPLATE=r"🎬 {telegraph_url}\n\n{video_links}")
        self.assertEqual((c.worker.telegraph_title, c.worker.imgbb_delay, c.uploader.response_timeout), ("My Image Collection", 5.0, 1800.0))
        self.assertEqual(c.worker.template, "🎬 {telegraph_url}\n\n{video_links}")
        self.assertEqual((c.worker.max_links_per_post, c.worker.cleanup), (8, True))

    def test_invalid_values_fail_fast(self):
        for bad in ({"FINAL_POST_TEMPLATE": "{nope} {video_links}"}, {"FINAL_POST_TEMPLATE": "no vars"},
                    {"MAX_VIDEO_LINKS_PER_POST": "9"}, {"MAX_VIDEO_LINKS_PER_POST": "0"}, {"MAX_CONCURRENT_ZIPS": "2"},
                    {"IMAGE_FAILURE_POLICY": "maybe"}, {"WORKER_POLL_INTERVAL_SECONDS": "x"}):
            with self.assertRaises(ConfigError, msg=bad):
                self.cfg(**bad)

    def test_repr_never_leaks_secrets(self):
        c = self.cfg(API_HASH="SUPERHASH123", SESSION_STRING="SESSIONSTRING999", IMGBB_API_KEY="IMGBBKEY777",
                     TELEGRAPH_ACCESS_TOKEN="TGTOKEN555", DATABASE_URL="postgresql://u:DBPASS888@h/db")
        text = repr(c) + repr(c.settings.imgbb_api_key and "x")
        for secret in ("SUPERHASH123", "SESSIONSTRING999", "IMGBBKEY777", "TGTOKEN555", "DBPASS888"):
            self.assertNotIn(secret, repr(c))

    def test_alias_helper_blank_old_name(self):
        env = {"API_ID": " ", "TELEGRAM_API_ID": "42"}
        apply_env_aliases(env)
        self.assertEqual(env["API_ID"], "42")


# ------------------------------------------------------------ logging
class Logging(unittest.TestCase):
    ENV = {"API_HASH": "SUPERHASH123", "SESSION_STRING": "SESSIONSTRING999", "IMGBB_API_KEY": "IMGBBKEY777",
           "TELEGRAPH_ACCESS_TOKEN": "TGTOKEN555", "DATABASE_URL": "postgresql://user:DBPASS888@host/db"}

    def test_redaction_in_messages_args_urls_and_tracebacks(self):
        stream = io.StringIO(); h = logging.StreamHandler(stream)
        h.addFilter(RedactingFilter(secret_values(self.ENV)))
        lg = logging.getLogger("t_redact"); lg.handlers[:] = [h]; lg.propagate = False; lg.setLevel(logging.INFO)
        lg.info("hash=%s url=https://api.imgbb.com/1/upload?key=IMGBBKEY777&x=1 db=%s", "SUPERHASH123", self.ENV["DATABASE_URL"])
        try:
            raise RuntimeError("failed with TGTOKEN555 and SESSIONSTRING999")
        except RuntimeError:
            lg.exception("boom")
        out = stream.getvalue()
        for s in ("SUPERHASH123", "IMGBBKEY777", "TGTOKEN555", "SESSIONSTRING999", "DBPASS888"):
            self.assertNotIn(s, out)
        self.assertIn("***", out); self.assertIn("RuntimeError", out)

    def test_short_values_ignored_and_access_token_param(self):
        self.assertEqual(secret_values({"IMGBB_API_KEY": "abc"}), [])
        self.assertEqual(redact("x access_token=SECRETVALUE y", []), "x access_token=*** y")


# ------------------------------------------------------------ health + CLI
class Health(unittest.TestCase):
    def test_health_endpoint_ok_degraded_and_404(self):
        db = D.Database(":memory:"); D.migrate(db)
        h = HealthServer(0, db, host="127.0.0.1"); h.start(); self.addCleanup(h.stop)
        with urllib.request.urlopen(f"http://127.0.0.1:{h.port}/health", timeout=5) as r:
            body = json.loads(r.read()); self.assertEqual((r.status, body["status"], body["database"]), (200, "ok", True))
        self.assertNotIn("token", json.dumps(body).lower())
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f"http://127.0.0.1:{h.port}/secret", timeout=5)
        self.assertEqual(cm.exception.code, 404)
        db.close()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f"http://127.0.0.1:{h.port}/health", timeout=5)
        self.assertEqual(cm.exception.code, 503)


class Cli(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory(); self.addCleanup(self._t.cleanup)
        self.tmp = Path(self._t.name)
        env = {"WORKSPACE_DIR": str(self.tmp / "ws"), "DATABASE_URL": f"sqlite:///{self.tmp}/cli.db"}
        p = mock.patch.dict(os.environ, env, clear=True); p.start(); self.addCleanup(p.stop)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = app.main(list(argv))
        return code, out.getvalue()

    def test_commands_end_to_end(self):
        code, out = self.run_cli("db-migrate"); self.assertEqual(code, 0); self.assertIn("schema version 2", out)
        self.assertEqual(self.run_cli("db-migrate")[0], 0)  # repeatable
        db = D.Database(os.environ["DATABASE_URL"]); repo = JobRepo(db)
        repo.discover(CH, 1, "a.zip", 1); repo.queue_discovered(); repo.pause(f"{CH}:1", "needs a look"); db.close()
        code, out = self.run_cli("queue"); self.assertIn("PAUSED", out); self.assertIn("needs a look", out); self.assertIn("Total ZIP files: 1", out)
        code, out = self.run_cli("report", f"{CH}:1"); self.assertIn("ZIP: a.zip", out)
        self.assertIn("OVERALL", self.run_cli("report")[1])
        self.assertEqual(self.run_cli("resume", f"{CH}:1")[0], 0)
        code, out = self.run_cli("resume", f"{CH}:1"); self.assertEqual(code, 2); self.assertIn("PAUSED nahi", out)  # clean error, traceback nahi
        self.assertEqual(self.run_cli("approve", f"{CH}:1")[0], 0)

    def test_legacy_import_command(self):
        legacy = legacy_dir(self.tmp)
        code, out = self.run_cli("db-import-legacy", "--manifests-dir", str(legacy))
        self.assertEqual(code, 0); self.assertIn("'completed': 1", out); self.assertIn("'paused': 1", out)

    def test_worker_requires_telegram_credentials_with_clear_error(self):
        code, out = self.run_cli("worker"); self.assertEqual(code, 2); self.assertIn("API_ID", out)


if __name__ == "__main__":
    unittest.main()
