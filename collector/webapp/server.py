#!/usr/bin/env python3
"""MeshCore Collector — cracker web app HTTP server.

Serves a static single-page UI (a handful of static files, no build step) plus
a small JSON API over the stdlib http.server (matching collector.api's style —
no extra web framework). Works in two modes:

  Offline (no hardware): just a SQLite DB.
      python -m collector.webapp --db collector.db --http-port 8090

  Live (attached to a device): a serial or socket:// port, like collector.api.
      python -m collector.webapp --port COM5
      python -m collector.webapp --port socket://host.local:5005 --password ...

Endpoints:
    GET  /                       — the cracker UI (static index.html)
    GET  /app.js, /styles.css    — static assets
    GET  /api/gpu                — GPU availability + engine
    GET  /api/config             — settings + engine + wordlist + db path
    GET  /api/pending            — pending unknown channel hashes + packet counts
    GET  /api/channels           — cracked channels (counts only, scalable)
    GET  /api/channels/messages  — paginated/searchable messages for a channel
    GET  /api/results            — cracked channels + messages (legacy, capped)
    GET  /api/crack/status       — live crack progress + last result
    POST /api/config             — update cracker settings
    POST /api/wordlist           — add a custom wordlist (text or path)
    POST /api/crack              — {hash, charset?, max_length?} start a crack
    POST /api/crack/packet       — {hex, charset?, max_length?} crack from a packet
"""

import argparse
import json
import os
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from ..config import DEFAULT_CONFIG_DIR, load_config
from ..crypto import extract_group_payload
from ..store import CollectorStore
from .cracker_app import CrackerApp

STATIC_DIR = Path(__file__).parent / "static"

# Static files the server will serve, with content types. Keeps path handling
# to an explicit allowlist (no traversal).
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
}


def _int_or_none(values):
    """First value of a parse_qs list as int, or None."""
    if not values:
        return None
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return None


def _str_or_none(values):
    if not values:
        return None
    v = values[0]
    return v if v != "" else None


