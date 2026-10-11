import tempfile
import unittest

from src import db as D
from src import jobs as J

CH = -1004316335869


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.url = f"sqlite:///{self.tmp.name}/q.db"
        self.open()

    def open(self):
        if getattr(self, "db", None):
            self.db.close()
        self.db = D.Database(self.url); D.migrate(self.db); self.repo = J.JobRepo(self.db)
        self.addCleanup(self.db.close)

    def add(self, *mids):
        for m in mids:
            self.repo.discover(CH, m, f"zip{m}.zip", 100 + m)
        self.repo.queue_discovered()


class Discovery(Base):
    def test_archive_id_format_and_dedupe(self):
        self.assertTrue(self.repo.discover(CH, 3, "a.zip", 10))
        self.assertFalse(self.repo.discover(CH, 3, "a-renamed.zip", 99))  # duplicate queue entry nahi
        self.assertEqual(len(self.repo.list()), 1)
        row = self.repo.get(f"{CH}:3")
        self.assertEqual((row["archive_id"], row["filename"], row["status"]), ("-1004316335869:3", "a.zip", "DISCOVERED"))
        self.assertEqual(self.repo.queue_discovered(), 1)
        self.assertEqual(self.repo.get(f"{CH}:3")["status"], "QUEUED")

    def test_last_scanned_checkpoint_never_goes_back(self):
        self.assertEqual(self.repo.last_scanned(CH), 0)
        self.repo.set_last_scanned(CH, 50); self.repo.set_last_scanned(CH, 40)
        self.assertEqual(self.repo.last_scanned(CH), 50)
        self.open()
        self.assertEqual(self.repo.last_scanned(CH), 50)  # restart ke baad bhi


class Ordering(Base):
    def test_oldest_message_first_regardless_of_discovery_order(self):
        self.add(30, 10, 20)
        self.assertEqual(self.repo.next_job().job["message_id"], 10)

    def test_zip2_not_started_until_zip1_completed(self):
        self.add(1, 2, 3)
        order = []
        while (sel := self.repo.next_job()).job:
            order.append(sel.job["message_id"])
            if len(order) > 1:  # job abhi chal rahi ho to wahi wapas milti hai, agli nahi
                pass
            self.repo.transition(sel.job["archive_id"], J.UPLOADING_VIDEOS)
            self.assertEqual(self.repo.next_job().job["message_id"], sel.job["message_id"])
            self.repo.complete(sel.job["archive_id"])
        self.assertEqual(order, [1, 2, 3])

    def test_paused_blocks_queue_never_skipped(self):
        self.add(1, 2)
        self.repo.pause(f"{CH}:1", "DiskWala timeout")
        sel = self.repo.next_job()
        self.assertIsNone(sel.job); self.assertEqual(sel.blocked_by["message_id"], 1)
        self.assertEqual(sel.blocked_by["paused_reason"], "DiskWala timeout")
        self.repo.resume(f"{CH}:1")
        self.assertEqual(self.repo.next_job().job["message_id"], 1)

    def test_failed_does_not_block_but_is_reported_and_requeueable(self):
        self.add(1, 2)
        self.repo.fail(f"{CH}:1", "Corrupt ZIP")
        self.assertEqual(self.repo.next_job().job["message_id"], 2)
        self.assertEqual(self.repo.overall()["failed"], 1)
        self.repo.requeue(f"{CH}:1")
        self.assertEqual((self.repo.next_job().job["message_id"], self.repo.get(f"{CH}:1")["retry_count"]), (1, 1))

    def test_empty_queue(self):
        s = self.repo.next_job(); self.assertEqual((s.job, s.blocked_by), (None, None))


class Recovery(Base):
    def test_crashed_job_resumes_after_restart_completed_ones_skipped(self):
        self.add(1, 2, 3)
        self.repo.complete(f"{CH}:1"); self.repo.complete(f"{CH}:2")
        self.repo.transition(f"{CH}:3", J.UPLOADING_VIDEOS, progress={"video": 4, "of": 9})
        self.open()  # "server restart"
        job = self.repo.next_job().job
        self.assertEqual((job["message_id"], job["status"], job["progress"]), (3, "UPLOADING_VIDEOS", {"video": 4, "of": 9}))

    def test_completed_is_immutable_and_idempotent_guard(self):
        self.add(1); self.repo.complete(f"{CH}:1")
        with self.assertRaises(J.JobError):
            self.repo.transition(f"{CH}:1", J.QUEUED)
        with self.assertRaises(J.JobError):
            self.repo.transition(f"{CH}:99", J.QUEUED)
        with self.assertRaises(J.JobError):
            self.repo.transition(f"{CH}:1", "BOGUS")

    def test_failed_needs_requeue_first(self):
        self.add(1); self.repo.fail(f"{CH}:1", "x")
        with self.assertRaises(J.JobError):
            self.repo.transition(f"{CH}:1", J.EXTRACTING)

    def test_resume_only_from_paused(self):
        self.add(1)
        with self.assertRaises(J.JobError):
            self.repo.resume(f"{CH}:1")

    def test_timestamps_and_stage(self):
        self.add(1)
        self.assertIsNone(self.repo.get(f"{CH}:1")["started_at"])
        self.repo.transition(f"{CH}:1", J.DOWNLOADING)
        r = self.repo.get(f"{CH}:1"); self.assertTrue(r["started_at"]); self.assertEqual(r["stage"], "DOWNLOADING")
        self.repo.complete(f"{CH}:1")
        self.assertTrue(self.repo.get(f"{CH}:1")["finished_at"])

    def test_extraction_json_roundtrip(self):
        self.add(1); self.repo.save_extraction(f"{CH}:1", {"images": 3, "videos": 2})
        self.assertEqual(self.repo.get(f"{CH}:1")["extraction"], {"images": 3, "videos": 2})


class Overall(Base):
    def test_counts(self):
        self.add(1, 2, 3, 4, 5, 6)
        self.repo.complete(f"{CH}:1"); self.repo.complete(f"{CH}:2")
        self.repo.transition(f"{CH}:3", J.UPLOADING_IMAGES)
        self.repo.pause(f"{CH}:4", "x"); self.repo.fail(f"{CH}:5", "y")
        self.assertEqual(self.repo.overall(),
                         {"total": 6, "completed": 2, "processing": 1, "paused": 1, "failed": 1, "pending": 1})


if __name__ == "__main__":
    unittest.main()
