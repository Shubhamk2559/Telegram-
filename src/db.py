"""Database layer: PostgreSQL (production) ya SQLite (local/tests). Same SQL dono par chalta hai.

- Placeholders `?` likho; PostgreSQL par apne aap `%s` ban jaate hain.
- Timestamps ISO-8601 UTC TEXT me, JSON TEXT me (portable).
- NOTE: PostgreSQL code path is sandbox me test nahi hua (wahan sirf SQLite hai). Pehli baar real DB par
  `python -m src.main db-migrate` chala ke check karna.
"""
from __future__ import annotations

import os
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Iterator


class DBError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, url: str):
        if not url:
            raise DBError("DATABASE_URL khaali hai.")
        self.url = url
        self.lock = threading.RLock()
        self._depth = 0
        self.kind = "postgres" if url.startswith(("postgres://", "postgresql://")) else "sqlite"
        self._conn = None
        self._connect()

    # ---- connection ---------------------------------------------------------
    def _connect(self) -> None:
        if self.kind == "postgres":
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as e:  # pragma: no cover
                raise DBError("PostgreSQL ke liye 'psycopg[binary]' install karo (requirements.txt).") from e
            self._pg = psycopg
            self._conn = psycopg.connect(self.url, autocommit=True, row_factory=dict_row, connect_timeout=15)
        else:
            path = self.url[len("sqlite:///"):] if self.url.startswith("sqlite:///") else self.url
            if path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
            self._conn.row_factory = sqlite3.Row
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        with self.lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.kind == "postgres" else sql

    def _is_conn_error(self, e: Exception) -> bool:
        return self.kind == "postgres" and isinstance(e, (self._pg.OperationalError, self._pg.InterfaceError))

    def _run(self, fn: Callable):
        """Connection toot jaye (idle timeout/redeploy) to ek baar reconnect - sirf transaction ke bahar."""
        with self.lock:
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                if self._depth == 0 and self._is_conn_error(e):
                    try:
                        self._conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._connect()
                    return fn()
                raise

    # ---- queries ------------------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> int:
        def go():
            cur = self._conn.execute(self._sql(sql), params)
            n = cur.rowcount
            cur.close()
            return n
        return self._run(go)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        def go():
            cur = self._conn.execute(self._sql(sql), params)
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
            return rows
        return self._run(go)

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomic block. Nested call andar wale transaction me hi chalta hai."""
        with self.lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self._depth = 1
            try:
                if self.kind == "postgres":
                    with self._conn.transaction():
                        yield
                else:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        yield
                    except BaseException:
                        self._conn.execute("ROLLBACK")
                        raise
                    else:
                        self._conn.execute("COMMIT")
            finally:
                self._depth = 0


# ------------------------------------------------------------------ migrations
# Har migration = (version, name, [SQL statements]). Purani entries kabhi edit mat karo; nayi append karo.
MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (1, "initial_schema", [
        """CREATE TABLE archives (
            archive_id TEXT PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
            filename TEXT NOT NULL,
            size BIGINT NOT NULL DEFAULT 0,
            msg_date TEXT,
            status TEXT NOT NULL,
            stage TEXT,
            progress TEXT,
            error TEXT,
            paused_reason TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            extraction TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )""",
        "CREATE INDEX archives_queue_idx ON archives (status, message_id)",
        """CREATE TABLE images (
            archive_id TEXT NOT NULL REFERENCES archives (archive_id),
            file_id TEXT NOT NULL,
            ord INTEGER NOT NULL,
            data TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (archive_id, file_id)
        )""",
        """CREATE TABLE videos (
            archive_id TEXT NOT NULL REFERENCES archives (archive_id),
            file_id TEXT NOT NULL,
            ord INTEGER NOT NULL,
            data TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (archive_id, file_id)
        )""",
        """CREATE TABLE articles (
            archive_id TEXT PRIMARY KEY REFERENCES archives (archive_id),
            data TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE posts (
            archive_id TEXT NOT NULL REFERENCES archives (archive_id),
            seq INTEGER NOT NULL,
            content TEXT NOT NULL,
            link_start INTEGER NOT NULL DEFAULT 0,
            link_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            message_id BIGINT,
            error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            published_at TEXT,
            PRIMARY KEY (archive_id, seq)
        )""",
        """CREATE TABLE kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE worker_lock (
            name TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL
        )""",
    ]),
]
LATEST_VERSION = MIGRATIONS[-1][0]


def current_version(db: Database) -> int:
    try:
        row = db.query_one("SELECT MAX(version) AS v FROM schema_migrations")
    except Exception:  # noqa: BLE001 - table abhi hai hi nahi
        return 0
    return int(row["v"] or 0) if row else 0


def migrate(db: Database) -> list[int]:
    """Pending migrations lagao (repeatable: dobara chalane par kuch nahi karta). Applied versions lautata hai."""
    with db.transaction():
        db.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                   "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    have = current_version(db)
    if have > LATEST_VERSION:
        raise DBError(f"Database version {have} is code ({LATEST_VERSION}) se naya hai. Code update karo.")
    applied = []
    for version, name, statements in MIGRATIONS:
        if version <= have:
            continue
        with db.transaction():  # har migration atomic: poori lagti hai ya bilkul nahi
            for stmt in statements:
                db.execute(stmt)
            db.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                       (version, name, now_iso()))
        applied.append(version)
    return applied


# ------------------------------------------------------------------ worker lease
class Lease:
    """DB-based lock with expiry. Ek hi worker active rahega; crash ho to ttl ke baad doosra le leta hai."""

    def __init__(self, db: Database, name: str = "zip-worker", owner: str | None = None, ttl: float = 120,
                 clock: Callable[[], float] = time.time):
        self.db, self.name, self.ttl, self.clock = db, name, ttl, clock
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    def acquire(self) -> bool:
        """Lease lo ya apni renew karo. False = koi aur worker active hai."""
        now = self.clock()
        n = self.db.execute(
            "INSERT INTO worker_lock (name, owner, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET owner = EXCLUDED.owner, expires_at = EXCLUDED.expires_at "
            "WHERE worker_lock.owner = EXCLUDED.owner OR worker_lock.expires_at < ?",
            (self.name, self.owner, now + self.ttl, now))
        return n > 0

    renew = acquire

    def release(self) -> None:
        self.db.execute("DELETE FROM worker_lock WHERE name = ? AND owner = ?", (self.name, self.owner))

    def holder(self) -> str | None:
        row = self.db.query_one("SELECT owner, expires_at FROM worker_lock WHERE name = ?", (self.name,))
        return row["owner"] if row and row["expires_at"] >= self.clock() else None