def make_handler(app: CrackerApp):
    """Build a request handler class bound to the given CrackerApp."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)

            if path == "/" or path == "/index.html":
                self._static("index.html")
                return
            if path.lstrip("/") in STATIC_FILES:
                self._static(path.lstrip("/"))
                return

            if path == "/api/channels/messages":
                self._call(lambda: self._messages(qs))
                return

            get_routes = {
                "/api/gpu": app.gpu_status,
                "/api/config": app.config,
                "/api/pending": app.pending_channels,
                "/api/channels": app.channels,
                "/api/results": app.results,
                "/api/crack/status": app.crack_status,
                "/api/exhausted": app.exhausted_channels,
            }
            handler = get_routes.get(path)
            if handler:
                self._call(handler)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/") or "/"
            body = self._read_body()

            if path == "/api/crack":
                self._call(lambda: self._crack(body))
            elif path == "/api/crack/packet":
                self._call(lambda: self._crack_packet(body))
            elif path == "/api/crack/sweep":
                self._call(lambda: app.enqueue_sweep(
                    charset=body.get("charset") or None,
                    max_length=int(body["max_length"]) if body.get("max_length") else None))
            elif path == "/api/crack/cancel":
                self._call(lambda: self._cancel(body))
            elif path == "/api/crack/retry":
                self._call(lambda: app.retry_hash(body["hash"]) if body.get("hash") is not None
                           else {"started": False, "error": "missing 'hash'"})
            elif path == "/api/config":
                self._call(lambda: app.update_settings(body))
            elif path == "/api/wordlist":
                self._call(lambda: app.add_custom_wordlist(
                    text=body.get("text"), path=body.get("path")))
            else:
                self._json(404, {"error": "not found"})

        # --- GET handlers ---

        def _messages(self, qs):
            channel = _str_or_none(qs.get("channel"))
            if not channel:
                return {"error": "missing 'channel'"}
            limit = _int_or_none(qs.get("limit")) or 50
            before_id = _int_or_none(qs.get("before_id"))
            after_id = _int_or_none(qs.get("after_id"))
            search = _str_or_none(qs.get("search"))
            return app.channel_messages(
                channel, limit=limit, before_id=before_id,
                after_id=after_id, search=search,
            )

        # --- POST handlers ---

        def _crack(self, body):
            target_hash = body.get("hash")
            if target_hash is None:
                return {"started": False, "error": "missing 'hash'"}
            charset = body.get("charset") or None
            max_length = body.get("max_length")
            max_length = int(max_length) if max_length else None
            return app.start_crack(
                int(target_hash), charset=charset, max_length=max_length,
            )

        def _crack_packet(self, body):
            hex_str = (body.get("hex") or "").strip().replace(" ", "")
            if not hex_str:
                return {"started": False, "error": "missing 'hex'"}
            try:
                raw = bytes.fromhex(hex_str)
            except ValueError as e:
                return {"started": False, "error": f"bad hex: {e}"}
            extracted = extract_group_payload(raw)
            if not extracted:
                return {"started": False, "error": "not a GRP_TXT/GRP_DATA packet"}
            charset = body.get("charset") or None
            max_length = body.get("max_length")
            max_length = int(max_length) if max_length else None
            return app.start_crack(
                extracted["channel_hash"],
                mac_and_data=extracted["mac_and_data"],
                charset=charset, max_length=max_length,
            )

        def _cancel(self, body):
            if body.get("all"):
                return app.cancel_all()
            job_id = body.get("job_id")
            if job_id is None:
                return {"ok": False, "error": "missing 'job_id' (or 'all': true)"}
            return app.cancel(job_id)

        # --- helpers ---

        def _read_body(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def _call(self, fn):
            try:
                self._json(200, fn())
            except Exception as e:
                self._json(500, {"error": str(e)})

        def _json(self, status, data):
            body = json.dumps(data, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _static(self, name):
            content_type = STATIC_FILES.get(name)
            if content_type is None:
                self._json(404, {"error": "not found"})
                return
            fpath = STATIC_DIR / name
            try:
                data = fpath.read_bytes()
            except OSError:
                self._json(404, {"error": f"{name} not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            pass  # quiet

    return Handler


def build_offline_app(db_path, use_gpu=None):
    """Open a store for offline cracking (no hardware) and wrap it."""
    store = CollectorStore(db_path)
    store.open()
    return CrackerApp(store, core=None, use_gpu=use_gpu), store


def build_live_app(port, baud, password, db_path, use_gpu=None):
    """Attach to a live device: start a CollectorCore with a passive cracker."""
    from ..core import CollectorCore
    from ..cracker import ChannelCracker

    core = CollectorCore(port=port, baud=baud, db_path=db_path, password=password)
    core.on_connected = lambda: print("  [collector] Connected")
    core.on_disconnected = lambda r: print(f"  [collector] Disconnected: {r}")
    core.on_error = lambda m: print(f"  [collector] Error: {m}")
    core.start()

    # Wait briefly for the core to open its store, then attach a cracker.
    deadline = time.monotonic() + 5.0
    while core.store is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if core.store is not None:
        cracker = ChannelCracker(store=core.store)
        core.set_cracker(cracker)
        cracker.start()
        app = CrackerApp(core.store, core=core, use_gpu=use_gpu)
    else:
        # Fall back to opening the DB directly if the core hasn't yet.
        store = CollectorStore(db_path)
        store.open()
        app = CrackerApp(store, core=core, use_gpu=use_gpu)
    return app, core


def main():
    parser = argparse.ArgumentParser(
        description="MeshCore Collector — channel-cracker web app"
    )
    parser.add_argument("--port", default=None,
                        help="Serial/socket port for live mode (omit for offline)")
    parser.add_argument("--live", action="store_true",
                        help="Live mode against MESHCORE_HOST from the environment/.env "
                             "(TCP 5005); password from MESHCORE_PASSWORD. Ignored if --port is given")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate")
    parser.add_argument("--password", default=None,
                        help="Admin password for socket:// ports (or MESHCORE_PASSWORD)")
    parser.add_argument("--db", default=None, help="SQLite database path")
    parser.add_argument("--http-port", type=int,
                        default=int(os.environ.get("PORT") or 8090),
                        help="HTTP port (defaults to $PORT when set, e.g. under Portico, else 8090)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--cpu", action="store_true",
                        help="Force the CPU brute-forcer even if a GPU is present")
    parser.add_argument("--seed", type=int, default=0, metavar="N",
                        help="Dev: seed the DB with N demo GRP_TXT messages, then serve")
    args = parser.parse_args()

    # --live: derive the socket port from MESHCORE_HOST (.env), so the device
    # address/credentials stay in .env rather than on the command line.
    if args.live and not args.port:
        from ..envfile import load_env
        load_env()
        host = os.environ.get("MESHCORE_HOST")
        if not host:
            parser.error("--live needs MESHCORE_HOST set (environment or .env)")
        args.port = f"socket://{host}:5005"

    config = load_config()
    db_path = args.db or config.get("db_path", "collector.db")
    if not os.path.isabs(db_path):
        db_path = str(DEFAULT_CONFIG_DIR / db_path)

    use_gpu = False if args.cpu else None

    core = None
    if args.port:
        print("MeshCore Cracker Web App (live mode)")
        print(f"  Serial:   {args.port} @ {args.baud}")
        print(f"  Database: {db_path}")
        app, core = build_live_app(args.port, args.baud, args.password, db_path, use_gpu)
    else:
        print("MeshCore Cracker Web App (offline mode)")
        print(f"  Database: {db_path}")
        app, _ = build_offline_app(db_path, use_gpu)

    if args.seed:
        from .seed import seed_demo_db
        print(f"  Seeding:  {args.seed} demo messages ...", flush=True)
        summary = seed_demo_db(app.store, total=args.seed)
        print(f"  Seeded:   {summary['messages']} msgs across "
              f"{summary['channels']} channels "
              f"({summary['cracked']} cracked, {summary['pending']} pending)")

    gpu = app.gpu_status()
    print(f"  Engine:   {gpu['engine']}" + (f" ({gpu['name']})" if gpu["name"] else ""))

    httpd = ThreadingHTTPServer((args.host, args.http_port), make_handler(app))
    print(f"  Web UI:   http://{args.host}:{args.http_port}/")
    print()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        httpd.server_close()
        app.shutdown()
        if core is not None:
            core.stop()


if __name__ == "__main__":
    main()
