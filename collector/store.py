"""
MeshCore Collector — SQLite storage layer.

Stores all captured mesh traffic for offline analysis. Designed to be
append-heavy with periodic reads from the dashboard/API.
"""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .protocol import (
    FRAME_TYPE_ADVERTISEMENT,
    FRAME_TYPE_HEARTBEAT,
    FRAME_TYPE_RX_RAW,
    FRAME_TYPE_TX_RAW,
)

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS raw_packets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL    NOT NULL,
    direction   TEXT    NOT NULL,  -- 'rx' or 'tx'
    snr         REAL,
    rssi        INTEGER,
    route_type  INTEGER,
    payload_type INTEGER,
    raw_hex     TEXT    NOT NULL,
    raw_len     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS advertisements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL    NOT NULL,
    device_time INTEGER,
    snr         REAL,
    pub_key_hex TEXT    NOT NULL,
    adv_type    INTEGER,
    adv_type_name TEXT,
    name        TEXT,
    lat         REAL,
    lon         REAL
);

CREATE TABLE IF NOT EXISTS nodes (
    pub_key_hex TEXT PRIMARY KEY,
    name        TEXT,
    adv_type    INTEGER,
    adv_type_name TEXT,
    lat         REAL,
    lon         REAL,
    first_seen  REAL    NOT NULL,
    last_seen   REAL    NOT NULL,
    advert_count INTEGER DEFAULT 0,
    rx_count    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS heartbeats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL    NOT NULL,
    device_time INTEGER,
    battery_mv  INTEGER,
    rx_flood    INTEGER,
    rx_direct   INTEGER,
    tx_flood    INTEGER,
    tx_direct   INTEGER,
    free_pkts   INTEGER,
    uptime_secs INTEGER
);

