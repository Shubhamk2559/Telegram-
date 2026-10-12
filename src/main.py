"""CLI.   python -m src.main <command>

Commands:
  login      QR se Telegram login (session save hota hai)
  list       DRY-RUN: channel ke ZIP archives list karo (kuch download nahi hota)
  process    --all  ya  --ids 12 15   (chuni hui ZIPs download + extract)
  selftest   Offline test (Telegram ki zarurat nahi)

Part 2 (DiskWala uploader):
  upload-videos                  PREVIEW (kuch upload nahi hota)
  upload-videos --run [--limit N] [--retry-failed] [--retry-interrupted] [--yes]
  upload-control pause|resume|stop
  upload-status                  saved links + failed list (offline)

Part 3 (ImgBB + Telegraph):
  imgbb-upload                   PREVIEW (kuch upload nahi hota)
  imgbb-upload --run [--limit N] [--retry-failed] [--yes]
  telegraph-publish              PREVIEW
  telegraph-publish --run [--title T] [--method embed|plain] [--force]
  final-report                   Telegraph URL + DiskWala links + counts (offline)
"""
from __future__ import annotations

import argparse
import asyncio
import sys


def _size(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


async def _login() -> int:
    from .config import load_settings
    from .telegram_auth import telegram_session
    s = load_settings()
    async with telegram_session(s) as client:
        me = await client.get_me()
        print(f"\nLogin OK: {me.first_name or 'account'} (session saved: workspace/sessions/)")
    return 0


async def _list() -> int:
    from .channel_reader import list_zip_messages, resolve_channel
    from .config import load_settings
    from .manifest import Manifest
    from .telegram_auth import telegram_session
    s = load_settings()
    manifest = Manifest(s.manifest_path, s.workspace)
    async with telegram_session(s) as client:
        ch = await resolve_channel(client, s.channel_name, s.channel_id)
        zips = await list_zip_messages(client, ch)
    print(f"\nChannel: {ch.title} (id {ch.id}) - {len(zips)} ZIP mile  [DRY-RUN: kuch download nahi hua]\n")
    print(f"{'msg_id':>8}  {'size':>10}  {'date':<10}  {'status':<10}  filename")
    for z in zips:
        rec = manifest.get(ch.id, z.message_id)
        status = rec["status"] if rec else "new"
        d = z.date.strftime("%Y-%m-%d") if z.date else "-"
        print(f"{z.message_id:>8}  {_size(z.size):>10}  {d:<10}  {status:<10}  {z.filename}")
    print("\nAgla step: python -m src.main process --all   (ya --ids 12 15)")
    return 0


async def _process(args) -> int:
    from .channel_reader import list_zip_messages, resolve_channel
    from .config import load_settings
    from .manifest import Manifest
    from .pipeline import process_archive
    from .telegram_auth import telegram_session
    if not args.all and not args.ids:
        print("Pehle 'list' chalao, phir '--all' ya '--ids 12 15' do.")
        return 2
    s = load_settings()
    manifest = Manifest(s.manifest_path, s.workspace)
    async with telegram_session(s) as client:
        ch = await resolve_channel(client, s.channel_name, s.channel_id)
        manifest.set_channel(ch.id, ch.title)
        zips = await list_zip_messages(client, ch)
        if args.ids:
            wanted = set(args.ids)
            missing = wanted - {z.message_id for z in zips}
            if missing:
                print(f"Ye message IDs ZIP me nahi mile: {sorted(missing)}")
            zips = [z for z in zips if z.message_id in wanted]
        results = {}
        for z in zips:
            results[z.message_id] = await process_archive(client, ch, z, s, manifest, force=args.force)
    print("\nSummary:")
    for mid, st in results.items():
        print(f"  {mid}: {st}")
    print(f"Manifest: {s.manifest_path}")
    return 0 if all(v in ("completed", "skipped") for v in results.values()) else 1


async def _upload_videos(args) -> int:
    from .config import load_settings
    from .diskwala_uploader import (
        ControlFile, DiskWalaUploader, TelethonBotChannel, TerminalUI, UploaderConfig,
        cli_manual_prompt, print_preview, print_summary,
    )
    from .manifest import Manifest
    from .upload_store import UploadStore, collect_videos
    from .zip_processor import MB

    s = load_settings(require_api=args.run)  # preview ke liye API keys nahi chahiye
    manifest = Manifest(s.manifest_path, s.workspace)
    if not manifest.data["archives"]:
        print("Part 1 manifest khaali hai. Pehle: python -m src.main process --all")
        return 2
    store = UploadStore(s.uploads_path, s.diskwala_bot)
    added = store.sync(collect_videos(manifest.data))
    if added:
        print(f"{added} naye video upload list me jude.")
    if store.recovered:
        print("\nDHYAN: pichli baar ye videos beech me ruke the (bot ko mil gaye ho sakte hain):")
        for name in store.recovered:
            print(f"  - {name}")
        print("Telegram me bot ka chat check karo. Dobara bhejna ho to --retry-interrupted lagao.")

    work = store.select_work(args.retry_failed, args.retry_interrupted, args.limit)
    print_preview(work, store, f" (limit {args.limit})" if args.limit else "")
    if not args.run:
        print("\nYe sirf PREVIEW tha. Asli upload: python -m src.main upload-videos --run --limit 1")
        return 0
    if not work:
        print("\nUpload karne ko kuch pending nahi hai.")
        print_summary(store)
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("Interactive terminal nahi hai. Confirm ke liye --yes lagao.")
            return 2
        if input(f"\n{len(work)} video bot ko bhejne hain. Shuru karein? (y/n): ").strip().lower() != "y":
            print("Cancel.")
            return 0

    from .telegram_auth import telegram_session
    async with telegram_session(s) as client:
        me = await client.get_me()
        max_bytes = (s.max_upload_mb or (4000 if getattr(me, "premium", False) else 2000)) * MB
        channel = await TelethonBotChannel.connect(client, s.diskwala_bot, s.upload_as_document)
        name = getattr(channel.entity, "first_name", "") or ""
        print(f"\nBot verified: @{channel.entity.username} ({name}). Upload limit: {max_bytes // MB} MB")
        control = ControlFile(s.control_path)
        control.write("run")
        cfg = UploaderConfig(
            bot_username=s.diskwala_bot, response_timeout=s.upload_timeout, poll_interval=s.upload_poll,
            max_retries=s.upload_retries, max_upload_bytes=max_bytes, link_domains=s.link_domains,
            delay_between=s.upload_delay, max_flood_wait=s.max_flood_wait)
        up = DiskWalaUploader(channel, store, cfg, ui=TerminalUI(), control=control, on_manual=cli_manual_prompt)
        await up.prepare()
        run = await up.run(work)
    print_summary(store, run)
    return 0 if run.failed == 0 and run.interrupted == 0 and not run.stopped_reason else 1


def _upload_control(args) -> int:
    from .config import load_settings
    from .diskwala_uploader import ControlFile
    ControlFile(load_settings(require_api=False).control_path).write(args.action if args.action != "resume" else "run")
    print(f"Control: {args.action}. (Chalu upload agle video se pehle ye padhega.)")
    return 0


def _upload_status() -> int:
    from .config import load_settings
    from .diskwala_uploader import print_summary
    from .upload_store import UploadStore
    s = load_settings(require_api=False)
    store = UploadStore(s.uploads_path, s.diskwala_bot)
    print("Status:", store.counts() or "(koi upload record nahi)")
    print_summary(store)
    return 0


def _part3_stores():
    from .config import load_settings
    from .image_store import ImageStore
    s = load_settings(require_api=False)
    return s, ImageStore(s.images_path)


def _imgbb_upload(args) -> int:
    from .config import ConfigError
    from .http_util import UrllibHttp
    from .image_store import collect_images
    from .imgbb_uploader import ImgBBClient
    from .manifest import Manifest
    from .publish import upload_images
    s, store = _part3_stores()
    manifest = Manifest(s.manifest_path, s.workspace)
    if not manifest.data["archives"]:
        print("Part 1 manifest khaali hai. Pehle: python -m src.main process --all")
        return 2
    added = store.sync(collect_images(manifest.data))
    if added:
        print(f"{added} nayi images image manifest me judin.")
    work = store.select_for_upload(args.retry_failed, args.limit)
    total = sum(r["size"] for r in work)
    print(f"\nImgBB upload ke liye: {len(work)} images ({_size(total)})  |  status: {store.counts() or '-'}")
    for r in work[:10]:
        print(f"  - {r['filename']} ({_size(r['size'])})")
    if len(work) > 10:
        print(f"  ... aur {len(work) - 10}")
    if not args.run:
        print("\nYe sirf PREVIEW tha. Asli upload: python -m src.main imgbb-upload --run --limit 1")
        return 0
    if not work:
        print("\nUpload karne ko kuch pending nahi hai.")
        return 0
    if not s.imgbb_api_key:
        raise ConfigError("IMGBB_API_KEY nahi mili. '.env' me daalo (https://api.imgbb.com/ se milti hai).")
    if not args.yes:
        if not sys.stdin.isatty():
            print("Interactive terminal nahi hai. Confirm ke liye --yes lagao.")
            return 2
        if input(f"\n{len(work)} images ImgBB par bhejni hain. Shuru karein? (y/n): ").strip().lower() != "y":
            print("Cancel.")
            return 0
    client = ImgBBClient(s.imgbb_api_key, UrllibHttp(), max_retries=s.imgbb_retries)
    run = upload_images(store, client, work, s.workspace, delay=s.imgbb_delay)
    print(f"\nUploaded: {run.uploaded}, failed: {run.failed}  |  status: {store.counts()}")
    if run.stopped_reason:
        print(f"Batch ruka: {run.stopped_reason}")
    return 0 if run.failed == 0 and not run.stopped_reason else 1


def _telegraph_publish(args) -> int:
    from .http_util import UrllibHttp
    from .publish import PublishError, ensure_telegraph_token, publish_article
    from .telegraph_publisher import METHOD_EMBED, METHOD_PLAIN, TelegraphClient
    s, store = _part3_stores()
    method = METHOD_PLAIN if args.method == "plain" else METHOD_EMBED
    ready = store.uploaded()
    print(f"\nTelegraph article ke liye ImgBB par uploaded images: {len(ready)} / {len(store.files)}")
    print(f"Method: {method}")
    if not args.run:
        print("\nYe sirf PREVIEW tha. Asli publish: python -m src.main telegraph-publish --run")
        return 0
    if not ready:
        print("Pehle: python -m src.main imgbb-upload --run")
        return 2
    http = UrllibHttp()
    client = TelegraphClient(http)
    ensure_telegraph_token(client, s.telegraph_token, s.telegraph_account_path, s.telegraph_author)
    try:
        info = publish_article(store, client, http, title=args.title or s.telegraph_title,
                               author=s.telegraph_author, method=method, force=args.force)
    except PublishError as e:
        print(f"\nERROR: {e}")
        return 1
    print(f"\nArticle: {info['main_url'] or '(page 1 nahi bana)'}")
    print("Poori report: python -m src.main final-report")
    return 0 if info["main_url"] else 1


def _final_report() -> int:
    from .publish import build_report, format_report
    from .manifest import atomic_write_json
    from .upload_store import UploadStore
    s, store = _part3_stores()
    rep = build_report(store, UploadStore(s.uploads_path, s.diskwala_bot))
    atomic_write_json(s.report_path, rep)
    print(format_report(rep))
    print(f"\n(JSON copy: {s.report_path})")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.main", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("list")
    sub.add_parser("selftest")
    sub.add_parser("upload-status")
    uc = sub.add_parser("upload-control")
    uc.add_argument("action", choices=["pause", "resume", "stop"])
    uv = sub.add_parser("upload-videos")
    uv.add_argument("--run", action="store_true", help="asli upload (bina iske sirf preview)")
    uv.add_argument("--limit", type=int, help="sirf itne videos (chhota test batch)")
    uv.add_argument("--retry-failed", action="store_true")
    uv.add_argument("--retry-interrupted", action="store_true")
    uv.add_argument("--yes", action="store_true", help="confirm prompt skip")
    iu = sub.add_parser("imgbb-upload")
    iu.add_argument("--run", action="store_true", help="asli upload (bina iske sirf preview)")
    iu.add_argument("--limit", type=int, help="sirf itni images (chhota test batch)")
    iu.add_argument("--retry-failed", action="store_true")
    iu.add_argument("--yes", action="store_true", help="confirm prompt skip")
    tp = sub.add_parser("telegraph-publish")
    tp.add_argument("--run", action="store_true", help="asli publish (bina iske sirf preview)")
    tp.add_argument("--title", help="article ka title")
    tp.add_argument("--method", choices=["embed", "plain"], default="embed",
                    help="embed = ImgBB HTML embed ko Nodes me convert (default); plain = sirf <img src>")
    tp.add_argument("--force", action="store_true", help="article pehle se bana ho tab bhi naya banao")
    sub.add_parser("final-report")
    pr = sub.add_parser("process")
    pr.add_argument("--all", action="store_true", help="saari ZIPs (jo complete nahi hain)")
    pr.add_argument("--ids", type=int, nargs="+", help="specific message IDs")
    pr.add_argument("--force", action="store_true", help="complete ho chuki ZIP ko dobara process karo")
    args = p.parse_args(argv)

    try:
        if args.cmd == "selftest":
            from .selftest import run
            return run()
        if args.cmd == "upload-control":
            return _upload_control(args)
        if args.cmd == "upload-status":
            return _upload_status()
        if args.cmd == "upload-videos":
            return asyncio.run(_upload_videos(args))
        if args.cmd == "imgbb-upload":
            return _imgbb_upload(args)
        if args.cmd == "telegraph-publish":
            return _telegraph_publish(args)
        if args.cmd == "final-report":
            return _final_report()
        coro = {"login": _login, "list": _list}.get(args.cmd)
        return asyncio.run(coro() if coro else _process(args))
    except KeyboardInterrupt:
        print("\nRoka gaya (Ctrl+C). Jo video beech me tha wo 'interrupted' save hua hai - dobara chalane se pehle bot chat check karo.")
        return 130
    except Exception as e:  # noqa: BLE001
        name = type(e).__name__
        if name in {"ConfigError", "AuthError", "ChannelError", "ManifestError", "UploaderError", "UploadStoreError",
                    "ImgBBError", "ImgBBFatal", "TelegraphError", "ImageStoreError", "PublishError"}:
            print(f"\nERROR: {e}")
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
