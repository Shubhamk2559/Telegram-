"""Docker HEALTHCHECK helper: local /health ko ping karo. Exit 0 = healthy, 1 = nahi."""
import os
import sys
import urllib.request

try:
    port = os.environ.get("PORT") or os.environ.get("HEALTH_PORT") or "8000"
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=4) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:  # noqa: BLE001
    sys.exit(1)
