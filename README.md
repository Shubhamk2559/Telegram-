# 📡 Telegram Media Project: Part 1 (QR Login + ZIP Extraction)

This tool logs in to **your own** Telegram account via QR code, finds ZIP files in the "File Manager" channel, downloads and safely extracts them, sorts images/videos into separate folders and writes a JSON manifest.
📤 Part 2 (DiskWala bot uploader) is in section 10 below. 🖼 Part 3 (ImgBB/Telegraph) is in its own section further down.

---

## 1️⃣ Install (one time)

🐧 Linux / Mac / Termux:
```bash
cd telegram_media_project
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```
🪟 Windows (PowerShell):
```powershell
cd telegram_media_project
py -3 -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```
🐍 Python 3.11+ is required (`python3 --version`).

## 2️⃣ 🔐 Secrets live only in `.env` (never in chat)

1. Go to https://my.telegram.org → log in → **API development tools** → create an app → you get `api_id` and `api_hash`.
2. Open the `.env` file and fill in `API_ID=` / `API_HASH=`.
3. `CHANNEL_NAME=File Manager` (exact name). If several channels share the same name, you can also set `CHANNEL_ID`.

`.env` and `workspace/sessions/` never go to Git (they are in `.gitignore`).

## 3️⃣ ⌨️ Commands

| Command | What it does |
|---|---|
| `python -m src.main selftest` | 🧪 Offline test (no Telegram needed) |
| `python -m src.main login` | 🔐 QR login, saves the session |
| `python -m src.main list` | 👀 **Dry-run**: lists ZIPs, downloads nothing |
| `python -m src.main process --ids 12 15` | 🎯 Only the selected ZIPs |
| `python -m src.main process --all` | 📦 All new ZIPs |
| `python -m src.main process --all --force` | ♻️ Re-process even completed ZIPs |

Tests separately: `python -m unittest discover -s tests -t .`

## 4️⃣ 📲 How QR login works

Running `login` shows a QR code in the terminal. On your phone: **Telegram → Settings → Devices → Link Desktop Device** → scan.
The QR expires in ~30 sec; the tool shows a fresh one automatically. If 2-step verification is on, a hidden password prompt appears.
From the next run on, the session is reused (no QR again).

## 5️⃣ 🖥 Expected output

```
$ python -m src.main selftest
== 1) Unit tests ==
................
Ran 16 tests in 0.05s
OK
== 2) End-to-end ZIP extraction demo ==
  image images/album/a.JPG  (73 bytes)
  ...
SELFTEST PASS: 4 media files extracted, manifest: .../manifest.json

$ python -m src.main list
Channel: File Manager (id -100123...) - 2 ZIPs found  [DRY-RUN: nothing downloaded]
  msg_id        size  date        status      filename
      12     85.3 MB  2026-10-01  new         photos_set1.zip
      15    420.0 MB  2026-10-05  new         videos_set2.zip

$ python -m src.main process --ids 12
[12] photos_set1.zip: starting...
  Download: 100%
  Done: 40 images, 0 videos.
```

## 6️⃣ 📁 Folder structure

```
workspace/downloads/   <channel>_<msgid>_<name>.zip   (original ZIP, never deleted)
workspace/extracted/   <channel>_<msgid>_<name>/images/...  and  /videos/...  (ZIP folder structure preserved)
workspace/manifests/manifest.json
workspace/sessions/    Telegram session (chmod 600/700)
```
Only images (jpg, jpeg, png, webp, gif, bmp) and videos (mp4, mkv, mov, webm, avi) are extracted; other entries are skipped (the count + names go into the manifest).
Nothing is ever executed, and nothing is ever auto-deleted.

## 7️⃣ 🧾 Manifest (`workspace/manifests/manifest.json`)

For every archive: `archive_id` (`channel:message_id`), `archive_filename`, `message_id`, `archive_sha256`, `status`
(`completed / rejected / corrupted / failed`), `errors`, `started_at`, `finished_at`, and `files[]`: `file_id` (SHA-256),
`type`, `filename`, `size`, `relative_path`, `path`, `original_path_in_zip`. A `completed` archive is not processed again (unless you use `--force`).

## 8️⃣ 🛡 Safety rules

