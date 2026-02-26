"""Tests for BuzzerBoard TUI.

Uses MockScreen + HarnessApplication from pyos.testing.
Serial and BLE communication are mocked -- no hardware needed.
"""

import pytest
from unittest.mock import patch, MagicMock

from pyos.testing import MockScreen, HarnessApplication
from pyos import Keys, Attrs
from pyos.Service import Service

from buzzerboard_tui.notes import (
    note_freq,
    key_to_note_and_freq,
    KEY_TO_NOTE,
    KEY_TO_NOTE_SHARP,
)
from buzzerboard_tui.recorder import Recorder
from buzzerboard_tui.instrument import InstrumentActivity
from buzzerboard_tui.port_picker import PortPickerActivity


# ---------------------------------------------------------------------------
# Mock serial service
# ---------------------------------------------------------------------------


class MockBuzzerSerial(Service):
    """Fake serial service that captures commands."""

    def __init__(self):
        super().__init__()
        self.commands = []
        self.port = "/dev/mock"

    def on_start(self):
        self._running_event.set()

    def on_stop(self):
        pass

    def play_tone(self, freq, duration_ms=150):
        self.commands.append(("TONE", freq, duration_ms))

    def tone_start(self, freq):
        self.commands.append(("TONE_START", freq))

    def play_rtttl(self, rtttl_str):
        self.commands.append(("RTTTL", rtttl_str))
        return "+OK PLAYING"

    def stop_playback(self):
        self.commands.append(("STOP",))

    def send_command(self, cmd):
        self.commands.append(("CMD", cmd))
        if cmd == "PING":
            return "+PONG"
        return "+OK"


@pytest.fixture
def mock_serial():
    return MockBuzzerSerial()


@pytest.fixture
def instrument_app(app, mock_serial):
    """HarnessApplication with mock serial service, ready for InstrumentActivity."""
    app.register_service("buzzer_serial", mock_serial)
    app.start_service_sync("buzzer_serial")
    return app


# ===========================================================================
# 1. Note frequency mapping
# ===========================================================================


class TestNoteFrequencies:
    def test_a4_is_440(self):
        assert note_freq("A", 4) == 440

    def test_c5_is_523(self):
        assert note_freq("C", 5) == 523

    def test_c4_is_262(self):
        assert note_freq("C", 4) == 262

    def test_octave_doubles_frequency(self):
        f4 = note_freq("A", 4)
        f5 = note_freq("A", 5)
        assert f5 == f4 * 2  # 880 == 440 * 2

    def test_all_chromatic_notes_ascending(self):
        """Each note in a chromatic scale should be higher than the previous."""
        names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
        freqs = [note_freq(n, 4) for n in names]
        for i in range(1, len(freqs)):
            assert freqs[i] > freqs[i - 1]


# ===========================================================================
# 2. Key-to-note mapping
# ===========================================================================


class TestKeyMapping:
    def test_a_key_is_c(self):
        result = key_to_note_and_freq(ord("a"), 5)
        assert result is not None
        name, freq = result
        assert name == "C5"
        assert freq == note_freq("C", 5)

    def test_w_key_is_c_sharp(self):
        result = key_to_note_and_freq(ord("w"), 5)
        assert result is not None
        name, freq = result
        assert name == "C#5"

    def test_k_key_is_c_next_octave(self):
        result = key_to_note_and_freq(ord("k"), 5)
        assert result is not None
        name, freq = result
        assert name == "C6"
        assert freq == note_freq("C", 6)

    def test_unknown_key_returns_none(self):
        assert key_to_note_and_freq(ord("z"), 5) is None

    def test_octave_affects_frequency(self):
        _, freq4 = key_to_note_and_freq(ord("a"), 4)
        _, freq5 = key_to_note_and_freq(ord("a"), 5)
        assert freq5 > freq4


# ===========================================================================
# 3. Recorder
# ===========================================================================


