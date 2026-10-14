# Rename feature: 3 chhote badlav + 1 nayi file (src/renamer.py)

## 1) src/worker.py
Import (top par):
    from .renamer import rename_media

WorkerConfig me 2 fields (telegraph_author ke neeche):
    video_name_base: str = ""   # e.g. "Trader Master"; khaali = rename nahi
    image_name_base: str = ""

_stage_extract me, `res = await asyncio.to_thread(...)` try/except ke BAAD, `files = [...]` se PEHLE:
        rename_media(res.files, dest, self.cfg.video_name_base, self.cfg.image_name_base)

## 2) src/app_config.py  (WorkerConfig(...) call me, cleanup=... ke saath)
            video_name_base=(env.get("VIDEO_NAME_BASE") or "").strip(),
            image_name_base=(env.get("IMAGE_NAME_BASE") or "").strip(),

## 3) .env.example (Koyeb me bhi yehi 2 variables)
# Videos: bada size = "<base> 01.mp4", usse chota 02, ... (khaali = rename nahi)
VIDEO_NAME_BASE=Trader Master
# Images: "<base> 01.jpg", "<base> 02.jpg", ... ZIP ke natural order me
IMAGE_NAME_BASE=Trader Master
