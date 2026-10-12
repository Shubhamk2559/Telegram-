"""Offline selftest: unit tests + ek end-to-end ZIP extraction demo + manifest. Telegram nahi chahiye."""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run() -> int:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from tests.helpers import make_zip, sample_entries

    from .manifest import Manifest
    from .zip_processor import Limits, extract_archive, sha256_file

    print("== 1) Unit tests ==")
    suite = unittest.defaultTestLoader.discover(str(PROJECT_ROOT / "tests"), top_level_dir=str(PROJECT_ROOT))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        print("\nSELFTEST FAIL: unit tests fail hue.")
        return 1

    print("\n== 2) End-to-end ZIP extraction demo ==")
    ws = PROJECT_ROOT / "workspace" / "selftest" / datetime.now().strftime("%Y%m%d_%H%M%S")
    ws.mkdir(parents=True)
    zip_path = make_zip(ws / "demo.zip", sample_entries())
    dest = ws / "extracted" / "demo"
    res = extract_archive(zip_path, dest, Limits(min_free_bytes=0))
    manifest = Manifest(ws / "manifest.json", ws)
    manifest.upsert({
        "archive_id": "selftest:1", "archive_filename": "demo.zip", "message_id": 1,
        "archive_sha256": sha256_file(zip_path), "status": "completed", "files": res.files,
        "errors": [], "extraction_dir": manifest.rel(dest),
    })
    manifest.save()
    data = json.loads((ws / "manifest.json").read_text())
    n = len(data["archives"][0]["files"])
    for f in res.files:
        print(f"  {f['type']:<5} {f['relative_path']}  ({f['size']} bytes)")
    ok = n == 4 and (ws / "demo.zip").exists()
    print(f"\n{'SELFTEST PASS' if ok else 'SELFTEST FAIL'}: {n} media files extracted, manifest: {ws / 'manifest.json'}")
    print("(workspace/selftest/ ko tum khud delete kar sakte ho; code kuch auto-delete nahi karta.)")
    return 0 if ok else 1