CREATE INDEX IF NOT EXISTS idx_raw_packets_ts ON raw_packets(timestamp);
CREATE INDEX IF NOT EXISTS idx_raw_packets_payload ON raw_packets(payload_type);
CREATE INDEX IF NOT EXISTS idx_advertisements_ts ON advertisements(timestamp);
CREATE INDEX IF NOT EXISTS idx_advertisements_key ON advertisements(pub_key_hex);
CREATE INDEX IF NOT EXISTS idx_nodes_last_seen ON nodes(last_seen);
CREATE INDEX IF NOT EXISTS idx_heartbeats_ts ON heartbeats(timestamp);
"""


class CollectorStore:
    """SQLite storage for captured mesh data."""

    def __init__(self, db_path="collector.db"):
        self.db_path = Path(db_path)
        self._conn = None

    def open(self):
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        self._conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    @contextmanager
    def _tx(self):
        with self._conn:
            yield self._conn

    def store_frame(self, frame):
        """Store a parsed frame. Dispatches by frame type."""
        ft = frame["type"]
        parsed = frame.get("parsed")
        if not parsed or "error" in parsed:
            return

        now = frame.get("received_at", time.time())

        if ft == FRAME_TYPE_RX_RAW:
            self._store_rx(now, parsed)
        elif ft == FRAME_TYPE_TX_RAW:
            self._store_tx(now, parsed)
        elif ft == FRAME_TYPE_ADVERTISEMENT:
            self._store_advertisement(now, parsed)
        elif ft == FRAME_TYPE_HEARTBEAT:
            self._store_heartbeat(now, parsed)

    def _store_rx(self, now, p):
        raw_hex = p.get("raw", b"").hex() if isinstance(p.get("raw"), bytes) else ""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO raw_packets (timestamp, direction, snr, rssi, route_type, payload_type, raw_hex, raw_len) "
                "VALUES (?, 'rx', ?, ?, ?, ?, ?, ?)",
                (now, p.get("snr"), p.get("rssi"), p.get("route_type"), p.get("payload_type"), raw_hex, p.get("raw_len", 0)),
            )

    def _store_tx(self, now, p):
        raw_hex = p.get("raw", b"").hex() if isinstance(p.get("raw"), bytes) else ""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO raw_packets (timestamp, direction, snr, rssi, route_type, payload_type, raw_hex, raw_len) "
                "VALUES (?, 'tx', NULL, NULL, ?, ?, ?, ?)",
                (now, p.get("route_type"), p.get("payload_type"), raw_hex, p.get("raw_len", 0)),
            )

    def _store_advertisement(self, now, p):
        pub_key_hex = p.get("pub_key_hex", "")
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO advertisements (timestamp, device_time, snr, pub_key_hex, adv_type, adv_type_name, name, lat, lon) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now, p.get("timestamp"), p.get("snr"), pub_key_hex,
                    p.get("adv_type"), p.get("adv_type_name"),
                    p.get("name"), p.get("lat"), p.get("lon"),
                ),
            )
            # Upsert node
            conn.execute(
                """INSERT INTO nodes (pub_key_hex, name, adv_type, adv_type_name, lat, lon, first_seen, last_seen, advert_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(pub_key_hex) DO UPDATE SET
                       name = COALESCE(excluded.name, nodes.name),
                       adv_type = COALESCE(excluded.adv_type, nodes.adv_type),
                       adv_type_name = COALESCE(excluded.adv_type_name, nodes.adv_type_name),
                       lat = COALESCE(excluded.lat, nodes.lat),
                       lon = COALESCE(excluded.lon, nodes.lon),
                       last_seen = excluded.last_seen,
                       advert_count = nodes.advert_count + 1""",
                (
                    pub_key_hex, p.get("name"), p.get("adv_type"), p.get("adv_type_name"),
                    p.get("lat"), p.get("lon"), now, now,
                ),
            )

    def _store_heartbeat(self, now, p):
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO heartbeats (timestamp, device_time, battery_mv, rx_flood, rx_direct, tx_flood, tx_direct, free_pkts, uptime_secs) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now, p.get("timestamp"), p.get("battery_mv"),
                    p.get("rx_flood"), p.get("rx_direct"),
                    p.get("tx_flood"), p.get("tx_direct"),
                    p.get("free_pkts"), p.get("uptime_secs"),
                ),
            )

    # --- Query methods (used by API and dashboard) ---

    def get_stats(self):
        """Return summary statistics."""
        c = self._conn
        row = c.execute("SELECT COUNT(*) as cnt FROM raw_packets").fetchone()
        total_packets = row["cnt"]
        row = c.execute("SELECT COUNT(*) as cnt FROM raw_packets WHERE direction='rx'").fetchone()
        rx_count = row["cnt"]
        row = c.execute("SELECT COUNT(*) as cnt FROM raw_packets WHERE direction='tx'").fetchone()
        tx_count = row["cnt"]
        row = c.execute("SELECT COUNT(*) as cnt FROM nodes").fetchone()
        node_count = row["cnt"]
        row = c.execute("SELECT COUNT(*) as cnt FROM advertisements").fetchone()
        advert_count = row["cnt"]

        hb = c.execute("SELECT * FROM heartbeats ORDER BY timestamp DESC LIMIT 1").fetchone()
        latest_heartbeat = dict(hb) if hb else None

        return {
            "total_packets": total_packets,
            "rx_count": rx_count,
            "tx_count": tx_count,
            "node_count": node_count,
            "advert_count": advert_count,
            "latest_heartbeat": latest_heartbeat,
        }

    def get_nodes(self):
        """Return all known nodes, most recently seen first."""
        rows = self._conn.execute(
            "SELECT * FROM nodes ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_packets(self, limit=50, direction=None, payload_type=None):
        """Return recent packets with optional filters."""
        sql = "SELECT * FROM raw_packets WHERE 1=1"
        params = []
        if direction:
            sql += " AND direction = ?"
            params.append(direction)
        if payload_type is not None:
            sql += " AND payload_type = ?"
            params.append(payload_type)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_recent_advertisements(self, limit=50):
        """Return recent advertisements."""
        rows = self._conn.execute(
            "SELECT * FROM advertisements ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_heartbeats(self, limit=50):
        """Return recent heartbeats."""
        rows = self._conn.execute(
            "SELECT * FROM heartbeats ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_traffic_by_type(self):
        """Return packet counts grouped by payload type."""
        rows = self._conn.execute(
            "SELECT payload_type, COUNT(*) as cnt FROM raw_packets GROUP BY payload_type ORDER BY cnt DESC"
        ).fetchall()
        return [dict(r) for r in rows]
