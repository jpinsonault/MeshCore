#!/usr/bin/env python3
"""
MeshCore Collector — WebSocket + HTTP server.

Composes CollectorCore (serial + store), the HTTP API (CollectorAPI), and a
WebSocket server that streams all events to connected clients in real time.

Usage:
    python -m collector.server --port /dev/cu.usbserial-0001
    python -m collector.server --port /dev/ttyUSB0 --ws-port 8081 --http-port 8080
"""

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from http.server import HTTPServer

import websockets

from .api import CollectorAPI, make_handler
from .config import DEFAULT_CONFIG_DIR, load_config
from .core import CollectorCore
from .protocol import FRAME_TYPE_NAMES


def _serialize_frame(frame):
    """Convert a frame dict to a JSON-safe dict (bytes -> hex strings)."""
    out = {"type_name": FRAME_TYPE_NAMES.get(frame.get("type"), "UNKNOWN")}
    parsed = frame.get("parsed")
    if parsed:
        safe = {}
        for k, v in parsed.items():
            if isinstance(v, bytes):
                safe[k] = v.hex()
            else:
                safe[k] = v
        out["parsed"] = safe
    return out


def _serialize_channel_message(msg):
    """Convert a channel message object to a JSON-safe dict."""
    return {
        "sender": msg.sender,
        "text": msg.text,
        "channel_name": msg.channel_name,
        "channel_hash": msg.channel_hash,
        "timestamp": msg.timestamp,
    }


