"""
MeshCore Collector — SQLite storage layer.

Stores all captured mesh traffic for offline analysis. Designed to be
append-heavy with periodic reads from the dashboard/API.
"""

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .protocol import (
    FRAME_TYPE_ADVERTISEMENT,
    FRAME_TYPE_DIAGNOSTICS,
    FRAME_TYPE_HEARTBEAT,
    FRAME_TYPE_RX_RAW,
    FRAME_TYPE_TX_RAW,
)

SCHEMA_VERSION = 9

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

SCHEMA_V2_SQL = """
CREATE TABLE IF NOT EXISTS channel_messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      REAL    NOT NULL,
    msg_timestamp  INTEGER,
    channel_name   TEXT    NOT NULL,
    channel_hash   INTEGER NOT NULL,
    sender         TEXT,
    text           TEXT,
    raw_packet_id  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_channel_messages_ts ON channel_messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_channel_messages_channel ON channel_messages(channel_name);
"""

SCHEMA_V3_SQL = """
CREATE TABLE IF NOT EXISTS diagnostics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL NOT NULL,
    mcu_temp        REAL,
    free_heap       INTEGER,
    min_free_heap   INTEGER,
    total_heap      INTEGER,
    noise_floor     INTEGER,
    last_rssi       INTEGER,
    last_snr        REAL,
    tx_airtime_ms   INTEGER,
    rx_airtime_ms   INTEGER,
    recv_errors     INTEGER,
    err_flags       INTEGER,
    tx_queue_len    INTEGER,
    direct_dups     INTEGER,
    flood_dups      INTEGER,
    n_recv          INTEGER,
    n_sent          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_diagnostics_ts ON diagnostics(timestamp);
"""

SCHEMA_V4_SQL = """
ALTER TABLE raw_packets ADD COLUMN seq INTEGER;
CREATE INDEX IF NOT EXISTS idx_raw_packets_seq ON raw_packets(seq);
"""

SCHEMA_V5_SQL = """
CREATE TABLE IF NOT EXISTS cracked_channels (
    channel_hash   INTEGER NOT NULL,
    channel_name   TEXT    NOT NULL UNIQUE,
    discovered_at  REAL    NOT NULL,
    decoded_count  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cracked_hash ON cracked_channels(channel_hash);
"""

SCHEMA_V6_SQL = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,          -- JSON-encoded value
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_channel_messages_id_channel
    ON channel_messages(channel_name, id);
