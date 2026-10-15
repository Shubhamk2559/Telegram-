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
  cancel ID            ZIP cancel (chal rahi ho to ~40s me ruk jaati hai; queue aage badhti hai; `requeue` se wapas)
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
from .db import Database, DBError, Lease, migrate
from .db_stores import PostStore
from .jobs import JobError, JobRepo
from .supervisor import acquire_lease  # noqa: F401 - app.acquire_lease naam bhi chalta rahe
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
    from .supervisor import SessionUnusable
    from .worker import SESSION_HELP, is_session_dead
    client = (factory or make_client)(cfg)
    try:
        await asyncio.wait_for(client.connect(), timeout)
        authorized = await client.is_user_authorized()
    except asyncio.TimeoutError as e:
        raise ConfigError(f"Telegram se {timeout:.0f}s me connect nahi ho paya (network/DC?).") from e
    except Exception as e:  # noqa: BLE001
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        if is_session_dead(e):
            raise SessionUnusable(f"{type(e).__name__}: {e}. {SESSION_HELP}") from e
        raise
    if not authorized:
        await client.disconnect()
        raise SessionUnusable("Telegram session authorized nahi hai (logout/revoke ho gayi). Bot me /login karo.")
    return client


class _Tracked:
    """download_zip sirf client.download_media use karta hai: usme live-progress callback jod do (download_zip ko chhue bina)."""

    def __init__(self, client, cb):
        self._c, self._cb = client, cb

    async def download_media(self, message, file=None, progress_callback=None, **kw):
        def both(done, total):
            self._cb(done, total)
            if progress_callback:
                progress_callback(done, total)
        return await self._c.download_media(message, file=file, progress_callback=both, **kw)


