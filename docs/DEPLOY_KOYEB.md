# 🚀 Deploying on Koyeb (24/7 worker)

📌 **Legend used in this guide**
- `[docs]` = read from Koyeb's official docs (checked via search on the day of writing)
- `[3rd-party]` = claims from other sites that were **not** confirmed on Koyeb's official pages

⚠️ **Before you deploy**, check https://www.koyeb.com/pricing and https://www.koyeb.com/docs/reference/instances yourself. Prices and limits keep changing.

---

## 1️⃣ Plan: the free tier is not suitable for this job

| 📝 Topic | 📋 Fact | 🔗 Source |
|---|---|---|
| Free Instance | **Web Service only**, no Worker service. 512 MB RAM, 0.1 vCPU, **2 GB disk**, one per org | [docs] |
| Free Instance sleep | Scales to zero after 1 hour without **incoming traffic**; cannot be disabled | [docs] |
| Paid Eco instances | eco-micro 4 GB disk ~$2.68/mo, eco-small 8 GB ~$5.36/mo, eco-medium 16 GB ~$10.71/mo, eco-large 20 GB ~$21.43/mo | [docs], snapshot a few months old |
| Free Postgres | 5 compute-hours/month, 1 GB, sleeps after 5 min idle | [3rd-party] |
| Paid Postgres Small | ~$29.76/mo | [3rd-party] |
| Card | Card hold for the free tier (since Feb 2026) | [3rd-party] |
| Health check | TCP/HTTP health check is required on a Web Service; not on a Worker service | [docs] + Koyeb community |
| Stop signal | Koyeb sends SIGTERM (not explicitly stated in the docs) | [3rd-party] |

### ✅ So, what to do
- ❌ **The free Instance can't run this worker.** A single ZIP (default limits) needs ~6.5 GB of disk (2048 MB ZIP + 4096 MB extracted + 500 MB reserve). Also, a video link can take up to 30 minutes to arrive while there is no incoming HTTP traffic ⇒ the instance would go to sleep.
- ✅ Use a **Worker service + paid Eco instance, at least eco-small (8 GB)**; eco-medium (16 GB) is comfortable. Or lower `MAX_ARCHIVE_MB` / `MAX_EXTRACTED_MB`. `python -m src.app doctor` checks the disk.
- 🗄 The worker polls every 30s, so the 5 hours of free Postgres run out in ~5 days. Take **paid Postgres**, or any external PostgreSQL (via `DATABASE_URL`). Free external tiers have their own sleep/limits: I have not verified them. If the database connection drops, the code reconnects once by itself.
- 💾 The local disk is ephemeral: ZIP/extracted files vanish on redeploy, **on purpose**. All state lives in PostgreSQL; the ZIP is simply downloaded again when needed. No persistent volume is required.

---

## 2️⃣ First-time setup (on your local machine)

1. 🔑 `python -m src.main login` (scan the QR) → `python -m src.app export-session` → keep the string it prints **only as a Koyeb Secret** (it gives full account access).
2. 🗄 Create a Postgres database and copy its connection string (`postgresql://...?sslmode=require`).
3. 📄 Copy `.env.example` to `.env`, fill it in, then check locally:
   ```bash
   python -m src.app doctor
   ```
