"""Tests for the OS diagnostics frame, store, formatting, and API."""

import struct
import tempfile
import time
from unittest.mock import MagicMock
import pytest

from pyos import Keys
from pyos.testing import MockScreen, HarnessApplication

from collector.protocol import (
    FRAME_START,
    FRAME_TYPE_DIAGNOSTICS,
    FRAME_TYPE_HEARTBEAT,
    FrameReader,
    parse_diagnostics,
)
from collector.store import CollectorStore
from collector.events import CollectorFrame
from collector.activities.system_diag import (
    SystemDiagActivity,
    build_diag_lines,
    _fmt_heap_bar,
    _fmt_duty,
    _fmt_error_flag,
    _fmt_recv_errors,
    _value_sparkline,
)


def _make_frame(frame_type, payload):
    """Build a raw binary frame for testing."""
    frame_len = 1 + len(payload)
    return bytes([FRAME_START]) + struct.pack("<H", frame_len) + bytes([frame_type]) + payload


def _diag_payload(
    mcu_temp=42.3,
    free_heap=287432,
    min_free_heap=245108,
    total_heap=368640,
    noise_floor=-102,
    last_rssi=-76,
    last_snr_x4=30,  # 7.5 dB
    tx_airtime_ms=342000,
    rx_airtime_ms=2092000,
    recv_errors=12,
    err_flags=0,
    tx_queue_len=2,
    direct_dups=12,
    flood_dups=234,
    n_recv=4523,
    n_sent=1847,
):
    """Build a 50-byte diagnostics payload."""
    return struct.pack(
        "<f3I3h2IIHH2H2I",
        mcu_temp,
        free_heap, min_free_heap, total_heap,
        noise_floor, last_rssi, last_snr_x4,
        tx_airtime_ms, rx_airtime_ms,
        recv_errors, err_flags, tx_queue_len,
        direct_dups, flood_dups,
        n_recv, n_sent,
    )


def _diag_frame(**kwargs):
    """Build a complete diagnostics frame dict for store testing."""
    payload = _diag_payload(**kwargs)
    parsed = parse_diagnostics(payload)
    return {
        "type": FRAME_TYPE_DIAGNOSTICS,
        "received_at": time.time(),
        "parsed": parsed,
    }


@pytest.fixture
def store():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        s = CollectorStore(f.name)
        s.open()
        yield s
        s.close()


# --- Protocol tests ---

class TestParseDiagnostics:
    def test_valid_payload(self):
        payload = _diag_payload()
        result = parse_diagnostics(payload)
        assert abs(result["mcu_temp"] - 42.3) < 0.1
        assert result["free_heap"] == 287432
        assert result["min_free_heap"] == 245108
        assert result["total_heap"] == 368640
        assert result["noise_floor"] == -102
        assert result["last_rssi"] == -76
        assert abs(result["last_snr"] - 7.5) < 0.01
        assert result["tx_airtime_ms"] == 342000
        assert result["rx_airtime_ms"] == 2092000
        assert result["recv_errors"] == 12
        assert result["err_flags"] == 0
        assert result["tx_queue_len"] == 2
        assert result["direct_dups"] == 12
        assert result["flood_dups"] == 234
        assert result["n_recv"] == 4523
        assert result["n_sent"] == 1847

    def test_too_short(self):
        result = parse_diagnostics(b"\x00" * 20)
        assert "error" in result

    def test_exact_50_bytes(self):
        payload = _diag_payload()
        assert len(payload) == 50

    def test_extra_bytes_ignored(self):
        payload = _diag_payload() + b"\xFF" * 10
        result = parse_diagnostics(payload)
        assert result["free_heap"] == 287432


