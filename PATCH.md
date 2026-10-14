# 🧩 Rename feature: 3 small changes + 1 new file (`src/renamer.py`)

## 1️⃣ `src/worker.py`

Import (at the top):
```python
from .renamer import rename_media
```

Add 2 fields to `WorkerConfig` (below `telegraph_author`):
```python
video_name_base: str = ""   # e.g. "Trader Master"; empty = no rename
image_name_base: str = ""
```

In `_stage_extract`, **after** the `res = await asyncio.to_thread(...)` try/except and **before** `files = [...]`:
```python
rename_media(res.files, dest, self.cfg.video_name_base, self.cfg.image_name_base)
```

## 2️⃣ `src/app_config.py`

Inside the `WorkerConfig(...)` call (next to `cleanup=...`):
```python
video_name_base=(env.get("VIDEO_NAME_BASE") or "").strip(),
image_name_base=(env.get("IMAGE_NAME_BASE") or "").strip(),
```

## 3️⃣ `.env.example` (set the same 2 variables in Koyeb too)

```bash
# Videos: the biggest file becomes "<base> 01.mp4", the next smaller one 02, ... (empty = no rename)
VIDEO_NAME_BASE=Trader Master
# Images: "<base> 01.jpg", "<base> 02.jpg", ... in the ZIP's natural order
IMAGE_NAME_BASE=Trader Master
```
