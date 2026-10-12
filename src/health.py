"""Halka HTTP health endpoint (alag thread; Telegram worker ko block nahi karta). GET /health -> JSON.
Koyeb health check / uptime monitor isi ko dekhta hai. Koi secret ya media detail expose nahi hoti."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .db import Database, now_iso


class HealthServer:
    def __init__(self, port: int, db: Database, worker=None, host: str = "0.0.0.0"):
        self.db, self.worker = db, worker  # worker baad me set ho sakta hai (health pehle start hota hai)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.split("?")[0] not in ("/", "/health", "/healthz"):
                    self.send_response(404); self.end_headers(); return
                code, body = outer.snapshot()
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):  # access log shor nahi
                pass

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="health", daemon=True)

    def snapshot(self) -> tuple[int, dict]:
        try:
            self.db.query_one("SELECT 1 AS ok")
            db_ok = True
        except Exception:  # noqa: BLE001
            db_ok = False
        w = self.worker
        body = {"status": "ok" if db_ok else "degraded", "database": db_ok, "time": now_iso(),
                "worker": "running" if w else "starting", "current_job": getattr(w, "current", None),
                "stopping": bool(w and w.stop_event.is_set())}
        return (200 if db_ok else 503), body

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
