"""Loopback-only operator UI. No publishing, arbitrary file access or external write actions."""

import datetime as dt
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from news_engine import UTC
from radar_store import RadarStore

ASSETS = Path(__file__).resolve().parent / "newsroom"


def make_server(state_dir, engine_config, config, port=8791):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Newsroom/1"

        def log_message(self, *_args):
            pass

        def allowed_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def send(self, status, body, content_type="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed_host():
                self.send(403, {"error": "Local host required."})
                return
            path = urlsplit(self.path).path
            if path in ("/", "/index.html", "/app.js", "/style.css"):
                filename = "index.html" if path == "/" else path[1:]
                content_type = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}[filename]
                self.send(200, (ASSETS / filename).read_bytes(), content_type)
                return
            if path == "/api/state":
                store = RadarStore(state_dir)
                try:
                    state = store.state(config, engine_config, dt.datetime.now(UTC))
                    state["csrf_token"] = token
                    state["stories"] = []
                    if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='stories'").fetchone():
                        state["stories"] = [json.loads(row[0]) for row in store.db.execute("SELECT body FROM stories ORDER BY rowid DESC LIMIT 30")]
                    self.send(200, state)
                finally:
                    store.close()
                return
            self.send(404, {"error": "Not found."})

        def do_POST(self):
            origin = self.headers.get("Origin", "")
            allowed_origins = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if not self.allowed_host() or origin not in allowed_origins or not secrets.compare_digest(self.headers.get("X-Radar-Token", ""), token):
                self.send(403, {"error": "Local same-origin operator request required."})
                return
            if urlsplit(self.path).path != "/api/candidate" or self.headers.get_content_type() != "application/json":
                self.send(404, {"error": "Not found."})
                return
            store = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2048:
                    raise ValueError("Invalid request size.")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or not isinstance(body.get("id"), str) or len(body["id"]) > 80:
                    raise ValueError("Invalid candidate ID.")
                store = RadarStore(state_dir)
                if body.get("action") == "queue":
                    job_id = store.queue_candidate(body["id"], config, dt.datetime.now(UTC))
                    self.send(202, {"job_id": job_id})
                elif body.get("action") == "dismiss":
                    store.dismiss(body["id"])
                    self.send(200, {"status": "dismissed"})
                else:
                    raise ValueError("Unknown action.")
            except (ValueError, TypeError, KeyError):
                self.send(409, {"error": "Action rejected: candidate may be stale, handled, or on cooldown."})
            finally:
                if store:
                    store.close()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server