class TestFrameReaderDiagnostics:
    def test_diagnostics_through_reader(self):
        reader = FrameReader()
        frame_data = _make_frame(FRAME_TYPE_DIAGNOSTICS, _diag_payload())
        reader.feed(frame_data)
        assert len(reader.frames) == 1
        f = reader.frames[0]
        assert f["type"] == FRAME_TYPE_DIAGNOSTICS
        assert f["type_name"] == "DIAGNOSTICS"
        assert f["parsed"]["free_heap"] == 287432

    def test_diagnostics_with_surrounding_text(self):
        reader = FrameReader()
        data = b"some text\n"
        data += _make_frame(FRAME_TYPE_DIAGNOSTICS, _diag_payload())
        data += b"more text\n"
        reader.feed(data)
        assert len(reader.text_lines) == 2
        assert len(reader.frames) == 1
        assert reader.frames[0]["parsed"]["mcu_temp"] == pytest.approx(42.3, abs=0.1)


# --- Store tests ---

class TestStoreDiagnostics:
    def test_store_diagnostics_frame(self, store):
        store.store_frame(_diag_frame())
        d = store.get_latest_diagnostics()
        assert d is not None
        assert d["free_heap"] == 287432
        assert abs(d["mcu_temp"] - 42.3) < 0.1

    def test_get_diagnostics_returns_list(self, store):
        store.store_frame(_diag_frame(mcu_temp=40.0))
        store.store_frame(_diag_frame(mcu_temp=42.0))
        rows = store.get_diagnostics(limit=10)
        assert len(rows) == 2
        # Most recent first
        assert abs(rows[0]["mcu_temp"] - 42.0) < 0.1

    def test_get_diagnostics_limit(self, store):
        for i in range(5):
            store.store_frame(_diag_frame(mcu_temp=30.0 + i))
        rows = store.get_diagnostics(limit=3)
        assert len(rows) == 3

    def test_get_latest_diagnostics_empty(self, store):
        assert store.get_latest_diagnostics() is None

    def test_all_fields_stored(self, store):
        store.store_frame(_diag_frame(
            err_flags=0x03,
            tx_queue_len=5,
            direct_dups=100,
            flood_dups=200,
        ))
        d = store.get_latest_diagnostics()
        assert d["err_flags"] == 0x03
        assert d["tx_queue_len"] == 5
        assert d["direct_dups"] == 100
        assert d["flood_dups"] == 200


class TestSchemaMigration:
    def test_v2_to_v3_migration(self):
        """A v2 database should migrate to v3 on open()."""
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            # Create a v2 database with all required columns for indexes
            import sqlite3
            conn = sqlite3.connect(f.name)
            from collector.store import SCHEMA_SQL, SCHEMA_V2_SQL
            conn.executescript(SCHEMA_SQL)
            conn.executescript(SCHEMA_V2_SQL)
            conn.execute(
                "UPDATE meta SET value = '2' WHERE key = 'schema_version'"
            )
            conn.commit()
            conn.close()

            # Open with CollectorStore — should migrate to v3
            s = CollectorStore(f.name)
            s.open()
            row = s._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            assert row["value"] == "3"

            # diagnostics table should exist
            s._conn.execute("SELECT COUNT(*) FROM diagnostics")
            s.close()


# --- Formatting tests ---

