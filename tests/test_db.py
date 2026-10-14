import tempfile
import unittest
from pathlib import Path

from src import db as D


class Migrations(unittest.TestCase):
    def mem(self):
        d = D.Database(":memory:"); self.addCleanup(d.close); return d

    def test_migrate_creates_schema_and_is_repeatable(self):
        d = self.mem()
        self.assertEqual(D.migrate(d), list(range(1, D.LATEST_VERSION + 1)))
        self.assertEqual(D.migrate(d), [])  # dobara: kuch nahi
        self.assertEqual(D.current_version(d), D.LATEST_VERSION)
        tables = {r["name"] for r in d.query("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"archives", "images", "videos", "articles", "posts", "kv", "worker_lock", "schema_migrations"} <= tables)

    def test_newer_db_than_code_refused(self):
        d = self.mem(); D.migrate(d)
        d.execute("INSERT INTO schema_migrations VALUES (99, 'future', 'x')")
        with self.assertRaises(D.DBError):
            D.migrate(d)

    def test_failed_migration_rolls_back(self):
        d = self.mem()
        bad = [(1, "bad", ["CREATE TABLE t1 (a INTEGER)", "THIS IS NOT SQL"])]
        orig = D.MIGRATIONS
        D.MIGRATIONS = bad
        try:
            with self.assertRaises(Exception):
                D.migrate(d)
        finally:
            D.MIGRATIONS = orig
        self.assertEqual(d.query("SELECT name FROM sqlite_master WHERE name='t1'"), [])  # half-applied nahi
        self.assertEqual(D.current_version(d), 0)

    def test_transaction_rollback_and_nesting(self):
        d = self.mem(); D.migrate(d)
        with self.assertRaises(RuntimeError):
            with d.transaction():
                d.execute("INSERT INTO kv VALUES ('a','1','t')")
                with d.transaction():
                    d.execute("INSERT INTO kv VALUES ('b','2','t')")
                raise RuntimeError("boom")
        self.assertEqual(d.query("SELECT * FROM kv"), [])
        with d.transaction():
            d.execute("INSERT INTO kv VALUES ('a','1','t')")
        self.assertEqual(len(d.query("SELECT * FROM kv")), 1)

    def test_foreign_keys_enforced(self):
        d = self.mem(); D.migrate(d)
        with self.assertRaises(Exception):
            d.execute("INSERT INTO images VALUES ('nope', 'f', 0, '{}', 't')")

    def test_persistence_across_reopen(self):
        with tempfile.TemporaryDirectory() as t:
            url = f"sqlite:///{t}/sub/app.db"
            d = D.Database(url); D.migrate(d); d.execute("INSERT INTO kv VALUES ('k','v','t')"); d.close()
            d2 = D.Database(url)
            self.assertEqual(D.migrate(d2), [])
            self.assertEqual(d2.query_one("SELECT value FROM kv WHERE key='k'")["value"], "v"); d2.close()

    def test_empty_url_rejected(self):
        with self.assertRaises(D.DBError):
            D.Database("")


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.db = D.Database(":memory:"); D.migrate(self.db); self.addCleanup(self.db.close)
        self.t = [1000.0]
        self.clock = lambda: self.t[0]

    def lease(self, owner, ttl=60):
        return D.Lease(self.db, owner=owner, ttl=ttl, clock=self.clock)

    def test_only_one_holder(self):
        a, b = self.lease("A"), self.lease("B")
        self.assertTrue(a.acquire()); self.assertFalse(b.acquire())
        self.assertTrue(a.renew())  # apni renew chalti hai
        self.assertEqual(a.holder(), "A")

    def test_expiry_lets_other_take_over(self):
        a, b = self.lease("A"), self.lease("B")
        a.acquire(); self.t[0] += 61
        self.assertIsNone(a.holder())
        self.assertTrue(b.acquire()); self.assertFalse(a.acquire())  # purana worker ab bahar

    def test_renew_extends(self):
        a, b = self.lease("A"), self.lease("B")
        a.acquire(); self.t[0] += 50; a.renew(); self.t[0] += 50
        self.assertFalse(b.acquire())

    def test_release(self):
        a, b = self.lease("A"), self.lease("B")
        a.acquire(); b.release(); self.assertFalse(b.acquire())  # doosra release nahi kar sakta
        a.release(); self.assertTrue(b.acquire())


if __name__ == "__main__":
    unittest.main()
