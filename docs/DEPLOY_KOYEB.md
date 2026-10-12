# Koyeb par deploy (24/7 worker)

Is guide me `[docs]` = Koyeb ke official docs se (aaj search se padhe), `[3rd-party]` = doosri sites ke claims jo maine
Koyeb ke official page se confirm nahi kiye. **Deploy se pehle https://www.koyeb.com/pricing aur
https://www.koyeb.com/docs/reference/instances khud ek baar dekh lo: prices/limits badalte rehte hain.**

## 1. Plan: free tier is kaam ke liye theek nahi

| Baat | Fact | Source |
|---|---|---|
| Free Instance | Sirf **Web Service**, Worker service nahi ban sakti. 512 MB RAM, 0.1 vCPU, **2 GB disk**, org me ek | [docs] |
| Free Instance sleep | 1 ghante bina **incoming traffic** ke scale-to-zero; disable nahi hota | [docs] |
| Paid Eco instances | eco-micro 4 GB disk ~$2.68/mo, eco-small 8 GB ~$5.36/mo, eco-medium 16 GB ~$10.71/mo, eco-large 20 GB ~$21.43/mo | [docs], snapshot kuch mahine purana |
| Free Postgres | 5 compute-hours/mahina, 1 GB, 5 min idle par sleep | [3rd-party] |
| Paid Postgres Small | ~$29.76/mo | [3rd-party] |
| Card | Free tier ke liye card hold (Feb 2026 se) | [3rd-party] |
| Health check | Web Service par TCP/HTTP health check zaroori; Worker service par nahi | [docs] + Koyeb community |
| Stop signal | Koyeb SIGTERM bhejta hai (docs me explicit nahi likha) | [3rd-party] |

**Isliye:**
- Free Instance par ye worker nahi chalega: ek hi ZIP (default limits) ko ~6.5 GB disk chahiye (2048 MB ZIP + 4096 MB extract + 500 MB reserve), aur ek video ka link 30 minute tak aa sakta hai jab koi incoming HTTP traffic nahi hota => instance sleep ho jaayega.
- **Worker service + paid Eco instance, minimum eco-small (8 GB)**; eco-medium (16 GB) aaram se. Ya `MAX_ARCHIVE_MB` / `MAX_EXTRACTED_MB` kam karo. `python -m src.app doctor` disk check karta hai.
- Worker har 30s poll karta hai, to free Postgres ke 5 ghante ~5 din me khatam. **Paid Postgres** lo, ya koi bhi external PostgreSQL (`DATABASE_URL` se). Free external tiers ki apni sleep/limits hoti hain: maine unhe verify nahi kiya. Database connection toote to code ek baar apne aap reconnect karta hai.
- Local disk ephemeral hai: ZIP/extracted files redeploy par ud jaati hain, jaanbujhkar: sab state PostgreSQL me hai; zaroorat par ZIP dobara download hota hai. Persistent volume ki zaroorat nahi.

## 2. Pehli baar setup (local machine par)

1. `python -m src.main login` (QR scan) -> `python -m src.app export-session` -> jo string mile wo **sirf Koyeb Secret** me rakho (poora account access hai).
2. Postgres bana lo, connection string lo (`postgresql://...?sslmode=require`).
3. `.env.example` ko `.env` copy karke bharo, phir local se check:
   ```bash
   python -m src.app doctor
   ```
4. Purana data hai to **pehle** (production DB ke `DATABASE_URL` ke saath, local par):
   ```bash
   python -m src.app db-import-legacy
   python -m src.app queue
   ```
   JSON files sirf padhi jaati hain. Poore ho chuke ZIP `COMPLETED` banenge (koi post auto-publish nahi). Adhure `PAUSED` honge: queue unpe ruki rehti hai jab tak `resume <id>` na karo.

## 3. Koyeb par service

1. Repo GitHub par push karo (`.env`, sessions, `workspace/` kabhi nahi: `.gitignore`/`.dockerignore` me hain).
2. Koyeb -> Create Service -> GitHub -> **Builder: Dockerfile**.
3. **Service type: Worker** (paid instance, eco-small ya bada). Region apni pasand.
4. Environment variables (secrets ko "Secret" type se): `.env.example` ke saare naam. Minimum:
   `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_STRING`, `FILE_MANAGER_CHANNEL_ID`, `FINAL_POST_DESTINATION_ID`,
   `IMGBB_API_KEY`, `DATABASE_URL`, `DISKWALA_BOT_USERNAME`.
   Template Koyeb UI me bina quotes ke paste karo; `\n` likhoge to asli newline banega.
5. **Instances: 1** (scaling/autoscaling band). Doosra instance chale bhi to DB lease use roak deta hai, par fayda kuch nahi.
6. Deploy. Logs me `preflight [ OK ]` lines aur "Worker shuru" dikhna chahiye.

Web Service type use karna ho (sirf agar Worker nahi chalega): port `8000`, health check HTTP path `/health`. Health endpoint Telegram connect hone se pehle hi up ho jaata hai.

**Pehli scan:** checkpoint nahi hota to channel ki purani history queue NAHI hoti (warna sab dobara process/publish ho jaata). Sirf uske baad aane wale ZIPs aate hain. Purane chahiye to `SCAN_BACKFILL_FROM_MESSAGE_ID` ya `python -m src.app scan --from-message-id N`.

## 4. Roz ka kaam

Admin commands local se chalao, `DATABASE_URL` production wala rakh ke (ya `koyeb instances exec <id> -- python -m src.app queue`):

```bash
python -m src.app queue                 # sab ZIPs + overall report
python -m src.app report -1001234:7     # ek ZIP ki detail
python -m src.app resume -1001234:7     # PAUSED ZIP ko aage badhao (done stages skip hote hain)
python -m src.app requeue -1001234:7    # FAILED ZIP dobara
python -m src.app approve -1001234:7    # needs_review video links approve karo, phir resume
```

## 5. Kab kya hota hai

| Situation | Behavior |
|---|---|
| Redeploy / SIGTERM | Worker ruk jaata hai, lease release; naya instance wahin se resume (DiskWala me awaiting video dobara nahi bhejta) |
| Instance crash (kill -9) | Naya worker lease expire (~2 min) hone par chalu |
| DiskWala timeout/fail | Resend policy ke baad ZIP **PAUSED**, chhote videos kabhi skip nahi, queue us ZIP par ruki |
| Corrupt/unsafe ZIP | **FAILED**, queue aage badhti hai |
| ImgBB 429 | Wait + retry (bypass nahi); key galat ho to PAUSED |
| Telegram FloodWait | Utna wait; bahut lamba ho to PAUSED |
| Post send ka result ambiguous | Destination me wahi text dhoond ke adopt, tabhi dobara bhejta hai |

## 6. Jo maine test NAHI kiya

- Docker image build, Koyeb deploy, real PostgreSQL, real Telegram/ImgBB/Telegraph/DiskWala bot: in sab ka koi real test nahi hua (sandbox me inme se kuch nahi tha). Logic mocked tests se verified hai.
- Pehle ek chhote ZIP par poora flow chalao (1 image, 1 video) aur destination ek test channel rakho.