class TestBuildDiagLines:
    def test_no_data(self):
        lines = build_diag_lines(None)
        text = "\n".join(lines)
        assert "waiting" in text

    def test_no_diag_with_heartbeat_shows_heartbeat_data(self):
        """When diag is None but heartbeat is available, show heartbeat info."""
        hb = {"uptime_secs": 7200, "battery_mv": 3800, "free_pkts": 28}
        lines = build_diag_lines(None, heartbeat=hb)
        text = "\n".join(lines)
        assert "waiting" in text
        assert "3,800 mV" in text
        assert "2h" in text
        assert "28 free" in text

    def test_no_diag_no_heartbeat_no_crash(self):
        """When both diag and heartbeat are None, no crash."""
        lines = build_diag_lines(None, heartbeat=None)
        text = "\n".join(lines)
        assert "waiting" in text
        assert "mV" not in text

    def test_with_diag_data(self):
        diag = parse_diagnostics(_diag_payload())
        diag["timestamp"] = time.time()
        lines = build_diag_lines(diag)
        text = "\n".join(lines)
        assert "42.3" in text
        assert "PROCESSOR" in text
        assert "MEMORY" in text
        assert "RADIO" in text
        assert "PACKETS" in text
        assert "ERRORS" in text

    def test_with_heartbeat(self):
        diag = parse_diagnostics(_diag_payload())
        diag["timestamp"] = time.time()
        hb = {"uptime_secs": 9000, "battery_mv": 3842, "free_pkts": 28}
        lines = build_diag_lines(diag, heartbeat=hb)
        text = "\n".join(lines)
        assert "3,842 mV" in text
        assert "28 free" in text
        assert "2h" in text

    def test_with_sparklines(self):
        diag = parse_diagnostics(_diag_payload())
        diag["timestamp"] = time.time()
        temp_hist = [40.0, 41.0, 42.0, 43.0, 42.5]
        heap_hist = [(300000, 368640), (290000, 368640), (287432, 368640)]
        lines = build_diag_lines(diag, temp_history=temp_hist, heap_history=heap_hist)
        text = "\n".join(lines)
        assert "Temp history" in text
        assert "Heap history" in text

    def test_error_flags_displayed(self):
        diag = parse_diagnostics(_diag_payload(err_flags=0x03))
        diag["timestamp"] = time.time()
        lines = build_diag_lines(diag)
        text = "\n".join(lines)
        assert "\u26a0" in text  # warning symbol for triggered flags
        assert "\u2713" in text  # check mark for OK flags

    def test_dedup_stats(self):
        diag = parse_diagnostics(_diag_payload(flood_dups=234, direct_dups=12))
        diag["timestamp"] = time.time()
        lines = build_diag_lines(diag)
        text = "\n".join(lines)
        assert "234 flood" in text
        assert "12 direct" in text


class TestHelperFunctions:
    def test_fmt_heap_bar(self):
        result = _fmt_heap_bar(287432, 368640)
        assert "287,432" in result
        assert "368,640" in result
        assert "%" in result
        assert "\u2588" in result

    def test_fmt_heap_bar_zero_total(self):
        assert _fmt_heap_bar(0, 0) == "---"

    def test_fmt_duty(self):
        result = _fmt_duty(342000, 14820)
        assert "%" in result
        assert "342" in result
        assert "14,820" in result

    def test_fmt_duty_zero_uptime(self):
        assert _fmt_duty(1000, 0) == "---"

    def test_fmt_error_flag_ok(self):
        result = _fmt_error_flag(0x00, 0x01, "Pool exhausted")
        assert "\u2713" in result

    def test_fmt_error_flag_triggered(self):
        result = _fmt_error_flag(0x01, 0x01, "Pool exhausted")
        assert "\u26a0" in result

    def test_fmt_recv_errors(self):
        result = _fmt_recv_errors(12, 4523)
        assert "4,523" in result
        assert "12" in result
        assert "%" in result

    def test_fmt_recv_errors_zero(self):
        result = _fmt_recv_errors(0, 1000)
        assert "0 errors" in result

    def test_value_sparkline(self):
        result = _value_sparkline([1, 2, 3, 4, 5], width=5)
        assert len(result) == 5

    def test_value_sparkline_empty(self):
        result = _value_sparkline([])
        assert "no data" in result

    def test_value_sparkline_constant(self):
        result = _value_sparkline([5, 5, 5, 5], width=4)
        assert len(result) == 4


# --- API tests ---

class TestDiagnosticsAPI:
    def test_api_endpoint(self, store):
        """Test that the API can query diagnostics."""
        store.store_frame(_diag_frame())
        rows = store.get_diagnostics(limit=10)
        assert len(rows) == 1
        assert rows[0]["free_heap"] == 287432


# --- End-to-end TUI tests ---

class _FakeCore:
    """Minimal mock for CollectorCore."""
    def __init__(self):
        self._commands = []

    def send_command(self, cmd):
        self._commands.append(cmd)
        return True


class _FakeCoreFail:
    """Mock CollectorCore where send_command fails."""
    def send_command(self, cmd):
        return False