Path traversal / absolute path / symlink / encrypted entries ⇒ the whole archive is **rejected** (before anything is extracted).
Limits in `.env`: `MAX_ARCHIVE_MB`, `MAX_EXTRACTED_MB`, `MAX_FILES`, `MAX_COMPRESSION_RATIO`, `MIN_FREE_DISK_MB`.
Downloads go into a `.part` file first and only take their final name once the size matches.

## 9️⃣ 🛠 Troubleshooting

- ❌ **`API_ID / API_HASH not found`** → Does `.env` exist? Are the values filled in? The file must be in the project root.
- ❌ **`ModuleNotFoundError: telethon`** → Activate the venv, then `pip install -r requirements.txt`.
- ⏳ **Stuck after scanning the QR** → Check for the 2-step password prompt, or run `login` again.
- 🔎 **`Channel not found`** → The name must be exact (spaces/emoji), and your account must be a member of that channel. Or set `CHANNEL_ID`.
- 🔢 **`Multiple matches`** → Pick a number, or set `CHANNEL_ID` in `.env`.
- ⏱ **FloodWait error** → Telegram asked you to wait a bit; run again after a few minutes.
- 💥 **Broken session** → Manually delete the `.session` file in `workspace/sessions/` and run `login` again.
- 🧾 **Broken manifest** → The tool makes a backup (`.corrupt-<time>`); run again.
- 💾 **Disk full** → Free up space in `workspace/` or check `MIN_FREE_DISK_MB`.

## 🔟 📤 Part 2: Sending videos to the DiskWala bot

Reads the Part 1 manifest and sends every **video** (videos only, no ZIPs/images) to `@DiskWalaFileUploaderBot` one by one,
reads the bot's reply and saves the link in `workspace/manifests/video_uploads.json`. It reuses the Part 1 login/session.

### 🧪 Commands (small test batch)
```bash
python -m src.main upload-videos                      # 1) PREVIEW: how many videos, names, total size (uploads nothing)
python -m src.main upload-videos --run --limit 1      # 2) send just 1 video (do this first!)
python -m src.main upload-status                      # 3) saved links + failed list
python -m src.main upload-videos --run                # 4) all remaining pending videos
```
🎛 Control while running (from another terminal): `python -m src.main upload-control pause` / `resume` / `stop`. Ctrl+C = safe cancel.
On restart, `completed` videos are not sent again.

| 🚩 Flag | 📖 Meaning |
|---|---|
| `--limit N` | only N videos |
| `--retry-failed` | retry failed videos |
| `--retry-interrupted` | the ones that stopped midway (check the bot chat first!) |
| `--yes` | skip the "Start now?" prompt |

### 🛡 How it works (safety)
- ✅ The bot is verified (exact username + a `bot` account). `/start` is sent only when the chat is empty.
- 🔗 A link is attached only from the reply that came **after that video**; replies to any other message are ignored.
  The `match` field tells you how: `reply_to` / `filename` / `sequential` / `user_confirmed`. If the previous video timed out and the
  match is `sequential`, it gets `needs_review: true`: check that link yourself.
- 🤖 If it looks like a CAPTCHA / join channel / verify step, it stops and asks you (complete it yourself in Telegram). There is no bypass.
- 🚫 If a limit/quota message appears, or Telegram returns `PeerFlood`/a block, the whole batch stops.
- ⏳ On FloodWait it waits for the seconds Telegram gives. On a network error it first checks whether the file reached the bot;
  if it did, it does not send again (no duplicates).
- ⚠️ A video that stopped midway (crash / Ctrl+C) becomes `interrupted` and is **not re-sent automatically**.
- 🗂 Original files are never deleted/modified.

### ⚙️ Settings (`.env`)
`UPLOAD_RESPONSE_TIMEOUT` (default 180s), `UPLOAD_RETRIES`, `MAX_UPLOAD_MB` (0 = auto: 2000 MB, Premium 4000 MB), `UPLOAD_AS` (document/video),
`DISKWALA_LINK_DOMAINS` (if the bot also sends promo links, allow only the real domain), `UPLOAD_DELAY_SECONDS`.

