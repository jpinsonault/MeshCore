#!/usr/bin/env python3
"""
MeshCore Collector — JSON API server.

Lightweight HTTP server exposing collector data as JSON. Designed for headless
deployments (e.g., Raspberry Pi) where you access data from a browser on
another machine.

Usage:
    python -m collector.api --port /dev/ttyUSB0 --http-port 8080

Endpoints:
    GET /api/status             — collector status + latest heartbeat
    GET /api/stats              — summary statistics
    GET /api/nodes              — all known nodes
    GET /api/packets            — recent packets (?limit=50&direction=rx&payload_type=5)
    GET /api/advertisements     — recent advertisements (?limit=50)
    GET /api/heartbeats         — heartbeat history (?limit=50)
    GET /api/traffic            — packet counts by type
    GET /api/channels           — channel summary (decoded group messages)
    GET /api/channels/messages  — channel messages (?channel=X&limit=N)
"""

import argparse
import json
import os
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from .config import load_config, DEFAULT_CONFIG_DIR
from .core import CollectorCore


class CollectorAPI:
    """Holds references to the core + provides query methods for the handler."""

    def __init__(self, core: CollectorCore):
        self.core = core
        self._start_time = time.time()

    def get_status(self):
        return {
            "running": self.core.is_running,
            "connected": self.core.is_connected,
            "port": self.core.port,
            "uptime_secs": int(time.time() - self._start_time),
        }

    def get_stats(self):
        store = self.core.store
        if not store:
            return {"error": "store not ready"}
        return store.get_stats()

    def get_nodes(self):
        store = self.core.store
        if not store:
            return []
        return store.get_nodes()

    def get_packets(self, limit=50, direction=None, payload_type=None):
        store = self.core.store
        if not store:
            return []
        return store.get_recent_packets(limit=limit, direction=direction, payload_type=payload_type)

    def get_advertisements(self, limit=50):
        store = self.core.store
        if not store:
            return []
        return store.get_recent_advertisements(limit=limit)

    def get_heartbeats(self, limit=50):
        store = self.core.store
        if not store:
            return []
        return store.get_heartbeats(limit=limit)

    def get_traffic(self):
        store = self.core.store
        if not store:
            return []
        return store.get_traffic_by_type()

    def get_channels(self):
        store = self.core.store
        if not store:
            return []
        return store.get_channel_summary()

    def get_channel_messages(self, channel=None, limit=100, search=None, sender=None):
        store = self.core.store
        if not store:
            return []
        if search or sender:
            return store.search_channel_messages(
                channel_name=channel, search_text=search,
                sender=sender, limit=limit,
            )
        return store.get_channel_messages(channel_name=channel, limit=limit)


def make_handler(api: CollectorAPI):
    """Create a request handler class bound to the given API instance."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            params = parse_qs(parsed.query)

            routes = {
                "/api/status": lambda: api.get_status(),
                "/api/stats": lambda: api.get_stats(),
                "/api/nodes": lambda: api.get_nodes(),
                "/api/packets": lambda: api.get_packets(
                    limit=int(params.get("limit", [50])[0]),
                    direction=params.get("direction", [None])[0],
                    payload_type=_int_or_none(params.get("payload_type", [None])[0]),
                ),
                "/api/advertisements": lambda: api.get_advertisements(
                    limit=int(params.get("limit", [50])[0]),
                ),
                "/api/heartbeats": lambda: api.get_heartbeats(
                    limit=int(params.get("limit", [50])[0]),
                ),
                "/api/traffic": lambda: api.get_traffic(),
                "/api/channels": lambda: api.get_channels(),
                "/api/channels/messages": lambda: api.get_channel_messages(
                    channel=params.get("channel", [None])[0],
                    limit=int(params.get("limit", [100])[0]),
                    search=params.get("search", [None])[0],
                    sender=params.get("sender", [None])[0],
                ),
            }

            handler = routes.get(path)
            if handler:
                try:
                    data = handler()
                    self._json_response(200, data)
                except Exception as e:
                    self._json_response(500, {"error": str(e)})
            else:
                self._json_response(404, {"error": "not found", "endpoints": list(routes.keys())})

        def _json_response(self, status, data):
            body = json.dumps(data, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            # Quieter logging
            pass

    return Handler


def _int_or_none(val):
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser(description="MeshCore Collector JSON API Server")
    parser.add_argument("--port", default=None, help="Serial port")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate")
    parser.add_argument("--http-port", type=int, default=8080, help="HTTP port for JSON API")
    parser.add_argument("--db", default=None, help="SQLite database path")
    args = parser.parse_args()

    config = load_config()
    serial_port = args.port or config.get("port")
    if not serial_port:
        print("Error: No serial port specified. Use --port or set it in config.")
        sys.exit(1)

    db_path = args.db or config.get("db_path", "collector.db")
    if not os.path.isabs(db_path):
        db_path = str(DEFAULT_CONFIG_DIR / db_path)

    print(f"MeshCore Collector API Server")
    print(f"  Serial:   {serial_port} @ {args.baud}")
    print(f"  Database: {db_path}")
    print(f"  API:      http://0.0.0.0:{args.http_port}/api/")
    print()

    core = CollectorCore(port=serial_port, baud=args.baud, db_path=db_path)
    core.on_connected = lambda: print("  [collector] Connected")
    core.on_disconnected = lambda r: print(f"  [collector] Disconnected: {r}")
    core.on_error = lambda m: print(f"  [collector] Error: {m}")

    api = CollectorAPI(core)

    # Start collector on background thread
    core.start()

    # Start HTTP server
    handler_class = make_handler(api)
    httpd = HTTPServer(("0.0.0.0", args.http_port), handler_class)
    print(f"  [api] Listening on port {args.http_port}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        httpd.server_close()
        core.stop()


if __name__ == "__main__":
    main()