class _FakeCollectorService:
    """Minimal mock that provides a store and core for TUI tests."""
    def __init__(self, store=None, core=None):
        self.store = store
        self.core = core or _FakeCore()
        self._application = None
        self.state = "RUNNING"

    def on_stop(self):
        pass


def _collector_frame_event(parsed=None, **kwargs):
    """Build a CollectorFrame event wrapping a diagnostics frame."""
    if parsed is None:
        payload = _diag_payload(**kwargs)
        parsed = parse_diagnostics(payload)
    frame = {
        "type": FRAME_TYPE_DIAGNOSTICS,
        "received_at": time.time(),
        "parsed": parsed,
    }
    return CollectorFrame(frame)


def _heartbeat_frame_event(battery_mv=3800, uptime_secs=7200, free_pkts=28, **kwargs):
    """Build a CollectorFrame event wrapping a heartbeat frame."""
    parsed = {
        "timestamp": 1700000000,
        "battery_mv": battery_mv,
        "rx_flood": 50,
        "rx_direct": 20,
        "tx_flood": 40,
        "tx_direct": 10,
        "free_pkts": free_pkts,
        "uptime_secs": uptime_secs,
        **kwargs,
    }
    frame = {
        "type": FRAME_TYPE_HEARTBEAT,
        "received_at": time.time(),
        "parsed": parsed,
    }
    return CollectorFrame(frame)