class CollectorServer:
    """WebSocket + HTTP server that wraps a CollectorCore.

    Chains into the core's callbacks without replacing any existing ones,
    and restores originals on stop().

    Args:
        core: A CollectorCore instance (caller manages its lifecycle).
        http_port: Port for the JSON HTTP API.
        ws_port: Port for the WebSocket server.
        host: Bind address (default "0.0.0.0").
    """

    def __init__(self, core, http_port=8080, ws_port=8081, host="0.0.0.0"):
        self._core = core
        self._http_port = http_port
        self._ws_port = ws_port
        self._host = host

        self._http_thread = None
        self._ws_thread = None
        self._httpd = None
        self._ws_loop = None
        self._ws_server = None
        self._clients = set()
        self._queue = None
        self._running = False

        # Saved original callbacks for restoration
        self._orig_on_frame = None
        self._orig_on_text = None
        self._orig_on_connected = None
        self._orig_on_disconnected = None
        self._orig_on_channel_message = None

    @property
    def is_running(self):
        return self._running

    @property
    def url(self):
        return f"http://{self._host}:{self._http_port}"

    @property
    def ws_url(self):
        return f"ws://{self._host}:{self._ws_port}"

    def start(self):
        """Start HTTP and WebSocket server threads."""
        if self._running:
            return
        self._running = True
        self._chain_callbacks()
        self._start_http()
        self._start_ws()

    def stop(self):
        """Gracefully shut down both servers and restore callbacks."""
        if not self._running:
            return
        self._running = False
        self._restore_callbacks()

        # Stop HTTP
        if self._httpd:
            self._httpd.shutdown()
        if self._http_thread and self._http_thread.is_alive():
            self._http_thread.join(timeout=3)

        # Stop WS
        if self._ws_loop and self._ws_loop.is_running():
            self._ws_loop.call_soon_threadsafe(self._ws_loop.stop)
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=3)

    # --- Callback chaining ---

    def _chain_callbacks(self):
        """Wrap core callbacks to also enqueue WS events."""
        self._orig_on_frame = self._core.on_frame
        self._orig_on_text = self._core.on_text
        self._orig_on_connected = self._core.on_connected
        self._orig_on_disconnected = self._core.on_disconnected
        self._orig_on_channel_message = self._core.on_channel_message

        orig_frame = self._orig_on_frame
        def chained_frame(frame):
            if orig_frame:
                orig_frame(frame)
            self._enqueue("frame", _serialize_frame(frame))
        self._core.on_frame = chained_frame

        orig_text = self._orig_on_text
        def chained_text(line):
            if orig_text:
                orig_text(line)
            self._enqueue("text", {"line": line})
        self._core.on_text = chained_text

        orig_connected = self._orig_on_connected
        def chained_connected():
            if orig_connected:
                orig_connected()
            self._enqueue("connected", {})
        self._core.on_connected = chained_connected

        orig_disconnected = self._orig_on_disconnected
        def chained_disconnected(reason):
            if orig_disconnected:
                orig_disconnected(reason)
            self._enqueue("disconnected", {"reason": reason})
        self._core.on_disconnected = chained_disconnected

        orig_channel = self._orig_on_channel_message
        def chained_channel(msg):
            if orig_channel:
                orig_channel(msg)
            self._enqueue("channel_message", _serialize_channel_message(msg))
        self._core.on_channel_message = chained_channel

    def _restore_callbacks(self):
        """Restore original callbacks."""
        self._core.on_frame = self._orig_on_frame
        self._core.on_text = self._orig_on_text
        self._core.on_connected = self._orig_on_connected
        self._core.on_disconnected = self._orig_on_disconnected
        self._core.on_channel_message = self._orig_on_channel_message

    def _enqueue(self, event_type, data):
        """Thread-safe enqueue of a WS message."""
        if not self._running or not self._ws_loop or not self._queue:
            return
        msg = json.dumps({
            "event": event_type,
            "timestamp": time.time(),
            "data": data,
        }, default=str)
        try:
            self._ws_loop.call_soon_threadsafe(self._queue.put_nowait, msg)
        except RuntimeError:
            pass  # loop closed

    # --- HTTP server ---

    def _start_http(self):
        api = CollectorAPI(self._core)
        handler_class = make_handler(api)
        self._httpd = HTTPServer((self._host, self._http_port), handler_class)
        self._http_thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )
        self._http_thread.start()

    # --- WebSocket server ---

    def _start_ws(self):
        self._ws_thread = threading.Thread(target=self._ws_run, daemon=True)
        self._ws_thread.start()

    def _ws_run(self):
        self._ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._ws_loop)
        self._queue = asyncio.Queue()
        self._ws_loop.run_until_complete(self._ws_serve())

    async def _ws_serve(self):
        self._ws_server = await websockets.serve(
            self._ws_handler, self._host, self._ws_port
        )
        broadcast_task = asyncio.create_task(self._broadcast_loop())
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            await asyncio.Future()  # run forever until loop.stop()
        except asyncio.CancelledError:
            pass
        finally:
            broadcast_task.cancel()
            heartbeat_task.cancel()
            self._ws_server.close()
            await self._ws_server.wait_closed()

    async def _ws_handler(self, websocket):
        self._clients.add(websocket)
        # Send initial status
        status = json.dumps({
            "event": "status",
            "timestamp": time.time(),
            "data": {
                "connected": self._core.is_connected,
                "port": self._core.port,
            },
        }, default=str)
        try:
            await websocket.send(status)
            async for _ in websocket:
                pass  # consume client messages (none expected)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(websocket)

    async def _broadcast_loop(self):
        while True:
            msg = await self._queue.get()
            if not self._clients:
                continue
            dead = set()
            for ws in self._clients:
                try:
                    await ws.send(msg)
                except websockets.ConnectionClosed:
                    dead.add(ws)
            self._clients -= dead

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(30)
            msg = json.dumps({
                "event": "server_heartbeat",
                "timestamp": time.time(),
                "data": {"clients": len(self._clients)},
            })
            dead = set()
            for ws in self._clients:
                try:
                    await ws.send(msg)
                except websockets.ConnectionClosed:
                    dead.add(ws)
            self._clients -= dead


def main():
    parser = argparse.ArgumentParser(
        description="MeshCore Collector — WebSocket + HTTP Server"
    )
    parser.add_argument("--port", default=None, help="Serial port")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate")
    parser.add_argument("--http-port", type=int, default=8080, help="HTTP API port")
    parser.add_argument("--ws-port", type=int, default=8081, help="WebSocket port")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
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

    print("MeshCore Collector Server")
    print(f"  Serial:    {serial_port} @ {args.baud}")
    print(f"  Database:  {db_path}")
    print(f"  HTTP API:  http://{args.host}:{args.http_port}/api/")
    print(f"  WebSocket: ws://{args.host}:{args.ws_port}")
    print()

    core = CollectorCore(port=serial_port, baud=args.baud, db_path=db_path)
    core.on_connected = lambda: print("  [collector] Connected")
    core.on_disconnected = lambda r: print(f"  [collector] Disconnected: {r}")
    core.on_error = lambda m: print(f"  [collector] Error: {m}")

    server = CollectorServer(
        core,
        http_port=args.http_port,
        ws_port=args.ws_port,
        host=args.host,
    )

    core.start()
    server.start()

    print(f"  [server] HTTP listening on {args.host}:{args.http_port}")
    print(f"  [server] WebSocket listening on {args.host}:{args.ws_port}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.stop()
        core.stop()


if __name__ == "__main__":
    main()