async def build_worker(cfg: AppConfig, db: Database, repo: JobRepo, client, lease: Lease | None = None, live=None):
    from .channel_reader import DownloadError, ZipMessage, download_zip, resolve_channel
    from .zip_processor import MB, safe_name
    from .diskwala_uploader import TelethonBotChannel
    from .http_util import UrllibHttp
    from .upload_bots import LazyBotChannel, active_key
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
    # Active bot (DB me /switchbot se badla hua, warna env default) ko pehle hi connect karo: galat username startup par pakda jaye.
    # Dusre bot lazy hain: switch ke baad pehli use par connect hote hain.
    bots = cfg.bots or {}
    channels: dict = {}
    first = active_key(repo, bots, cfg.default_bot) if bots else ""
    for key, prof in bots.items():
        if key == first:
            channels[key] = await TelethonBotChannel.connect(client, prof.username, s.upload_as_document)
        else:
            channels[key] = LazyBotChannel(client, prof.username, s.upload_as_document)
    legacy_bot = None if bots else await TelethonBotChannel.connect(client, s.diskwala_bot, s.upload_as_document)

    def make_uploader(store):
        if not bots:  # purane tests/configs (bots={}) ke liye pehle jaisa
            return StrictVideoUploader(legacy_bot, store, cfg.uploader, policy=cfg.video_policy, ui=live)
        key = active_key(repo, bots, cfg.default_bot)  # har ZIP ke video stage par DB se padhta hai => /switchbot turant lagta hai
        prof = bots[key]
        log.info("Video upload bot: %s (@%s)", prof.label, prof.username)
        return StrictVideoUploader(channels[key], store, prof.uploader, policy=prof.policy, ui=live)
    http = UrllibHttp()
    tg = TelegraphClient(http)
    ensure_token_db(tg, s.telegraph_token, repo, s.telegraph_author)

    async def download(job: dict) -> Path:
        msg = await client.get_messages(source.entity, ids=job["message_id"])
        if not msg or not getattr(msg, "file", None):
            raise ArchiveGone(f"ZIP message {job['message_id']} channel me ab nahi hai.")
        z = ZipMessage(job["message_id"], job["filename"], int(msg.file.size or job["size"] or 0), msg.date, msg)
        return await download_zip(_Tracked(client, live.on_download) if live else client, source, z, s)

    async def download_media(job: dict, dest: Path) -> list[dict]:
        """Batch (seedhe bheje image/video) ke messages dest/images|videos me download. ZIP ki tarah .part -> size check -> rename."""
        import re
        import shutil
        specs = job.get("media") or []
        msgs = await client.get_messages(source.entity, ids=[int(m["id"]) for m in specs])
        got = [(m, sp) for m, sp in zip(msgs, specs) if m and getattr(m, "file", None)]
        if not got:
            raise ArchiveGone(f"Batch {job['archive_id']} ke saare messages channel me ab nahi hain.")
        total = sum(int(m.file.size or sp.get("size") or 0) for m, sp in got)
        dest.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(dest).free
        if free < total + s.limits.min_free_bytes:
            raise DownloadError(f"Disk space kam: {free // MB} MB free, chahiye ~{(total + s.limits.min_free_bytes) // MB} MB")
        tracked = _Tracked(client, live.on_download) if live else client
        out = []
        for m, sp in got:
            p = Path(sp["name"])
            name = f"{m.id}_{safe_name(p.stem, 80)}{re.sub(r'[^A-Za-z0-9.]', '', p.suffix).lower()}"
            folder = dest / ("images" if sp["kind"] == "image" else "videos")
            folder.mkdir(parents=True, exist_ok=True)
            final = folder / name
            part = final.with_name(name + ".part")
            await tracked.download_media(m, file=str(part))
            if not part.exists():
                raise DownloadError(f"Message {m.id} download nahi hua.")
            want = int(m.file.size or 0)
            if want and part.stat().st_size != want:
                raise DownloadError(f"Message {m.id} adhoora download ({part.stat().st_size} / {want} bytes).")
            part.replace(final)
            out.append({"type": sp["kind"], "path": final, "name": name, "msg_id": m.id})
        return out

    async def ensure_connected():
        if not client.is_connected():
            await client.connect()

    ports = Ports(
        scan=lambda: scan_channel(client, source.entity, source.id, repo, backfill_from=cfg.backfill_from,
                                  media_batches=cfg.media_batches, quiet_seconds=cfg.media_quiet),
        download=download, download_media=download_media,
        imgbb=ImgBBClient(s.imgbb_api_key, http, max_retries=s.imgbb_retries),
        telegraph=tg, http=http,
        make_uploader=make_uploader,
        publisher=PostPublisher(TelethonDestination(client, dest.entity, cfg.link_preview), PostStore(db), cfg.publish_policy),
        ensure_connected=ensure_connected)
    return Worker(db, repo, cfg.worker, ports, lease=lease)


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
    from .control_bot import ControlBot, load_bot_settings
    from .health import HealthServer
    from .preflight import ERROR, OK, Check, format_checks, preflight
    from .supervisor import Supervisor
    db, repo = _open(cfg)
    bs = load_bot_settings()
    checks = preflight(cfg, check_db=False)
    if bs.enabled:  # session ab bot ke /login se aa sakti hai: env me hona zaroori nahi
        checks = [c for c in checks if c.name != "session"]
        checks.append(Check(OK, "control_bot", f"BOT_TOKEN set hai, admins: {len(bs.admins)}"
                            + ("" if bs.admins else " (ADMIN_IDS khaali: bot ko /start bhejo, wo tumhari ID batayega)")))
    for line in format_checks(checks).splitlines():
        log.info("preflight %s", line)
    if any(c.level == ERROR for c in checks):
        log.error("Preflight fail: upar [FAIL] lines theek karo. `python -m src.app doctor` se dobara dekho.")
        db.close()
        return 2
    if db.kind == "sqlite":
        log.warning("DATABASE SQLite hai: redeploy par queue/login reset hoga aur do instance ek session ek saath chala sakte hain "
                    "(lease sirf PostgreSQL me instances ke beech kaam karti hai). DATABASE_URL me PostgreSQL do.")
    lease = Lease(db, ttl=cfg.worker.lease_ttl)
    sup = Supervisor(cfg, db, repo, lease, connect=connect_client, build=build_worker, client_factory=client_factory,
                     bot_enabled=bs.enabled)
    health = None
    if cfg.health_enabled:  # PEHLE start: health check Telegram/login se pehle bhi pass hona chahiye
        health = HealthServer(cfg.health_port, db, sup)
        health.start()
        log.info("Health endpoint port %s par.", health.port)
    install_signal_handlers(asyncio.get_running_loop(), sup)
    bot = None
    code = 0
    try:
        if not await acquire_lease(lease, sup.stop_event):  # Telegram/bot se judne SE PEHLE
            return 0
        if bs.enabled:
            bot = ControlBot(cfg, bs, sup)
            await bot.start()
        code = await sup.run()
    finally:
        if bot:
            await bot.stop()
        lease.release()
        if health:
            health.stop()
        db.close()
    return code


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
        info = await scan_channel(client, src.entity, src.id, repo, backfill_from=from_id or cfg.backfill_from,
                                  media_batches=cfg.media_batches, quiet_seconds=cfg.media_quiet)
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
    for name in ("report", "resume", "requeue", "approve", "cancel"):
        sp = sub.add_parser(name)  # ID "-100..." se shuru hota hai: positional ya --archive-id=ID, dono chalte hain
        sp.add_argument("archive_id", nargs="?")
        sp.add_argument("--archive-id", dest="archive_id_opt")
    sc = sub.add_parser("scan")
    sc.add_argument("--from-message-id", type=int)
    argv = [f"--archive-id={t}" if re.fullmatch(r"-\d+:\d+", t) else t for t in (sys.argv[1:] if argv is None else argv)]
    args = ap.parse_args(argv)
    if args.cmd in ("report", "resume", "requeue", "approve", "cancel"):
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
            elif args.cmd == "cancel":
                was = repo.cancel(args.archive_id)
                print(f"{args.archive_id} cancel ho gayi (pehle: {was}). Chal rahi thi to worker ~40s me rok dega.\n"
                      "Dhyan: jo posts publish ho chuke wo delete nahi hote. Wapas lene ke liye: requeue.")
        finally:
            db.close()
        return 0
    except (ConfigError, DBError, JobError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