class TestRecorder:
    def test_initial_state(self):
        r = Recorder()
        assert not r.recording
        assert r.note_count == 0

    def test_start_stop(self):
        r = Recorder()
        r.start()
        assert r.recording
        r.stop()
        assert not r.recording

    def test_add_note_while_recording(self):
        r = Recorder()
        r.start()
        r.add_note("C", 5, 523)
        r.add_note("D", 5, 587)
        assert r.note_count == 2

    def test_add_note_while_not_recording_ignored(self):
        r = Recorder()
        r.add_note("C", 5, 523)
        assert r.note_count == 0

    def test_start_clears_previous(self):
        r = Recorder()
        r.start()
        r.add_note("C", 5, 523)
        r.stop()
        r.start()
        assert r.note_count == 0

    def test_to_rtttl_empty(self):
        r = Recorder()
        assert r.to_rtttl() == ""

    def test_to_rtttl_produces_valid_format(self):
        r = Recorder()
        r.start()
        r.add_note("C", 5, 523)
        r.add_note("E", 5, 659)
        r.stop()
        rtttl = r.to_rtttl("Test", bpm=120)
        assert rtttl.startswith("Test:")
        assert ":d=" in rtttl
        assert ",o=" in rtttl
        assert ",b=120" in rtttl
        # Should have note section after second colon
        parts = rtttl.split(":")
        assert len(parts) == 3


# ===========================================================================
# 4. Instrument Activity rendering
# ===========================================================================


