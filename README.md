# Telegram Media Project - Part 1 (QR Login + ZIP Extraction)

Ye tool tumhare **apne** Telegram account se QR code se login karta hai, "File Manager" channel ki ZIP files dhundhta hai,
download karke safely extract karta hai, aur images/videos alag folders me rakhkar ek JSON manifest banata hai.
Part 2 (DiskWala bot uploader) neeche section 10 me hai. Part 3 (ImgBB/Telegraph) abhi nahi bana.

## 1. Install (ek baar)

Linux / Mac / Termux:
```bash
cd telegram_media_project
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```
Windows (PowerShell):
```powershell
cd telegram_media_project
py -3 -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```
Python 3.11+ chahiye (`python3 --version`).

## 2. Secrets sirf `.env` me (chat me kabhi nahi)

1. https://my.telegram.org -> login -> **API development tools** -> app banao -> `api_id` aur `api_hash` milenge.
2. `.env` file kholo aur `API_ID=` / `API_HASH=` ke aage apni values likho.
3. `CHANNEL_NAME=File Manager` (exact naam). Same naam ke kai channels hon to `CHANNEL_ID` bhi daal sakte ho.

`.env` aur `workspace/sessions/` Git me jaate hi nahi (`.gitignore` me hain).

## 3. Commands

| Command | Kya karta hai |
|---|---|
| `python -m src.main selftest` | Offline test (Telegram nahi chahiye) |
| `python -m src.main login` | QR login, session save |
| `python -m src.main list` | **Dry-run**: ZIPs ki list, kuch download nahi |
| `python -m src.main process --ids 12 15` | Sirf chuni hui ZIPs |
| `python -m src.main process --all` | Saari nayi ZIPs |
| `python -m src.main process --all --force` | Complete ZIPs ko bhi dobara |

Tests alag se: `python -m unittest discover -s tests -t .`

## 4. QR login kaise hota hai

`login` chalane par terminal me QR aata hai. Phone me: **Telegram -> Settings -> Devices -> Link Desktop Device** -> scan.
QR ~30 sec me expire hota hai, tool apne aap naya QR dikha deta hai. 2-step password lagta ho to hidden prompt aayega.
Agli baar se session reuse hota hai (QR dobara nahi).

## 5. Expected output

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
Channel: File Manager (id -100123...) - 2 ZIP mile  [DRY-RUN: kuch download nahi hua]
  msg_id        size  date        status      filename
      12     85.3 MB  2026-10-01  new         photos_set1.zip
      15    420.0 MB  2026-10-05  new         videos_set2.zip

$ python -m src.main process --ids 12
[12] photos_set1.zip: shuru...
  Download: 100%
  Done: 40 images, 0 videos.