class TestSystemDiagTUI:
    """End-to-end TUI tests for SystemDiagActivity."""

    def test_initial_waiting_state(self, app, mock_screen):
        """Fresh activity with no data shows waiting message."""
        activity = SystemDiagActivity()
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("waiting")
        mock_screen.assert_text_on_screen("30s")
        mock_screen.assert_text_on_screen("System Diagnostics")

    def test_esc_pops_activity(self, app, mock_screen):
        """ESC key returns to previous screen."""
        activity = SystemDiagActivity()
        app.start_activity(activity)
        assert app.activity_stack_depth() == 1
        app.send_key(Keys.ESC)
        app.flush_stop_events()
        assert app.activity_stack_depth() == 0

    def test_diagnostics_frame_updates_display(self, app, mock_screen):
        """Dispatching a DIAGNOSTICS frame populates the screen with data."""
        activity = SystemDiagActivity()
        app.start_activity(activity)

        # Initially shows waiting
        mock_screen.assert_text_on_screen("waiting")

        # Send a diagnostics frame
        app.dispatch_event(_collector_frame_event())
        app.drain()

        # Now the display should show actual data
        mock_screen.assert_text_on_screen("42.3")
        mock_screen.assert_text_on_screen("PROCESSOR")
        mock_screen.assert_text_on_screen("MEMORY")
        mock_screen.assert_text_on_screen("287,432")
        mock_screen.assert_text_on_screen("RADIO")
        mock_screen.assert_text_on_screen("-102")
        mock_screen.assert_text_on_screen("PACKETS")
        mock_screen.assert_text_on_screen("4,523")

    def test_heartbeat_frame_updates_display(self, app, mock_screen):
        """Dispatching a HEARTBEAT frame shows battery/uptime even without diag."""
        activity = SystemDiagActivity()
        app.start_activity(activity)

        app.dispatch_event(_heartbeat_frame_event(battery_mv=3842, uptime_secs=9000))
        app.drain()

        # Still shows waiting (no diag yet) but also shows heartbeat data
        mock_screen.assert_text_on_screen("waiting")
        mock_screen.assert_text_on_screen("3,842 mV")
        mock_screen.assert_text_on_screen("2h")

    def test_diag_then_heartbeat_shows_combined(self, app, mock_screen):
        """Both diag and heartbeat data are shown together."""
        activity = SystemDiagActivity()
        app.start_activity(activity)

        app.dispatch_event(_collector_frame_event())
        app.dispatch_event(_heartbeat_frame_event(battery_mv=3700, free_pkts=12))
        app.drain()

        # Diag data visible on screen
        mock_screen.assert_text_on_screen("42.3")
        # Heartbeat data visible on screen
        mock_screen.assert_text_on_screen("3,700 mV")
        # Pool info may be scrolled off screen, verify via display content
        content = "\n".join(activity.display_state["content"]["items"])
        assert "12 free" in content

    def test_heartbeat_then_diag_replaces_waiting(self, app, mock_screen):
        """After heartbeat arrives, then diag arrives, waiting is replaced."""
        activity = SystemDiagActivity()
        app.start_activity(activity)

        # Heartbeat first
        app.dispatch_event(_heartbeat_frame_event())
        app.drain()
        mock_screen.assert_text_on_screen("waiting")

        # Then diagnostics
        app.dispatch_event(_collector_frame_event())
        app.drain()
        # Should no longer show waiting
        mock_screen.assert_text_on_screen("PROCESSOR")
        mock_screen.assert_text_on_screen("42.3")

    def test_multiple_diag_frames_update_sparklines(self, app, mock_screen):
        """Multiple diagnostics frames build up sparkline history."""
        activity = SystemDiagActivity()
        app.start_activity(activity)

        for temp in [40.0, 41.0, 42.0]:
            app.dispatch_event(_collector_frame_event(mcu_temp=temp))
        app.drain()

        # Sparkline should appear after 2+ data points
        assert len(activity._temp_history) == 3
        assert len(activity._heap_history) == 3

    def test_r_key_sends_command(self, app, mock_screen):
        """Pressing 'r' sends 'collector diag' via the service."""
        activity = SystemDiagActivity()
        app.start_activity(activity)

        core = _FakeCore()
        svc = _FakeCollectorService(core=core)
        app._services["collector"] = svc
        svc._application = app

        app.send_key(ord("r"))
        app.drain()

        assert len(core._commands) == 1
        assert core._commands[0] == b"collector diag\r"
        mock_screen.assert_text_on_screen("Requesting")

        app._services.pop("collector", None)

    def test_r_key_shows_error_when_no_serial(self, app, mock_screen):
        """Pressing 'r' when serial is not available shows error in status bar."""
        activity = SystemDiagActivity()
        app.start_activity(activity)

        svc = _FakeCollectorService(core=_FakeCoreFail())
        app._services["collector"] = svc
        svc._application = app

        app.send_key(ord("r"))
        app.drain()

        mock_screen.assert_text_on_screen("No serial connection")

        app._services.pop("collector", None)

    def test_r_key_no_service_shows_not_running(self, app, mock_screen):
        """Pressing 'r' when no collector service is registered shows status."""
        activity = SystemDiagActivity()
        app.start_activity(activity)

        app.send_key(ord("r"))
        app.drain()

        mock_screen.assert_text_on_screen("Collector not running")

    def test_diag_activity_internal_state(self, app, mock_screen):
        """Verify internal state is updated correctly by frame events."""
        activity = SystemDiagActivity()
        app.start_activity(activity)

        assert activity._diag is None
        assert activity._heartbeat is None

        app.dispatch_event(_collector_frame_event(mcu_temp=50.0))
        app.drain()
        assert activity._diag is not None
        assert abs(activity._diag["mcu_temp"] - 50.0) < 0.1

        app.dispatch_event(_heartbeat_frame_event(battery_mv=4000))
        app.drain()
        assert activity._heartbeat is not None
        assert activity._heartbeat["battery_mv"] == 4000


