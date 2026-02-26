"""Tests for BuzzerBoard TUI.

Uses MockScreen + HarnessApplication from pyos.testing.
Serial and BLE communication are mocked -- no hardware needed.
"""

import curses
import pytest
from unittest.mock import patch, MagicMock

from pyos.testing import MockScreen, HarnessApplication
from pyos import Keys
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
    def test_pressing_a_sends_tone(self, instrument_app, mock_screen, mock_serial):
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(ord("a"))
        assert len(mock_serial.commands) >= 1
        cmd = mock_serial.commands[-1]
        assert cmd[0] == "TONE"
        assert cmd[1] == note_freq("C", 5)

    def test_pressing_w_sends_sharp(self, instrument_app, mock_screen, mock_serial):
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(ord("w"))
        cmd = mock_serial.commands[-1]
        assert cmd[0] == "TONE"
        assert cmd[1] == note_freq("C#", 5)

    def test_note_name_displayed_after_press(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(ord("a"))
        mock_screen.assert_text_on_screen("C5")

    def test_active_key_highlighted(self, instrument_app, mock_screen):
        instrument_app.start_activity(InstrumentActivity())
        instrument_app.send_key(ord("a"))
        # The active key cell should have REVERSE attribute
        mock_screen.assert_text_has_attr(" C ", curses.A_REVERSE)


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
        cmd = mock_serial.commands[-1]
        assert cmd[1] == note_freq("C", 6)


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

    def test_ble_play_tone(self, instrument_app_ble, mock_screen, mock_ble):
        instrument_app_ble.start_activity(InstrumentActivity())
        instrument_app_ble.send_key(ord("a"))
        assert len(mock_ble.commands) >= 1
        cmd = mock_ble.commands[-1]
        assert cmd[0] == "TONE"
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

        # Initially focused on serial_ports
        assert activity.focus == "serial_ports"

        # TAB moves to ble_devices
        app.send_key(Keys.TAB)
        assert activity.focus == "ble_devices"

        # TAB wraps back to serial_ports
        app.send_key(Keys.TAB)
        assert activity.focus == "serial_ports"

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