"""

SCHEMA_V7_SQL = """
CREATE TABLE IF NOT EXISTS crack_attempts (
    channel_hash INTEGER NOT NULL,
    charset      TEXT    NOT NULL,
    max_length   INTEGER NOT NULL,
    result       TEXT    NOT NULL,          -- 'exhausted' = swept, not found
    packets_seen INTEGER NOT NULL DEFAULT 0, -- packet count for this hash when attempted
    attempted_at REAL    NOT NULL,
    PRIMARY KEY (channel_hash, charset, max_length)
);
"""

# How each cracked channel was recovered (dictionary / rules / bruteforce).
SCHEMA_V8_SQL = """
ALTER TABLE cracked_channels ADD COLUMN method TEXT;
"""

# Durable link up/down log. Survives host restarts so the connection history
# (when the device dropped, how long it was gone, why) isn't lost to process
# memory — the ephemeral stdout log is gone the moment the service restarts.
SCHEMA_V9_SQL = """
CREATE TABLE IF NOT EXISTS connection_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  REAL NOT NULL,
    event      TEXT NOT NULL,   -- 'connected' | 'disconnected'
    detail     TEXT,            -- handshake info / disconnect reason
    gap_secs   REAL             -- 'connected': downtime since last link; 'disconnected': session length
);
CREATE INDEX IF NOT EXISTS idx_connection_events_ts ON connection_events(timestamp);
"""


class CollectorStore:
    """SQLite storage for captured mesh data."""

    def __init__(self, db_path="collector.db"):
        self.db_path = Path(db_path)
        # One SQLite connection per thread for file DBs: a single connection
        # can't be used concurrently from the webapp's worker + HTTP threads
        # (corrupts cursors), but WAL allows one connection per thread with
        # concurrent reads and a serialized writer. In-memory DBs can't be
        # shared across connections, so they keep one shared connection.
        self._is_memory = (str(self.db_path) == ":memory:")
        self._local = threading.local()
        self._shared = None

    def _new_conn(self):
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")  # wait out a concurrent writer
        return conn

    @property
    def _conn(self):
        if self._is_memory:
            if self._shared is None:
                self._shared = self._new_conn()
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
        return conn

    def open(self):
        conn = self._conn  # create this thread's connection
        conn.executescript(SCHEMA_SQL)
        # Insert base version (1) for fresh DBs; existing DBs keep their value
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", "1"),
        )
        self._migrate()
        conn.commit()

    def _migrate(self):
        """Run schema migrations if needed."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        current = int(row["value"]) if row else 1

        if current < 2:
            self._conn.executescript(SCHEMA_V2_SQL)
            self._conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(2),),
            )
            current = 2

        if current < 3:
            self._conn.executescript(SCHEMA_V3_SQL)
            self._conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(3),),
            )
            current = 3

        if current < 4:
            self._conn.executescript(SCHEMA_V4_SQL)
            self._conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(4),),
            )
            current = 4

        if current < 5:
            self._conn.executescript(SCHEMA_V5_SQL)
            self._conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(5),),
            )
            current = 5

        if current < 6:
            self._conn.executescript(SCHEMA_V6_SQL)
            self._conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(6),),
            )
            current = 6

        if current < 7:
            self._conn.executescript(SCHEMA_V7_SQL)
            self._conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(7),),
            )
            current = 7

        if current < 8:
            self._conn.executescript(SCHEMA_V8_SQL)
            self._conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(8),),
            )
            current = 8

        if current < 9:
            self._conn.executescript(SCHEMA_V9_SQL)
            self._conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(9),),
            )
            current = 9

    def close(self):
        if self._is_memory:
            if self._shared is not None:
                self._shared.close()
                self._shared = None
            return
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def _tx(self):
        with self._conn:
            yield self._conn

    def get_last_committed_seq(self) -> int:
        """Return the last committed sequence number (default 0)."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'last_committed_seq'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def set_last_committed_seq(self, seq: int):
        """Upsert the last committed sequence number."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("last_committed_seq", str(seq)),
            )

    def store_frame(self, frame):
        """Store a parsed frame. Dispatches by frame type. Returns raw_packets rowid for RX/TX, else None."""
        ft = frame["type"]
        parsed = frame.get("parsed")
        if not parsed or "error" in parsed:
            return None

        now = frame.get("received_at", time.time())
        seq = frame.get("seq")

        if ft == FRAME_TYPE_RX_RAW:
            return self._store_rx(now, parsed, seq=seq)
        elif ft == FRAME_TYPE_TX_RAW:
            return self._store_tx(now, parsed, seq=seq)
        elif ft == FRAME_TYPE_ADVERTISEMENT:
            self._store_advertisement(now, parsed)
        elif ft == FRAME_TYPE_HEARTBEAT:
            self._store_heartbeat(now, parsed)
        elif ft == FRAME_TYPE_DIAGNOSTICS:
            self._store_diagnostics(now, parsed)
        return None

    def _store_rx(self, now, p, seq=None):
        raw_hex = p.get("raw", b"").hex() if isinstance(p.get("raw"), bytes) else ""
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO raw_packets (timestamp, direction, snr, rssi, route_type, payload_type, raw_hex, raw_len, seq) "
                "VALUES (?, 'rx', ?, ?, ?, ?, ?, ?, ?)",
                (now, p.get("snr"), p.get("rssi"), p.get("route_type"), p.get("payload_type"), raw_hex, p.get("raw_len", 0), seq),
            )
            return cursor.lastrowid

    def _store_tx(self, now, p, seq=None):
        raw_hex = p.get("raw", b"").hex() if isinstance(p.get("raw"), bytes) else ""
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO raw_packets (timestamp, direction, snr, rssi, route_type, payload_type, raw_hex, raw_len, seq) "
                "VALUES (?, 'tx', NULL, NULL, ?, ?, ?, ?, ?)",
                (now, p.get("route_type"), p.get("payload_type"), raw_hex, p.get("raw_len", 0), seq),
            )
            return cursor.lastrowid

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

    def _store_diagnostics(self, now, p):
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO diagnostics "
                "(timestamp, mcu_temp, free_heap, min_free_heap, total_heap, "
                "noise_floor, last_rssi, last_snr, tx_airtime_ms, rx_airtime_ms, "
                "recv_errors, err_flags, tx_queue_len, direct_dups, flood_dups, "
                "n_recv, n_sent) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now, p.get("mcu_temp"), p.get("free_heap"),
                    p.get("min_free_heap"), p.get("total_heap"),
                    p.get("noise_floor"), p.get("last_rssi"), p.get("last_snr"),
                    p.get("tx_airtime_ms"), p.get("rx_airtime_ms"),
                    p.get("recv_errors"), p.get("err_flags"), p.get("tx_queue_len"),
                    p.get("direct_dups"), p.get("flood_dups"),
                    p.get("n_recv"), p.get("n_sent"),
                ),
            )

    def store_channel_message(self, msg, raw_packet_id=None):
        """Store a decoded channel message."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO channel_messages "
                "(timestamp, msg_timestamp, channel_name, channel_hash, sender, text, raw_packet_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    msg.raw_timestamp, msg.timestamp,
                    msg.channel_name, msg.channel_hash,
                    msg.sender, msg.text, raw_packet_id,
                ),
            )

    def get_channel_messages(self, channel_name=None, limit=100, offset=0):
        """Return channel messages, optionally filtered by channel."""
        sql = "SELECT * FROM channel_messages WHERE 1=1"
        params = []
        if channel_name:
            sql += " AND channel_name = ?"
            params.append(channel_name)
        sql += " ORDER BY timestamp ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def search_channel_messages(self, channel_name=None, search_text=None,
                                sender=None, limit=200, offset=0):
        """Search channel messages with optional text, sender, and channel filters.

        Uses SQL LIKE for text/sender matching (case-insensitive for ASCII).
        Escapes '%' and '_' in search terms to prevent wildcard injection.
        """
        sql = "SELECT * FROM channel_messages WHERE 1=1"
        params = []
        if channel_name:
            sql += " AND channel_name = ?"
            params.append(channel_name)
        if search_text:
            escaped = search_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " AND (text LIKE ? ESCAPE '\\' OR sender LIKE ? ESCAPE '\\')"
            pattern = f"%{escaped}%"
            params.extend([pattern, pattern])
        if sender:
            escaped = sender.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " AND sender LIKE ? ESCAPE '\\'"
            params.append(f"%{escaped}%")
        sql += " ORDER BY timestamp ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_channel_summary(self):
        """Return per-channel summary: count, last_activity, unique senders."""
        rows = self._conn.execute(
            "SELECT channel_name, channel_hash, "
            "COUNT(*) as msg_count, "
            "MAX(timestamp) as last_activity, "
            "COUNT(DISTINCT sender) as unique_senders "
            "FROM channel_messages "
            "GROUP BY channel_name "
            "ORDER BY last_activity DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_channel_message_count(self):
        """Return total number of decoded channel messages."""
        row = self._conn.execute("SELECT COUNT(*) as cnt FROM channel_messages").fetchone()
        return row["cnt"]

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

        row = c.execute("SELECT COUNT(*) as cnt FROM channel_messages").fetchone()
        channel_msg_count = row["cnt"]

        return {
            "total_packets": total_packets,
            "rx_count": rx_count,
            "tx_count": tx_count,
            "node_count": node_count,
            "advert_count": advert_count,
            "latest_heartbeat": latest_heartbeat,
            "channel_msg_count": channel_msg_count,
        }

    def get_nodes(self):
        """Return all known nodes, most recently seen first."""
        rows = self._conn.execute(
            "SELECT * FROM nodes ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_nodes_by_type(self, adv_type):
        """Return nodes filtered by advertisement type, most recently seen first."""
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE adv_type = ? ORDER BY last_seen DESC",
            (adv_type,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_node_count_by_type(self):
        """Return {adv_type: count} dict for all node types."""
        rows = self._conn.execute(
            "SELECT adv_type, COUNT(*) as cnt FROM nodes GROUP BY adv_type"
        ).fetchall()
        return {r["adv_type"]: r["cnt"] for r in rows}

    def get_advertisement_snr_history(self, pub_key_hex, limit=50):
        """Return [(timestamp, snr)] for a node, oldest first."""
        rows = self._conn.execute(
            "SELECT timestamp, snr FROM advertisements "
            "WHERE pub_key_hex = ? AND snr IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT ?",
            (pub_key_hex, limit),
        ).fetchall()
        return [(r["timestamp"], r["snr"]) for r in reversed(rows)]

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

    def get_recent_packet_timestamps(self, limit=1000):
        """Return recent packet timestamps (newest first) for sparkline/rate rebuild."""
        rows = self._conn.execute(
            "SELECT timestamp FROM raw_packets ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["timestamp"] for r in rows]

    def get_traffic_by_type(self):
        """Return packet counts grouped by payload type."""
        rows = self._conn.execute(
            "SELECT payload_type, COUNT(*) as cnt FROM raw_packets GROUP BY payload_type ORDER BY cnt DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_diagnostics(self):
        """Return the most recent diagnostics row, or None."""
        row = self._conn.execute(
            "SELECT * FROM diagnostics ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def get_diagnostics(self, limit=50):
        """Return recent diagnostics rows."""
        rows = self._conn.execute(
            "SELECT * FROM diagnostics ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Connection event log ---

    def record_connection_event(self, event, detail=None, gap_secs=None, now=None):
        """Append a durable link up/down event.

        event: 'connected' or 'disconnected'. gap_secs is the downtime before a
        reconnect ('connected') or the session length ('disconnected'). Persisted
        so the link history outlives the collector process.
        """
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO connection_events (timestamp, event, detail, gap_secs) "
                "VALUES (?, ?, ?, ?)",
                (now if now is not None else time.time(), event, detail, gap_secs),
            )

    def get_connection_events(self, limit=100):
        """Return recent connection events, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM connection_events ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Cracker cache methods ---

    def store_cracked_channel(self, name, channel_hash, decoded_count, method=None):
        """INSERT OR REPLACE a cracked channel into the cache."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cracked_channels "
                "(channel_hash, channel_name, discovered_at, decoded_count, method) "
                "VALUES (?, ?, ?, ?, ?)",
                (channel_hash, name, time.time(), decoded_count, method),
            )

    def get_cracked_channels(self):
        """Return all cached cracked channel entries."""
        rows = self._conn.execute(
            "SELECT * FROM cracked_channels ORDER BY discovered_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def has_channel_message_for_packet(self, raw_packet_id):
        """Check whether a channel_message already exists for a given raw_packet_id."""
        row = self._conn.execute(
            "SELECT 1 FROM channel_messages WHERE raw_packet_id = ? LIMIT 1",
            (raw_packet_id,),
        ).fetchone()
        return row is not None

    def get_grp_txt_packets(self, limit=10000):
        """Return RX raw_packets with payload_type=5 (GRP_TXT) for cracker scanning."""
        rows = self._conn.execute(
            "SELECT id, timestamp, raw_hex FROM raw_packets "
            "WHERE payload_type = 5 AND direction = 'rx' "
            "ORDER BY timestamp ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Paginated / cursor message queries (scale to thousands of rows) ---

    def _message_filter(self, channel_name, search):
        """Build the shared WHERE clause + params for message queries."""
        sql = ""
        params = []
        if channel_name:
            sql += " AND channel_name = ?"
            params.append(channel_name)
        if search:
            escaped = (search.replace("\\", "\\\\")
                       .replace("%", "\\%").replace("_", "\\_"))
            sql += " AND (text LIKE ? ESCAPE '\\' OR sender LIKE ? ESCAPE '\\')"
            pattern = f"%{escaped}%"
            params.extend([pattern, pattern])
        return sql, params

    # Relay flooding means the same message is captured in several raw packets
    # (identical sender/text/sender-timestamp, different path). Collapse those to
    # one logical message in reads, keeping the per-packet rows that the
    # undecoded/collision tracking relies on.
    _DEDUP_GROUP = " GROUP BY channel_name, msg_timestamp, sender, text"

    def count_channel_messages(self, channel_name=None, search=None):
        """Count distinct decoded messages (relay duplicates collapsed)."""
        where, params = self._message_filter(channel_name, search)
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM (SELECT 1 FROM channel_messages "
            "WHERE 1=1" + where + self._DEDUP_GROUP + ")",
            params,
        ).fetchone()
        return row["cnt"]

    def page_channel_messages(self, channel_name=None, search=None,
                              before_id=None, after_id=None, limit=50):
        """Return a page of messages ordered newest-first, with id cursors.

        - ``before_id``: only rows with id < before_id (scroll back to older).
        - ``after_id``: only rows with id > after_id (fetch newer than a cursor);
          still returned newest-first.
        Use either cursor, not both. ``limit`` is clamped to [1, 500].
        """
        limit = max(1, min(int(limit), 500))
        inner_where, params = self._message_filter(channel_name, search)
        # Canonical id = the first (lowest-id) copy of each logical message.
        sql = ("SELECT * FROM channel_messages WHERE id IN ("
               "SELECT MIN(id) FROM channel_messages WHERE 1=1" + inner_where
               + self._DEDUP_GROUP + ")")
        if before_id is not None:
            sql += " AND id < ?"
            params.append(int(before_id))
        if after_id is not None:
            sql += " AND id > ?"
            params.append(int(after_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_cracked_channel_summaries(self):
        """Cracked channels joined with their live message counts + last activity.

        Uses a GROUP BY aggregate rather than loading message bodies, so it stays
        cheap with thousands of messages per channel.
        """
        rows = self._conn.execute(
            "SELECT c.channel_name, c.channel_hash, c.discovered_at, "
            "       c.decoded_count, c.method, "
            "       COUNT(m.id) AS msg_count, "
            "       MAX(m.timestamp) AS last_activity, "
            "       COUNT(DISTINCT m.sender) AS unique_senders "
            "FROM cracked_channels c "
            "LEFT JOIN channel_messages m ON m.channel_name = c.channel_name "
            "GROUP BY c.channel_name "
            "ORDER BY c.discovered_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_named_channel_summaries(self):
        """Every channel that has decoded messages (includes the auto-decoded
        public channel, not just cracked ones), with counts + how it was found.

        LEFT JOINs cracked_channels so the public channel (messages but no crack
        row) still appears; its method/channel_hash come back NULL for the caller
        to fill in.
        """
        rows = self._conn.execute(
            "SELECT m.channel_name, "
            "       COUNT(DISTINCT COALESCE(m.msg_timestamp,'') || '|' || "
            "             COALESCE(m.sender,'') || '|' || COALESCE(m.text,'')) AS msg_count, "
            "       MAX(m.timestamp) AS last_activity, "
            "       COUNT(DISTINCT m.sender) AS unique_senders, "
            "       c.channel_hash AS channel_hash, "
            "       c.discovered_at AS discovered_at, "
            "       c.decoded_count AS decoded_count, "
            "       c.method AS method "
            "FROM channel_messages m "
            "LEFT JOIN cracked_channels c ON c.channel_name = m.channel_name "
            "GROUP BY m.channel_name "
            "ORDER BY last_activity DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Settings persistence (durable UI / cracker config) ---

    def get_setting(self, key, default=None):
        """Return a JSON-decoded setting value, or ``default`` if absent."""
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return default

    def set_setting(self, key, value):
        """Upsert a JSON-encodable setting value."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, json.dumps(value), time.time()),
            )

    def get_all_settings(self):
        """Return all settings as a {key: decoded_value} dict."""
        rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        out = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except (ValueError, TypeError):
                continue
        return out

    # --- Crack-attempt cache (don't re-grind exhausted channels) ---

    def record_crack_attempt(self, channel_hash, charset, max_length,
                             result="exhausted", packets_seen=0):
        """Record that (channel_hash, charset, max_length) was swept with the
        given outcome ('exhausted' = fully searched, not found)."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO crack_attempts "
                "(channel_hash, charset, max_length, result, packets_seen, attempted_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(channel_hash, charset, max_length) DO UPDATE SET "
                "result = excluded.result, packets_seen = excluded.packets_seen, "
                "attempted_at = excluded.attempted_at",
                (int(channel_hash), charset, int(max_length), result,
                 int(packets_seen), time.time()),
            )

    def get_crack_attempt(self, channel_hash, charset, max_length):
        """Return the attempt row for exactly these params, or None."""
        return self._conn.execute(
            "SELECT * FROM crack_attempts WHERE channel_hash = ? AND charset = ? "
            "AND max_length = ?",
            (int(channel_hash), charset, int(max_length)),
        ).fetchone()

    def get_crack_attempts(self):
        """All recorded crack attempts (for the UI's exhausted list)."""
        return self._conn.execute(
            "SELECT * FROM crack_attempts ORDER BY attempted_at DESC"
        ).fetchall()

    def clear_crack_attempt(self, channel_hash):
        """Forget all exhausted marks for a hash so it can be retried."""
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM crack_attempts WHERE channel_hash = ?",
                (int(channel_hash),),
            )

    def clear_all_crack_attempts(self):
        with self._tx() as conn:
            conn.execute("DELETE FROM crack_attempts")