4. 📦 If you have old data, do this **first** (locally, with the production DB's `DATABASE_URL`):
   ```bash
   python -m src.app db-import-legacy
   python -m src.app queue
   ```
   - JSON files are only read, never modified.
   - Fully finished ZIPs become `COMPLETED` (no post is auto-published).
   - Unfinished ones become `PAUSED`: the queue waits on them until you run `resume <id>`.

---

## 3️⃣ The service on Koyeb

1. 📤 Push the repo to GitHub (never `.env`, sessions or `workspace/`: they are in `.gitignore` / `.dockerignore`).
2. ➕ Koyeb → Create Service → GitHub → **Builder: Dockerfile**.
3. ⚙️ **Service type: Worker** (paid instance, eco-small or bigger). Pick any region you like.
4. 🔐 Environment variables (use the "Secret" type for secrets): all names from `.env.example`. Minimum:
   `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_STRING`, `FILE_MANAGER_CHANNEL_ID`, `FINAL_POST_DESTINATION_ID`,
   `IMGBB_API_KEY`, `DATABASE_URL`, `DISKWALA_BOT_USERNAME`.
   Paste templates into the Koyeb UI **without quotes**; if you write `\n` it becomes a real newline.
5. 1️⃣ **Instances: 1** (scaling/autoscaling off). If a second instance does start, the DB lease stops it from working, but it gives no benefit.
6. ▶️ Deploy. In the logs you should see the `preflight [ OK ]` lines and "Worker started".

🌐 If you must use the Web Service type (only if Worker doesn't work for you): port `8000`, health check HTTP path `/health`. The health endpoint is up even before Telegram connects.

🔍 **First scan:** if there is no checkpoint, the channel's old history is **NOT** queued (otherwise everything would be processed/published again). Only ZIPs that arrive afterwards are picked up. If you want the old ones, use `SCAN_BACKFILL_FROM_MESSAGE_ID` or `python -m src.app scan --from-message-id N`.

---

## 4️⃣ Daily operations

Run admin commands locally with the **production** `DATABASE_URL` (or `koyeb instances exec <id> -- python -m src.app queue`):

```bash
python -m src.app queue                 # all ZIPs + overall report
python -m src.app report -1001234:7     # details of one ZIP
python -m src.app resume -1001234:7     # push a PAUSED ZIP forward (finished stages are skipped)
python -m src.app requeue -1001234:7    # retry a FAILED ZIP
python -m src.app approve -1001234:7    # approve needs_review video links, then resume
python -m src.app cancel -1001234:7     # cancel a ZIP (queue moves on); bring it back with requeue
```

### 🛑 How cancel works
- ✅ The ZIP becomes `FAILED ("Cancelled by user")`, so the queue does not wait on it. You can also cancel a PAUSED ZIP that is blocking the queue to unblock it.
- ⏱ If the ZIP is running, the worker stops it within ~40 seconds (lease_ttl/3). Work already done (images, article, complete videos) stays safe in the DB.
- ❌ **Not undone:** posts already published on Telegram are not deleted, ImgBB images / Telegraph pages remain, and the link of a video already sent to the bot will still arrive in the bot's chat.
- ♻️ You can bring it back with `requeue <id>`; finished stages are skipped.
- ⏸ **To stop everything:** Pause the service in Koyeb (SIGTERM → the worker stops gracefully, state stays safe). When you Resume, it continues from where it stopped.

---

## 5️⃣ What happens when

| ⚡ Situation | 🤖 Behavior |
|---|---|
| Redeploy / SIGTERM | Worker stops, releases the lease; the new instance resumes from the same point (it does not re-send a video that is awaiting a DiskWala reply) |
| Instance crash (kill -9) | A new worker starts once the lease expires (~2 min) |
| DiskWala timeout/failure | After the resend policy the ZIP becomes **PAUSED**, small videos are never skipped, the queue waits on that ZIP |
| Corrupt/unsafe ZIP | **FAILED**, the queue moves on |
| ImgBB 429 | Wait + retry (no bypass); if the key is wrong → PAUSED |
| Telegram FloodWait | Waits that long; if it is very long → PAUSED |
| Ambiguous post-send result | Looks for the same text in the destination and adopts it; only then does it send again |

---

## 6️⃣ Troubleshooting (from real runs)

| 🪵 Log / reason | 💡 Meaning | 🛠 What to do |
|---|---|---|
| `YouBlockedUserError: You blocked this user` | **The Telegram account whose session the worker uses (`TELEGRAM_SESSION_STRING`) has blocked the DiskWala bot.** The ZIP goes PAUSED. | Open the Telegram app with that same account → DiskWala bot chat → Unblock / Restart. Then run `python -m src.app resume <archive_id>`. If you never blocked the bot, the session string probably belongs to a different account: run `export-session` from the account that did the uploads locally. |
| `PAUSED ... AuthKeyUnregisteredError` / `SessionRevoked` | Invalid session | New `login` + `export-session`, update the Koyeb secret, redeploy, `resume` |
| `PAUSED ... ChatWriteForbiddenError` | No permission to post in the destination | Make the account an admin (with post rights) in the destination channel, then `resume` |
| `PAUSED ... PeerFloodError` | Telegram applied a spam limit | Wait a few hours, check @SpamBot, then `resume` |
| `Unexpected error (n/5)` | A code bug or a new error | Send the full traceback; after 5 tries the ZIP goes PAUSED |

ℹ️ After `resume`, finished work (downloaded images, Telegraph article, complete videos) is not redone. If a redeploy wiped the local disk, only the ZIP is downloaded again.

---

## 7️⃣ ⚠️ What I did NOT test

- 🐳 Docker image build, Koyeb deploy, real PostgreSQL, real Telegram / ImgBB / Telegraph / DiskWala bot: none of these were tested for real (none were available in the sandbox). The logic is verified with mocked tests.
- 🧪 First run the whole flow on a small ZIP (1 image, 1 video), and use a test channel as the destination.
