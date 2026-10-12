"""Production entry point:  python -m src.app <command>

Purane `python -m src.main ...` commands waise hi chalte hain; ye alag entry point hai.
  worker               24/7 worker (Koyeb ka start command)
  db-migrate           schema lagao (repeatable)
  db-import-legacy     purane JSON manifests -> database (sirf padhta hai, overwrite nahi karta)
  queue                saari ZIP jobs ka status
  report [ID]          ek ZIP ki report (ID nahi to overall)
  resume ID            PAUSED ZIP ko dobara queue me
  requeue ID           FAILED ZIP ko dobara queue me
  approve ID           ZIP ke needs_review video links ko approve karo (phir resume)
  scan [--from-message-id N]   channel scan (backfill ke liye)
  export-session       purani file session se TELEGRAM_SESSION_STRING nikalo (Koyeb ke liye)
  doctor               deploy se pehle config / database / disk space check
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import signal
import sys
from pathlib import Path

from .app_config import AppConfig, load_app_config
from .config import ConfigError
from .db import Database, DBError, migrate
from .db_stores import PostStore
from .jobs import JobError, JobRepo
from .legacy_import import import_legacy
from .reports_zip import overall_report, zip_report

log = logging.getLogger("app")


def _open(cfg: AppConfig) -> tuple[Database, JobRepo]:
    db = Database(cfg.database_url)
    migrate(db)  # repeatable; startup par hamesha safe
    return db, JobRepo(db)


# --------------------------------------------------------------------------- runtime wiring (Telethon)
def make_client(cfg: AppConfig):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    s = cfg.settings
    if cfg.session_string:
        session = StringSession(cfg.session_string)
    else:
        session = str(s.session_base)  # local file session (pehle `main login`)
    return TelegramClient(session, s.api_id, s.api_hash, connection_retries=10**6, retry_delay=3, auto_reconnect=True)


async def connect_client(cfg: AppConfig, factory=None, timeout: float = 90.0):
    """Connect + authorize. Network na ho to 90s baad saaf error (warna Telethon ke infinite retry me atka rehta)."""
    client = (factory or make_client)(cfg)
    try:
        await asyncio.wait_for(client.connect(), timeout)
    except asyncio.TimeoutError as e:
        raise ConfigError(f"Telegram se {timeout:.0f}s me connect nahi ho paya (network/DC?).") from e
    if not await client.is_user_authorized():
        await client.disconnect()
        raise ConfigError("Telegram session authorized nahi hai. Local par `python -m src.main login` karke "
                          "`python -m src.app export-session` se TELEGRAM_SESSION_STRING set karo.")
    return client


async def build_worker(cfg: AppConfig, db: Database, repo: JobRepo, client):
    from .channel_reader import ZipMessage, download_zip, resolve_channel
    from .diskwala_uploader import TelethonBotChannel
    from .http_util import UrllibHttp
    from .imgbb_uploader import ImgBBClient
    from .scanner import scan_channel
    from .strict_videos import StrictVideoUploader
    from .telegraph_publisher import TelegraphClient
    from .tg_publisher import PostPublisher, TelethonDestination
    from .worker import ArchiveGone, Ports, Worker

    s = cfg.settings
    if not s.imgbb_api_key:
        raise ConfigError("IMGBB_API_KEY set nahi hai.")
    if cfg.dest_id is None:
        raise ConfigError("FINAL_POST_DESTINATION_ID set nahi hai.")
    source = await resolve_channel(client, s.channel_name, s.channel_id)
    dest = await resolve_channel(client, "", cfg.dest_id)
    bot = await TelethonBotChannel.connect(client, s.diskwala_bot, s.upload_as_document)
    http = UrllibHttp()
    tg = TelegraphClient(http)
    ensure_token_db(tg, s.telegraph_token, repo, s.telegraph_author)

    async def download(job: dict) -> Path:
        msg = await client.get_messages(source.entity, ids=job["message_id"])
        if not msg or not getattr(msg, "file", None):
            raise ArchiveGone(f"ZIP message {job['message_id']} channel me ab nahi hai.")
        z = ZipMessage(job["message_id"], job["filename"], int(msg.file.size or job["size"] or 0), msg.date, msg)
        return await download_zip(client, source, z, s)

    async def ensure_connected():
        if not client.is_connected():
            await client.connect()

    ports = Ports(
        scan=lambda: scan_channel(client, source.entity, source.id, repo, backfill_from=cfg.backfill_from),
        download=download,
        imgbb=ImgBBClient(s.imgbb_api_key, http, max_retries=s.imgbb_retries),
        telegraph=tg, http=http,
        make_uploader=lambda store: StrictVideoUploader(bot, store, cfg.uploader, policy=cfg.video_policy),
        publisher=PostPublisher(TelethonDestination(client, dest.entity, cfg.link_preview), PostStore(db), cfg.publish_policy),
        ensure_connected=ensure_connected)
    return Worker(db, repo, cfg.worker, ports)


def ensure_token_db(client, env_token: str, repo: JobRepo, author: str = "") -> None:
    """Telegraph token: env > database > naya account (database me save). Redeploy par account nahi badalta,
    isliye crash-recovery (getPageList) hamesha usi account ke pages me dhoondhti hai."""
    if env_token:
        client.token = env_token
        return
    saved = repo.get_kv("telegraph_token")
    if saved:
        client.token = saved
        return
    client.create_account("gallery", author)
    repo.set_kv("telegraph_token", client.token)
    log.info("Naya Telegraph account bana; token database me save hua.")


def install_signal_handlers(loop, worker, sigs=(signal.SIGTERM, signal.SIGINT)) -> list:
    """SIGTERM (Koyeb redeploy/stop) par worker graceful ruke: state DB me safe hai, lease release hoti hai."""
    done = []
    for sig in sigs:
        try:
            loop.add_signal_handler(sig, worker.stop)
            done.append(sig)
        except (NotImplementedError, RuntimeError):  # Windows / non-main thread
            pass
    return done


async def run_worker(cfg: AppConfig, *, client_factory=None) -> int:
    from .health import HealthServer
    from .preflight import ERROR, format_checks, preflight
    db, repo = _open(cfg)
    checks = preflight(cfg, check_db=False)
    for line in format_checks(checks).splitlines():
        log.info("preflight %s", line)
    if any(c.level == ERROR for c in checks):
        log.error("Preflight fail: upar [FAIL] lines theek karo. `python -m src.app doctor` se dobara dekho.")
        db.close()
        return 2
    health = None
    if cfg.health_enabled:  # PEHLE start: Koyeb health check Telegram connect hone se pehle bhi pass hona chahiye
        health = HealthServer(cfg.health_port, db)
        health.start()
        log.info("Health endpoint port %s par.", health.port)
    client = None
    try:
        client = await connect_client(cfg, client_factory)
        worker = await build_worker(cfg, db, repo, client)
        if health:
            health.worker = worker
        install_signal_handlers(asyncio.get_running_loop(), worker)
        await worker.run_forever()
    finally:
        if health:
            health.stop()
        if client:
            await client.disconnect()
        db.close()
    return 0


# --------------------------------------------------------------------------- simple commands
def cmd_queue(repo: JobRepo) -> int:
    rows = repo.list()
    if not rows:
        print("Queue khaali hai.")
    for r in rows:
        extra = f" - {r['paused_reason']}" if r["status"] == "PAUSED" else (f" - {r['error']}" if r["status"] == "FAILED" else "")
        print(f"{r['archive_id']:>24}  {r['status']:<20} {r['filename']}{extra}")
    print("\n" + overall_report(repo.db))
    return 0


def cmd_doctor(cfg: AppConfig) -> int:
    from .preflight import ERROR, format_checks, preflight
    checks = preflight(cfg)
    print(format_checks(checks))
    bad = sum(c.level == ERROR for c in checks)
    print("\n" + ("Sab theek hai." if not bad else f"{bad} problem(s) theek karo, phir deploy karo."))
    return 1 if bad else 0


def cmd_approve(db: Database, aid: str) -> int:
    n = 0
    for row in db.query("SELECT file_id, data FROM videos WHERE archive_id = ?", (aid,)):
        rec = json.loads(row["data"])
        if rec.get("needs_review"):
            rec["needs_review"] = False
            db.execute("UPDATE videos SET data = ? WHERE archive_id = ? AND file_id = ?", (json.dumps(rec), aid, row["file_id"]))
            n += 1
    print(f"{n} video link(s) approve kiye. Ab: python -m src.app resume {aid}")
    return 0


async def cmd_scan(cfg: AppConfig, from_id: int | None) -> int:
    from .channel_reader import resolve_channel
    from .scanner import scan_channel
    db, repo = _open(cfg)
    client = await connect_client(cfg)
    try:
        src = await resolve_channel(client, cfg.settings.channel_name, cfg.settings.channel_id)
        info = await scan_channel(client, src.entity, src.id, repo, backfill_from=from_id or cfg.backfill_from)
        print(f"Scan: {info}")
    finally:
        await client.disconnect()
    return 0


async def cmd_export_session(cfg: AppConfig) -> int:
    from telethon.sessions import StringSession
    from .telegram_auth import telegram_session
    async with telegram_session(cfg.settings) as client:
        print("\nYe TELEGRAM_SESSION_STRING hai. Ye tumhare account ka POORA access hai: kisi ko mat bhejo, git me mat dalo,\n"
              "sirf Koyeb ke secret me paste karo.\n")
        print(StringSession.save(client.session))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.app", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("worker", "db-migrate", "queue", "export-session", "doctor"):
        sub.add_parser(name)
    li = sub.add_parser("db-import-legacy")
    li.add_argument("--manifests-dir")
    for name in ("report", "resume", "requeue", "approve"):
        sp = sub.add_parser(name)  # ID "-100..." se shuru hota hai: positional ya --archive-id=ID, dono chalte hain
        sp.add_argument("archive_id", nargs="?")
        sp.add_argument("--archive-id", dest="archive_id_opt")
    sc = sub.add_parser("scan")
    sc.add_argument("--from-message-id", type=int)
    argv = [f"--archive-id={t}" if re.fullmatch(r"-\d+:\d+", t) else t for t in (sys.argv[1:] if argv is None else argv)]
    args = ap.parse_args(argv)
    if args.cmd in ("report", "resume", "requeue", "approve"):
        args.archive_id = args.archive_id_opt or args.archive_id
        if args.cmd != "report" and not args.archive_id:
            ap.error(f"{args.cmd}: archive ID chahiye (jaise -1004316335869:3)")

    try:
        needs_api = args.cmd in ("worker", "scan", "export-session")
        if args.cmd == "doctor":
            return cmd_doctor(load_app_config(require_api=False))
        cfg = load_app_config(require_api=needs_api)
        from .logging_setup import setup_logging
        setup_logging(cfg.log_level)
        if args.cmd == "worker":
            return asyncio.run(run_worker(cfg))
        if args.cmd == "scan":
            return asyncio.run(cmd_scan(cfg, args.from_message_id))
        if args.cmd == "export-session":
            return asyncio.run(cmd_export_session(cfg))
        db, repo = _open(cfg)
        try:
            if args.cmd == "db-migrate":
                from .db import current_version
                print(f"Database ready (schema version {current_version(db)}, {db.kind}).")
            elif args.cmd == "db-import-legacy":
                d = Path(args.manifests_dir) if args.manifests_dir else cfg.settings.manifests_dir
                print("Import summary:", import_legacy(db, d))
            elif args.cmd == "queue":
                return cmd_queue(repo)
            elif args.cmd == "report":
                print(zip_report(db, args.archive_id) if args.archive_id else overall_report(db))
            elif args.cmd == "resume":
                repo.resume(args.archive_id); print("Queue me wapas.")
            elif args.cmd == "requeue":
                repo.requeue(args.archive_id); print("Queue me wapas.")
            elif args.cmd == "approve":
                return cmd_approve(db, args.archive_id)
        finally:
            db.close()
        return 0
    except (ConfigError, DBError, JobError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