class TestInstrumentRendering:
    def test_top_bar_shows_title(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        mock_screen.assert_text_on_screen("BuzzerBoard")

    def test_top_bar_shows_octave(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        mock_screen.assert_text_on_screen("Oct: 5")

    def test_top_bar_shows_bpm(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        mock_screen.assert_text_on_screen("BPM: 120")

    def test_piano_keys_visible(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        for label in ["C", "D", "E", "F", "G", "A", "B"]:
            mock_screen.assert_text_on_screen(label)

    def test_key_hints_visible(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        for hint in ["a", "s", "d", "f", "g", "h", "j", "k"]:
            mock_screen.assert_text_on_screen(hint)

    def test_bottom_bar_shows_controls(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        mock_screen.assert_text_on_screen("notes")


# ===========================================================================
# 5. Key-to-note dispatch (integration)
# ===========================================================================


class TestNotePlayback:
    def test_pressing_a_sends_tone_start(self, instrument_app, mock_screen, mock_serial):
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(ord("a"))
        assert len(mock_serial.commands) >= 1
        cmd = mock_serial.commands[-1]
        assert cmd[0] == "TONE_START"
        assert cmd[1] == note_freq("C", 5)

    def test_pressing_w_sends_sharp(self, instrument_app, mock_screen, mock_serial):
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(ord("w"))
        cmd = mock_serial.commands[-1]
        assert cmd[0] == "TONE_START"
        assert cmd[1] == note_freq("C#", 5)

    def test_note_name_displayed_after_press(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(ord("a"))
        mock_screen.assert_text_on_screen("C5")

    def test_active_key_highlighted(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(ord("a"))
        # The active key cell should have REVERSE attribute
        mock_screen.assert_text_has_attr(" C ", Attrs.REVERSE)


# ===========================================================================
# 6. Octave shifting
# ===========================================================================


class TestOctaveShift:
    def test_octave_up(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(Keys.RIGHT_BRACKET)
        mock_screen.assert_text_on_screen("Oct: 6")

    def test_octave_down(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(Keys.LEFT_BRACKET)
        mock_screen.assert_text_on_screen("Oct: 4")

    def test_octave_clamps_at_7(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        for _ in range(5):
            instrument_app.send_key(Keys.RIGHT_BRACKET)
        mock_screen.assert_text_on_screen("Oct: 7")

    def test_octave_clamps_at_3(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        for _ in range(5):
            instrument_app.send_key(Keys.LEFT_BRACKET)
        mock_screen.assert_text_on_screen("Oct: 3")

    def test_octave_affects_tone_frequency(self, instrument_app, mock_screen, mock_serial):
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(Keys.RIGHT_BRACKET)  # octave 6
        instrument_app.send_key(ord("a"))  # C6
        tone_cmds = [c for c in mock_serial.commands if c[0] == "TONE_START"]
        assert tone_cmds[-1][1] == note_freq("C", 6)


# ===========================================================================
# 7. Recording
# ===========================================================================


class TestRecording:
    def test_toggle_recording_on(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(Keys.FORWARD_SLASH)
        mock_screen.assert_text_on_screen("[REC]")

    def test_toggle_recording_off(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(Keys.FORWARD_SLASH)  # on
        instrument_app.send_key(Keys.FORWARD_SLASH)  # off
        mock_screen.assert_text_not_on_screen("[REC]")

    def test_recorded_notes_counted(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(Keys.FORWARD_SLASH)  # start recording
        instrument_app.send_key(ord("a"))
        instrument_app.send_key(ord("s"))
        instrument_app.send_key(ord("d"))
        mock_screen.assert_text_on_screen("3 notes")

    def test_playback_sends_rtttl(self, instrument_app, mock_screen, mock_serial):
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(Keys.FORWARD_SLASH)  # record
        instrument_app.send_key(ord("a"))
        instrument_app.send_key(ord("s"))
        instrument_app.send_key(Keys.FORWARD_SLASH)  # stop
        instrument_app.send_key(ord("."))  # playback
        rtttl_cmds = [c for c in mock_serial.commands if c[0] == "RTTTL"]
        assert len(rtttl_cmds) == 1

    def test_playback_empty_shows_message(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(ord("."))
        mock_screen.assert_text_on_screen("Nothing recorded")


# ===========================================================================
# 8. Port Picker rendering
# ===========================================================================


class TestPortPicker:
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_renders_title(self, mock_scan, mock_ble_scan, app, mock_screen):
        mock_scan.return_value = [("/dev/ttyUSB0", "USB Serial")]
        app.start_activity(PortPickerActivity())
        mock_screen.assert_text_on_screen("BuzzerBoard")
        mock_screen.assert_text_on_screen("Select Connection")

    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_shows_ports(self, mock_scan, mock_ble_scan, app, mock_screen):
        mock_scan.return_value = [
            ("/dev/ttyUSB0", "USB Serial"),
            ("/dev/ttyACM0", "nRF52840"),
        ]
        app.start_activity(PortPickerActivity())
        mock_screen.assert_text_on_screen("/dev/ttyUSB0")
        mock_screen.assert_text_on_screen("/dev/ttyACM0")

    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_no_ports_message(self, mock_scan, mock_ble_scan, app, mock_screen):
        mock_scan.return_value = []
        app.start_activity(PortPickerActivity())
        mock_screen.assert_text_on_screen("No serial ports found")

    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_esc_quits(self, mock_scan, mock_ble_scan, app, mock_screen):
        mock_scan.return_value = []
        app.start_activity(PortPickerActivity())
        app.send_key(Keys.ESC)
        stops = app.flush_stop_events()
        assert len(stops) >= 1


# ===========================================================================
# 9. ESC from instrument
# ===========================================================================


class TestInstrumentNavigation:
    def test_esc_pops_activity(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        assert instrument_app.activity_stack_depth() == 1
        instrument_app.send_key(Keys.ESC)
        stops = instrument_app.flush_stop_events()
        assert len(stops) >= 1


# ---------------------------------------------------------------------------
# Mock BLE service
# ---------------------------------------------------------------------------


class MockBuzzerBLE(Service):
    """Fake BLE service that captures commands (same interface as serial)."""

    def __init__(self):
        super().__init__()
        self.commands = []
        self.address = "AA:BB:CC:DD:EE:FF"

    def on_start(self):
        self._running_event.set()

    def on_stop(self):
        pass

    def play_tone(self, freq, duration_ms=150):
        self.commands.append(("TONE", freq, duration_ms))

    def tone_start(self, freq):
        self.commands.append(("TONE_START", freq))

    def play_rtttl(self, rtttl_str):
        self.commands.append(("RTTTL", rtttl_str))
        return "+OK PLAYING"

    def stop_playback(self):
        self.commands.append(("STOP",))

    def send_command(self, cmd):
        self.commands.append(("CMD", cmd))
        if cmd == "PING":
            return "+PONG"
        return "+OK"


@pytest.fixture
def mock_ble():
    return MockBuzzerBLE()


@pytest.fixture
def instrument_app_ble(app, mock_ble):
    """HarnessApplication with mock BLE service, ready for InstrumentActivity."""
    app.register_service("buzzer_serial", mock_ble)
    app.start_service_sync("buzzer_serial")
    return app


# ===========================================================================
# 10. BLE service interface (polymorphism with serial)
# ===========================================================================


class TestBLEServiceInterface:
    """BLE mock has the same interface as serial -- instrument works unchanged."""

    def test_ble_play_tone_start(self, instrument_app_ble, mock_screen, mock_ble):
        instrument_app_ble.start_activity(InstrumentActivity())
        instrument_app_ble.send_key(ord("a"))
        assert len(mock_ble.commands) >= 1
        cmd = mock_ble.commands[-1]
        assert cmd[0] == "TONE_START"
        assert cmd[1] == note_freq("C", 5)

    def test_ble_play_rtttl(self, mock_ble):
        result = mock_ble.play_rtttl("Test:d=4,o=5,b=120:c,e,g")
        assert result == "+OK PLAYING"
        assert ("RTTTL", "Test:d=4,o=5,b=120:c,e,g") in mock_ble.commands

    def test_ble_stop_playback(self, mock_ble):
        mock_ble.stop_playback()
        assert ("STOP",) in mock_ble.commands

    def test_ble_send_command_ping(self, mock_ble):
        result = mock_ble.send_command("PING")
        assert result == "+PONG"

    def test_ble_recording_playback(self, instrument_app_ble, mock_screen, mock_ble):
        """Record and playback works the same over BLE."""
        instrument_app_ble.start_activity(InstrumentActivity())
        instrument_app_ble.send_key(Keys.FORWARD_SLASH)  # record
        instrument_app_ble.send_key(ord("a"))
        instrument_app_ble.send_key(ord("s"))
        instrument_app_ble.send_key(Keys.FORWARD_SLASH)  # stop
        instrument_app_ble.send_key(ord("."))  # playback
        rtttl_cmds = [c for c in mock_ble.commands if c[0] == "RTTTL"]
        assert len(rtttl_cmds) == 1


# ===========================================================================
# 11. Port Picker with dual lists
# ===========================================================================


class TestPortPickerDualList:
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_renders_connection_title(self, mock_scan, mock_ble_scan, app, mock_screen):
        mock_scan.return_value = [("/dev/ttyUSB0", "USB Serial")]
        app.start_activity(PortPickerActivity())
        mock_screen.assert_text_on_screen("BuzzerBoard")
        mock_screen.assert_text_on_screen("Select Connection")

    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_shows_serial_ports_label(self, mock_scan, mock_ble_scan, app, mock_screen):
        mock_scan.return_value = [("/dev/ttyUSB0", "USB Serial")]
        app.start_activity(PortPickerActivity())
        mock_screen.assert_text_on_screen("Serial Ports:")

    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_shows_ble_devices_label(self, mock_scan, mock_ble_scan, app, mock_screen):
        mock_scan.return_value = []
        app.start_activity(PortPickerActivity())
        mock_screen.assert_text_on_screen("BLE Devices:")

    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_tab_cycles_focus(self, mock_scan, mock_ble_scan, app, mock_screen):
        mock_scan.return_value = [("/dev/ttyUSB0", "USB Serial")]
        app.start_activity(PortPickerActivity())
        activity = app.current_activity()

        # Initially focused on ble_devices (BLE is now default)
        assert activity.focus == "ble_devices"

        # TAB moves to serial_ports
        app.send_key(Keys.TAB)
        assert activity.focus == "serial_ports"

        # TAB wraps back to ble_devices
        app.send_key(Keys.TAB)
        assert activity.focus == "ble_devices"

    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_shows_no_ble_devices_message(self, mock_scan, mock_ble_scan, app, mock_screen):
        mock_scan.return_value = []
        app.start_activity(PortPickerActivity())
        mock_screen.assert_text_on_screen("No BLE devices found")

    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_bottom_bar_shows_tab_hint(self, mock_scan, mock_ble_scan, app, mock_screen):
        mock_scan.return_value = []
        app.start_activity(PortPickerActivity())
        mock_screen.assert_text_on_screen("TAB: switch")

    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_esc_quits(self, mock_scan, mock_ble_scan, app, mock_screen):
        mock_scan.return_value = []
        app.start_activity(PortPickerActivity())
        app.send_key(Keys.ESC)
        stops = app.flush_stop_events()
        assert len(stops) >= 1


# ===========================================================================
# 12. Sustained notes (hold-key-to-sustain)
# ===========================================================================


class TestSustainedNotes:
    def test_key_press_sends_tone_start(self, instrument_app, mock_screen, mock_serial):
        """First press of a note key sends TONE_START with correct frequency."""
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(ord("a"))
        tone_cmds = [c for c in mock_serial.commands if c[0] == "TONE_START"]
        assert len(tone_cmds) == 1
        assert tone_cmds[0][1] == note_freq("C", 5)

    def test_key_repeat_does_not_resend(self, instrument_app, mock_screen, mock_serial):
        """Repeating the same key should NOT send another TONE_START."""
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(ord("a"))
        instrument_app.send_key(ord("a"))  # repeat
        tone_cmds = [c for c in mock_serial.commands if c[0] == "TONE_START"]
        assert len(tone_cmds) == 1

    def test_different_key_sends_new_tone_start(self, instrument_app, mock_screen, mock_serial):
        """Pressing a different key sends a new TONE_START."""
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(ord("a"))  # C5
        instrument_app.send_key(ord("s"))  # D5
        tone_cmds = [c for c in mock_serial.commands if c[0] == "TONE_START"]
        assert len(tone_cmds) == 2
        assert tone_cmds[0][1] == note_freq("C", 5)
        assert tone_cmds[1][1] == note_freq("D", 5)

    def test_quit_sends_stop_when_held(self, instrument_app, mock_screen, mock_serial):
        """ESC while holding a note should send STOP before quitting."""
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(ord("a"))  # hold a note
        instrument_app.send_key(Keys.ESC)  # quit
        stop_cmds = [c for c in mock_serial.commands if c[0] == "STOP"]
        assert len(stop_cmds) >= 1


# ===========================================================================
# 13. BLE scanning
# ===========================================================================


def _make_ble_device(address, name=None):
    """Create a mock BleakClient device + AdvertisementData pair."""
    device = MagicMock()
    device.address = address
    device.name = name or address
    return device


def _make_adv_data(service_uuids=None, local_name=None):
    """Create a mock AdvertisementData."""
    adv = MagicMock()
    adv.service_uuids = service_uuids or []
    adv.local_name = local_name
    return adv


NUS_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"


class TestBLEScanning:
    def test_scan_filters_nus_devices(self):
        """Only devices advertising NUS UUID should be returned."""
        import asyncio
        from buzzerboard_tui.ble_service import BuzzerBLEService

        dev_nus = _make_ble_device("AA:BB:CC:DD:EE:01", "BuzzerBoard")
        adv_nus = _make_adv_data(service_uuids=[NUS_UUID], local_name="BuzzerBoard")
        dev_other = _make_ble_device("AA:BB:CC:DD:EE:02", "SomeOther")
        adv_other = _make_adv_data(service_uuids=["0000180a-0000-1000-8000-00805f9b34fb"])

        scan_result = {
            "AA:BB:CC:DD:EE:01": (dev_nus, adv_nus),
            "AA:BB:CC:DD:EE:02": (dev_other, adv_other),
        }

        with patch("bleak.BleakScanner.discover", return_value=scan_result):
            results = asyncio.run(BuzzerBLEService.scan(timeout=1.0))
        assert len(results) == 1
        assert results[0][0] == "AA:BB:CC:DD:EE:01"
        assert results[0][1] == "BuzzerBoard"

    def test_scan_empty_returns_empty(self):
        """No devices found → empty list."""
        import asyncio
        from buzzerboard_tui.ble_service import BuzzerBLEService

        with patch("bleak.BleakScanner.discover", return_value={}):
            results = asyncio.run(BuzzerBLEService.scan(timeout=1.0))
        assert results == []

    def test_scan_uses_advertised_name(self):
        """local_name from advertisement data is preferred over cached device.name."""
        import asyncio
        from buzzerboard_tui.ble_service import BuzzerBLEService

        dev = _make_ble_device("AA:BB:CC:DD:EE:03", "CachedName")
        adv = _make_adv_data(service_uuids=[NUS_UUID], local_name="BuzzerBoard-Live")

        with patch("bleak.BleakScanner.discover",
                   return_value={"AA:BB:CC:DD:EE:03": (dev, adv)}):
            results = asyncio.run(BuzzerBLEService.scan(timeout=1.0))
        assert results[0][1] == "BuzzerBoard-Live"

    def test_scan_case_sensitivity(self):
        """UUID matching should be case-insensitive (bleak may return uppercase)."""
        import asyncio
        from buzzerboard_tui.ble_service import BuzzerBLEService

        dev = _make_ble_device("AA:BB:CC:DD:EE:04", "BB")
        adv = _make_adv_data(service_uuids=[NUS_UUID.upper()], local_name="BB")

        with patch("bleak.BleakScanner.discover",
                   return_value={"AA:BB:CC:DD:EE:04": (dev, adv)}):
            results = asyncio.run(BuzzerBLEService.scan(timeout=1.0))
        assert len(results) == 1
        assert results[0][0] == "AA:BB:CC:DD:EE:04"


# ===========================================================================
# 14. BLE connection via port picker
# ===========================================================================


class TestBLEConnection:
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_ble_device_appears_in_picker(self, mock_scan, mock_ble_scan, app, mock_screen):
        """A discovered BLE device name should render on screen."""
        mock_scan.return_value = []
        app.start_activity(PortPickerActivity())
        activity = app.current_activity()
        # Simulate scan completing with a device
        activity._on_ble_scan_done([("AA:BB:CC:DD:EE:FF", "BuzzerBoard-Test")])
        mock_screen.assert_text_on_screen("BuzzerBoard-Test")

    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_enter_on_ble_device_connects(self, mock_scan, mock_ble_scan, app, mock_screen):
        """Pressing ENTER on a BLE device should call _connect_ble."""
        mock_scan.return_value = []
        app.start_activity(PortPickerActivity())
        activity = app.current_activity()
        activity._on_ble_scan_done([("AA:BB:CC:DD:EE:FF", "BuzzerBoard-Test")])

        with patch.object(activity, "_connect_ble") as mock_connect:
            app.send_key(Keys.ENTER)
            mock_connect.assert_called_once_with("AA:BB:CC:DD:EE:FF")

    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_connect_failure_shows_error(self, mock_scan, mock_ble_scan, app, mock_screen):
        """Connection failure should display an error message."""
        mock_scan.return_value = []
        app.start_activity(PortPickerActivity())
        activity = app.current_activity()
        # Simulate error via _show_error (same path _connect_ble uses on failure)
        activity._show_error("fail")
        assert activity.display_state["bottom"]["items"]["status"] == "Error: fail"

    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_service_re_registration(self, mock_scan, mock_ble_scan, app, mock_screen):
        """Re-connecting should not crash due to 'already registered' service."""
        mock_scan.return_value = []
        app.start_activity(PortPickerActivity())

        # Register a service under buzzer_serial (simulates first connection)
        mock_svc = MockBuzzerBLE()
        app.register_service("buzzer_serial", mock_svc)

        activity = app.current_activity()
        activity._on_ble_scan_done([("AA:BB:CC:DD:EE:FF", "BuzzerBoard-Test")])

        # _connect_ble imports BuzzerBLEService locally — patch at the source
        with patch("buzzerboard_tui.ble_service.BuzzerBLEService") as MockCls, \
             patch("buzzerboard_tui.port_picker.CentralDispatch"):
            MockCls.return_value = MagicMock()
            # Should not raise even though buzzer_serial already exists
            activity._connect_ble("AA:BB:CC:DD:EE:FF")

    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_continuous_scan_restarts(self, mock_scan, mock_ble_scan, app, mock_screen):
        """After scan completes, the next scan starts automatically."""
        mock_scan.return_value = []
        app.start_activity(PortPickerActivity())
        activity = app.current_activity()
        mock_ble_scan.reset_mock()

        activity._on_ble_scan_done([("AA:BB:CC:DD:EE:FF", "BuzzerBoard")])
        # Should immediately start another scan
        mock_ble_scan.assert_called_once()

    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_scan_stops_on_connect(self, mock_scan, mock_ble_scan, app, mock_screen):
        """Connecting to a device should stop the continuous scan."""
        mock_scan.return_value = []
        app.start_activity(PortPickerActivity())
        activity = app.current_activity()
        activity._on_ble_scan_done([("AA:BB:CC:DD:EE:FF", "BuzzerBoard")])
        mock_ble_scan.reset_mock()

        with patch("buzzerboard_tui.ble_service.BuzzerBLEService") as MockCls, \
             patch("buzzerboard_tui.port_picker.CentralDispatch"):
            MockCls.return_value = MagicMock()
            activity._connect_ble("AA:BB:CC:DD:EE:FF")
        assert activity._ble_scan_active is False

    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_scan_preserves_selection(self, mock_scan, mock_ble_scan, app, mock_screen):
        """Rescan should preserve the user's selected device."""
        mock_scan.return_value = []
        app.start_activity(PortPickerActivity())
        activity = app.current_activity()

        # First scan finds two devices
        activity._on_ble_scan_done([
            ("AA:BB:CC:DD:EE:01", "Device-A"),
            ("AA:BB:CC:DD:EE:02", "Device-B"),
        ])
        # User selects second device
        activity.display_state["ble_devices"]["selected_index"] = 1

        # Next scan returns same devices (maybe different order)
        activity._on_ble_scan_done([
            ("AA:BB:CC:DD:EE:02", "Device-B"),
            ("AA:BB:CC:DD:EE:01", "Device-A"),
        ])
        # Selection should still point to Device-B
        idx = activity.display_state["ble_devices"]["selected_index"]
        assert activity.ble_devices[idx][0] == "AA:BB:CC:DD:EE:02"

    @patch("buzzerboard_tui.port_picker.PortPickerActivity._start_ble_scan")
    @patch("buzzerboard_tui.port_picker.PortPickerActivity._scan_ports")
    def test_scan_merges_devices(self, mock_scan, mock_ble_scan, app, mock_screen):
        """New scan results should merge with existing devices, not replace."""
        mock_scan.return_value = []
        app.start_activity(PortPickerActivity())
        activity = app.current_activity()

        activity._on_ble_scan_done([("AA:BB:CC:DD:EE:01", "Device-A")])
        activity._on_ble_scan_done([("AA:BB:CC:DD:EE:02", "Device-B")])
        assert len(activity.ble_devices) == 2