```

## 6. Folder structure

```
workspace/downloads/   <channel>_<msgid>_<name>.zip   (original ZIP, kabhi delete nahi)
workspace/extracted/   <channel>_<msgid>_<name>/images/...  aur  /videos/...  (ZIP ka folder structure preserved)
workspace/manifests/manifest.json
workspace/sessions/    Telegram session (chmod 600/700)
```
Sirf images (jpg, jpeg, png, webp, gif, bmp) aur videos (mp4, mkv, mov, webm, avi) extract hote hain; baaki entries skip hoti hain
(count + naam manifest me). Kuch bhi execute nahi hota, aur kuch bhi auto-delete nahi hota.

## 7. Manifest (`workspace/manifests/manifest.json`)

Har archive ke liye: `archive_id` (`channel:message_id`), `archive_filename`, `message_id`, `archive_sha256`, `status`
(`completed / rejected / corrupted / failed`), `errors`, `started_at`, `finished_at`, aur `files[]`: `file_id` (SHA-256),
`type`, `filename`, `size`, `relative_path`, `path`, `original_path_in_zip`. `completed` archive dobara process nahi hota (`--force` se hota hai).

## 8. Safety rules

Path traversal / absolute path / symlink / encrypted entries => poora archive **rejected** (kuch extract hone se pehle).
Limits `.env` me: `MAX_ARCHIVE_MB`, `MAX_EXTRACTED_MB`, `MAX_FILES`, `MAX_COMPRESSION_RATIO`, `MIN_FREE_DISK_MB`.
Download pehle `.part` file me hota hai aur size match hone par hi final naam leta hai.

## 9. Troubleshooting

- **`API_ID / API_HASH nahi mile`** -> `.env` bana hai? Values bharee? File project root me honi chahiye.
- **`ModuleNotFoundError: telethon`** -> venv activate karo, phir `pip install -r requirements.txt`.
- **QR scan hone ke baad bhi atka** -> 2-step password ka prompt check karo; ya `login` dobara chalao.
- **`Channel nahi mila`** -> naam exact (spaces/emoji) ho, aur tumhara account us channel ka member ho. Ya `CHANNEL_ID` daalo.
- **`Multiple matches`** -> number chuno, ya `.env` me `CHANNEL_ID` set karo.
- **FloodWait error** -> Telegram ne thodi der rukne ko kaha; kuch minute baad dobara chalao.
- **Session kharab** -> `workspace/sessions/` ki `.session` file manually delete karke `login` dobara chalao.
- **Manifest kharab** -> tool backup (`.corrupt-<time>`) bana deta hai; dobara chalao.
- **Disk full** -> `workspace/` me jagah banao ya `MIN_FREE_DISK_MB` check karo.

## 10. Part 2 - DiskWala bot ko videos bhejna

Part 1 ka manifest padhta hai, har **video** (sirf videos - ZIP/images nahi) `@DiskWalaFileUploaderBot` ko ek-ek karke bhejta hai,
bot ka reply padhkar link `workspace/manifests/video_uploads.json` me save karta hai. Part 1 ka login/session hi use hota hai.

### Commands (chhota test batch)
```bash
python -m src.main upload-videos                      # 1) PREVIEW: kitne video, naam, total size (kuch upload nahi)
python -m src.main upload-videos --run --limit 1      # 2) sirf 1 video bhejo (pehle ye karo!)
python -m src.main upload-status                      # 3) saved links + failed list
python -m src.main upload-videos --run                # 4) baaki saare pending
```
Beech me control (dusre terminal se): `python -m src.main upload-control pause` / `resume` / `stop`. Ctrl+C = safe cancel.
Restart karne par `completed` videos dobara nahi jaate.

| Flag | Matlab |
|---|---|
| `--limit N` | sirf N videos |
| `--retry-failed` | failed videos dobara |
| `--retry-interrupted` | jo beech me ruke the (pehle bot chat check karo!) |
| `--yes` | "Shuru karein?" prompt skip |

### Ye kaise kaam karta hai (safety)
- Bot verify hota hai (exact username + `bot` account). Chat khaali ho tabhi `/start` bheja jaata hai.
- Link sirf us reply se jodta hai jo **us video ke baad** aaya ho; kisi aur message ka reply ho to ignore.
  `match` field batata hai: `reply_to` / `filename` / `sequential` / `user_confirmed`. Pichla video timeout hua ho aur
  match `sequential` ho to `needs_review: true` lagta hai - wo link khud check karo.
- CAPTCHA / join channel / verify jaisa dikhe to rukkar tumse poochta hai (Telegram me khud complete karo). Koi bypass nahi.
- Limit/quota ka message aaye ya Telegram `PeerFlood`/block de to poora batch ruk jaata hai.
- FloodWait me bataye gaye second tak ruka jaata hai. Network error par pehle check hota hai ki file bot tak pahunch gayi ya nahi;
  pahunch gayi ho to dobara nahi bhejta (duplicate nahi).
- Jo video beech me ruka (crash / Ctrl+C) wo `interrupted` hota hai aur **apne aap dobara nahi jaata**.
- Original files kabhi delete/modify nahi hoti.

### Settings (`.env`)
`UPLOAD_RESPONSE_TIMEOUT` (default 180s), `UPLOAD_RETRIES`, `MAX_UPLOAD_MB` (0 = auto: 2000 MB, Premium 4000 MB), `UPLOAD_AS` (document/video),
`DISKWALA_LINK_DOMAINS` (agar bot promo links bhi bheje to sirf asli domain allow karo), `UPLOAD_DELAY_SECONDS`.

### Pehli baar zaroor dekho
Bot ka asli reply format mujhe nahi pata, isliye pehle `--limit 1` chalao aur `video_uploads.json` me `url`, `urls` aur
`bot_reply_excerpt` dekho. Agar bot 2 links deta hai (jaise stream + download) to `url` pehla link hai, saare `urls` me hain.
Agar promo/join link pehle aa jaata hai to `DISKWALA_LINK_DOMAINS` set karo.

### video_uploads.json
```json
{"files": [{"file_id": "<sha256>", "filename": "lecture-01.mp4", "status": "completed",
            "url": "<bot ka link>", "urls": ["..."], "error": null, "match": "reply_to", "needs_review": false}]}