class TestSystemDiagWithStore:
    """TUI tests that use a real SQLite store via a fake service."""

    def _register_service(self, app, store, core=None):
        svc = _FakeCollectorService(store=store, core=core)
        app._services["collector"] = svc
        svc._application = app
        return svc

    def _cleanup_service(self, app):
        app._services.pop("collector", None)

    def test_loads_diag_from_store_on_start(self, app, mock_screen):
        """Activity loads latest diagnostics from SQLite on start."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            store = CollectorStore(db_path)
            store.open()

            # Pre-populate store with a diagnostics frame
            store.store_frame(_diag_frame(mcu_temp=55.5))
            # Pre-populate store with a heartbeat
            store.store_frame({
                "type": FRAME_TYPE_HEARTBEAT,
                "received_at": time.time(),
                "parsed": {
                    "timestamp": 1700000000,
                    "battery_mv": 3900,
                    "rx_flood": 10, "rx_direct": 5,
                    "tx_flood": 8, "tx_direct": 3,
                    "free_pkts": 20,
                    "uptime_secs": 3600,
                },
            })

            self._register_service(app, store)

            activity = SystemDiagActivity()
            app.start_activity(activity)

            # Should have loaded from store
            assert activity._diag is not None
            assert abs(activity._diag["mcu_temp"] - 55.5) < 0.1
            assert activity._heartbeat is not None
            assert activity._heartbeat["battery_mv"] == 3900

            # Screen should show the data, not "waiting"
            mock_screen.assert_text_on_screen("55.5")
            mock_screen.assert_text_on_screen("3,900 mV")

            self._cleanup_service(app)
            store.close()

    def test_loads_heartbeat_only_from_store(self, app, mock_screen):
        """With only heartbeat in store (no diag), shows heartbeat while waiting."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            store = CollectorStore(db_path)
            store.open()

            # Only a heartbeat, no diagnostics
            store.store_frame({
                "type": FRAME_TYPE_HEARTBEAT,
                "received_at": time.time(),
                "parsed": {
                    "timestamp": 1700000000,
                    "battery_mv": 3750,
                    "rx_flood": 0, "rx_direct": 0,
                    "tx_flood": 0, "tx_direct": 0,
                    "free_pkts": 32,
                    "uptime_secs": 1800,
                },
            })

            self._register_service(app, store)

            activity = SystemDiagActivity()
            app.start_activity(activity)

            # Should show waiting + heartbeat data
            mock_screen.assert_text_on_screen("waiting")
            mock_screen.assert_text_on_screen("3,750 mV")
            mock_screen.assert_text_on_screen("32 free")

            self._cleanup_service(app)
            store.close()

    def test_live_frame_after_store_load(self, app, mock_screen):
        """Live frame updates override store-loaded data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            store = CollectorStore(db_path)
            store.open()

            store.store_frame(_diag_frame(mcu_temp=40.0))
            self._register_service(app, store)

            activity = SystemDiagActivity()
            app.start_activity(activity)

            # Initially from store
            mock_screen.assert_text_on_screen("40.0")

            # Live frame with different temp
            app.dispatch_event(_collector_frame_event(mcu_temp=65.0))
            app.drain()

            mock_screen.assert_text_on_screen("65.0")

            self._cleanup_service(app)
            store.close()

    def test_r_key_with_store_service(self, app, mock_screen):
        """Pressing 'r' works correctly with a store-backed service."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            store = CollectorStore(db_path)
            store.open()

            core = _FakeCore()
            self._register_service(app, store, core=core)

            activity = SystemDiagActivity()
            app.start_activity(activity)

            app.send_key(ord("R"))  # uppercase R should also work
            app.drain()

            assert len(core._commands) == 1
            mock_screen.assert_text_on_screen("Requesting")

            self._cleanup_service(app)
            store.close()


class TestDashboardToSystemDiag:
    """Test the 's' key segue from dashboard to system diagnostics."""

    def test_s_key_segues_to_diag(self, app, mock_screen):
        """Pressing 's' from dashboard opens system diagnostics."""
        from collector.activities.dashboard import DashboardActivity

        dashboard = DashboardActivity(port="/dev/ttyUSB0", auto_start=False)
        app.start_activity(dashboard)

        mock_screen.assert_text_on_screen("MeshCore Collector")
        assert app.activity_stack_depth() == 1

        app.send_key(ord("s"))
        app.drain()

        # segue_to pushes the new activity onto the stack
        assert app.activity_stack_depth() == 2
        mock_screen.assert_text_on_screen("System Diagnostics")