### 👀 Check this the first time
I don't know the bot's real reply format, so run `--limit 1` first and look at `url`, `urls` and
`bot_reply_excerpt` in `video_uploads.json`. If the bot gives 2 links (e.g. stream + download), `url` is the first link and all of them are in `urls`.
If a promo/join link comes first, set `DISKWALA_LINK_DOMAINS`.

### 📄 video_uploads.json
```json
{"files": [{"file_id": "<sha256>", "filename": "lecture-01.mp4", "status": "completed",
            "url": "<bot's link>", "urls": ["..."], "error": null, "match": "reply_to", "needs_review": false}]}
```
Status: `pending / uploading / awaiting_response / completed / failed / interrupted`.

### 🛠 Part 2 troubleshooting
- 📭 **`Part 1 manifest is empty`** → first run `python -m src.main process --all`.
- ⏱ **Timeout / "no reply"** → increase `UPLOAD_RESPONSE_TIMEOUT`, check the bot chat in Telegram, then `--retry-failed`.
- 🔗 **"no valid http(s) link found"** → the error contains the bot's last reply; use it to understand the format.
- ⚠️ **`interrupted` shows up** → check in the bot chat whether the file was sent, then `--retry-interrupted`.
- 🚫 **Bot reports a limit/rate-limit** → run again later; the tool never bypasses a limit.

---

## 🖼 Part 3: ImgBB + Telegraph

No new pip package is needed. Fill in `IMGBB_API_KEY` in `.env` (get it from https://api.imgbb.com/).

```bash
python -m unittest discover -s tests -t .
python -m src.main imgbb-upload                    # preview
python -m src.main imgbb-upload --run --limit 1    # 1 image first
python -m src.main imgbb-upload --run              # the rest
python -m src.main telegraph-publish               # preview
python -m src.main telegraph-publish --run
python -m src.main final-report
```

🧩 New modules: `imgbb_uploader.py`, `telegraph_publisher.py`, `image_store.py`, `publish.py`, `http_util.py`.
Part 1/2 modules only got additive changes: `config.py` (Part 3 settings), `main.py` (3 new commands).

### 📚 2 important things learned from the docs

1. **The ImgBB API does not return HTML embed code.** The response only has `url`, `display_url`, `url_viewer`,
   `thumb.url`, `medium.url`, `delete_url`. HTML snippets are generated in the ImgBB website UI.
   So `html_embed.original_from_imgbb` in `image_manifest.json` is always `null`, and
   `html_embed.constructed` is a snippet we build ourselves from the API URLs (`<a href=viewer><img src=url ...></a>`).
2. **Telegraph does not accept arbitrary HTML.** `createPage` needs a Node array (JSON), only a few tags
   (a, figure, img, h3, p, ...) and only `href`/`src` attributes. So the constructed snippet is parsed and converted into Nodes
   (`alt` and `border` are dropped; the list is in `telegraph.dropped_from_embed` in the manifest).
   `iframe` is in Telegraph's tag list, but the docs don't say which src works, so we don't use it.
   `--method plain` only builds `<figure><img src></figure>`.

### 🏷 What the Telegraph statuses mean

- ✅ `embedded_verified`: the image is in the stored content from getPage, the page HTML has `<img src>`, and the image URL returns a real image.
- 🟡 `embedded_unverified`: the image was put into the article, but one check could not be confirmed (reason is in the manifest).
- ❌ `failed` / `skipped`: the image is not in the article (reason is in the manifest).

These are server-side checks. To see the pixels in a browser, open the article link once.

📏 Telegraph content is limited to 64 KB. With many images the article is split into several "Part N/M" pages, each ending with a "Next page" link.
If the article already exists, it is not created again without `--force`.

---

## 🏭 Production (Koyeb): Part 4

📘 Full guide: `docs/DEPLOY_KOYEB.md`. In short:

```bash
python -m src.app doctor             # config + database + disk check
python -m src.app db-migrate         # schema (also runs automatically at worker startup)
python -m src.app db-import-legacy   # old JSON manifests -> database (no overwrite)
python -m src.app worker             # 24/7 worker (Docker CMD)
python -m src.app queue | report [ID] | resume ID | requeue ID | approve ID | scan
```

The old `python -m src.main ...` commands keep working as before.