```
Status: `pending / uploading / awaiting_response / completed / failed / interrupted`.

### Part 2 troubleshooting
- **`Part 1 manifest khaali hai`** -> pehle `python -m src.main process --all`.
- **Timeout / "koi reply nahi"** -> `UPLOAD_RESPONSE_TIMEOUT` badhao, Telegram me bot chat dekho, phir `--retry-failed`.
- **"valid http(s) link nahi mila"** -> error me bot ka aakhri reply likha hota hai; usse format samjho.
- **`interrupted` dikh raha hai** -> bot chat me check karo file gayi thi ya nahi, phir `--retry-interrupted`.
- **Bot limit/rate-limit bataye** -> baad me dobara chalao; tool koi limit bypass nahi karta.

---

## Part 3: ImgBB + Telegraph

Naya pip package nahi chahiye. `.env` me `IMGBB_API_KEY` bharo (https://api.imgbb.com/ se).

```bash
python -m unittest discover -s tests -t .
python -m src.main imgbb-upload                    # preview
python -m src.main imgbb-upload --run --limit 1    # pehle 1 image
python -m src.main imgbb-upload --run              # baaki
python -m src.main telegraph-publish               # preview
python -m src.main telegraph-publish --run
python -m src.main final-report
```

Naye modules: `imgbb_uploader.py`, `telegraph_publisher.py`, `image_store.py`, `publish.py`, `http_util.py`.
Part 1/2 ke modules me sirf additive badlav: `config.py` (Part 3 settings), `main.py` (3 naye commands).

### Docs se mili 2 zaroori baatein

1. **ImgBB API HTML embed code return nahi karta.** Response me sirf `url`, `display_url`, `url_viewer`,
   `thumb.url`, `medium.url`, `delete_url` hote hain. HTML snippets ImgBB website ke UI me banate hain.
   Isliye `image_manifest.json` me `html_embed.original_from_imgbb` hamesha `null` hai, aur
   `html_embed.constructed` API ke URLs se hamara banaya hua snippet hai (`<a href=viewer><img src=url ...></a>`).
2. **Telegraph arbitrary HTML accept nahi karta.** `createPage` ko Node array (JSON) chahiye, sirf kuch tags
   (a, figure, img, h3, p, ...) aur sirf `href`/`src` attributes. Isliye constructed snippet parse hoke Nodes
   me convert hota hai (`alt`, `border` drop hote hain; list manifest me `telegraph.dropped_from_embed` me).
   `iframe` Telegraph ke tag list me hai, par docs nahi batate kaunsa src chalega, isliye use nahi karte.
   `--method plain` sirf `<figure><img src></figure>` banata hai.

### Telegraph status kya matlab

- `embedded_verified`: getPage ke stored content me image hai, page HTML me `<img src>` hai, aur image URL asli image deta hai.
- `embedded_unverified`: article me daali gayi, par ek check confirm nahi hua (reason manifest me).
- `failed` / `skipped`: image article me nahi hai (reason manifest me).

Ye server-side checks hain. Browser me pixels dekhne ke liye article link ek baar khol lo.

Telegraph content 64 KB tak hota hai. Zyada images hon to article kai "Part N/M" pages me bantta hai, har page ke end me "Next page" link.
Article pehle se bana ho to `--force` ke bina dobara nahi banta.


---

## Production (Koyeb) - Part 4

Poori guide: `docs/DEPLOY_KOYEB.md`. Short me:

```bash
python -m src.app doctor             # config + database + disk check
python -m src.app db-migrate         # schema (worker startup par bhi apne aap)
python -m src.app db-import-legacy   # purane JSON manifests -> database (overwrite nahi)
python -m src.app worker             # 24/7 worker (Docker CMD)
python -m src.app queue | report [ID] | resume ID | requeue ID | approve ID | scan
```

Purane `python -m src.main ...` commands waise hi chalte hain.
