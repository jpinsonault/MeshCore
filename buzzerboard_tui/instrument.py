"""Instrument activity -- main BuzzerBoard screen."""

import time
import threading
from functools import partial

from pyos.Activity import Activity
from pyos import Keys, Attrs
from pyos.KeyMap import KeyMap
from pyos.EventTypes import KeyStroke, KeyRelease
from pyos.printers.TopBar import TopBar
from pyos.printers.BottomBar import BottomBar
from pyos.printers.printers import print_line, print_empty_line

from .notes import (
    key_to_note_and_freq,
    WHITE_KEY_LABELS,
    WHITE_KEY_CODES,
    BLACK_KEY_LABELS,
    BLACK_KEY_CODES,
)
from .recorder import Recorder

# Staccato timing
#
# Each repeat cycle:  |── tone on ──|── silence ──|
#                     |   TONE_MS   |   GAP_MS    |
#                     |<──────── CYCLE_S ────────>|
#
# CYCLE_S  = 0.15s (150ms) — matches the framework's repeat-throttle interval
# TONE_MS  = 100          — each beep lasts 100ms (play_tone is self-terminating)
# GAP_MS   = 50           — silence before next beep ("room")
#
# On release, STOP arrives during the gap (or later), so the last beep
# always finishes naturally — no audible cut.
STACCATO_CYCLE_S = 0.15
STACCATO_TONE_MS = 100


class InstrumentActivity(Activity):

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self.application.subscribe(KeyRelease, self, self.on_key_release)

        self.octave = 5
        self.bpm = 120
        self.recorder = Recorder()
        self.last_note = ""
        self.active_key = None
        self.status_message = ""
        self._held_key = None
        self._last_tone_time = 0.0
        # Kitty terminals deliver real key-release → sustained notes.
        # Non-Kitty terminals use synthetic release → staccato pattern.
        self._sustained = getattr(self.application, '_kitty_active', False)

        self.serial = self.application.service("buzzer_serial")

        self.keymap = KeyMap(
            {
                Keys.ESC: self._quit,
                Keys.LEFT_BRACKET: self._octave_down,
                Keys.RIGHT_BRACKET: self._octave_up,
                Keys.FORWARD_SLASH: self._toggle_recording,
                ord("."): self._playback,
                ord(","): self._save,
                ord("l"): self._dump_log,
            }
        )

        self.display_state = {
            "top": TopBar.display_state(items=self._top_items()),
            "piano": {
                "layout": {"flex": 1, "min_height": 8},
                "line_generator": self._render_piano,
            },
            "bottom": BottomBar.display_state(items=self._bottom_items()),
        }

    def _top_items(self) -> dict:
        rec_indicator = " [REC]" if self.recorder.recording else ""
        return {
            "title": "BuzzerBoard",
            "octave": f"Oct: {self.octave}",
            "bpm": f"BPM: {self.bpm}",
            "rec": rec_indicator,
        }

    def _bottom_items(self) -> dict:
        items = {
            "keys": "ASDFGHJK: notes",
            "oct": "[]: octave",
            "rec": "/: record",
            "play": ".: play",
            "save": ",: save",
            "log": "l: log",
        }
        if self.status_message:
            items["status"] = self.status_message
        if self.last_note:
            items["note"] = self.last_note
        return items

    def _render_piano(self, context, remaining_height):
        """Custom line_generator that draws an ASCII piano keyboard."""
        lines = []

        # Black keys
        black_line = self._make_black_key_line()
        black_hint = self._make_black_hint_line()
        lines.append(partial(self._print_key_line, black_line))
        lines.append(partial(self._print_key_line, black_hint))

        # Separator
        lines.append(print_empty_line)

        # White keys
        white_line = self._make_white_key_line()
        white_hint = self._make_white_hint_line()
        lines.append(partial(self._print_key_line, white_line))
        lines.append(partial(self._print_key_line, white_hint))

        # Status area
        lines.append(print_empty_line)

        now_playing = f"  Playing: {self.last_note}" if self.last_note else ""
        rec_status = ""
        if self.recorder.recording:
            rec_status = f"  RECORDING ({self.recorder.note_count} notes)"

        status = now_playing + rec_status
        if status:
            lines.append(partial(print_line, 2, status))

        if self.status_message:
            lines.append(partial(print_line, 2, f"  {self.status_message}"))

        while len(lines) < remaining_height:
            lines.append(print_empty_line)

        return lines[:remaining_height]

    def _make_white_key_line(self) -> list:
        result = []
        for i, label in enumerate(WHITE_KEY_LABELS):
            key_code = WHITE_KEY_CODES[i]
            is_active = self.active_key == key_code
            result.append((f" {label:^3} ", is_active))
        return result

    def _make_white_hint_line(self) -> list:
        keys = "asdfghjk"
        result = []
        for i, ch in enumerate(keys):
            key_code = WHITE_KEY_CODES[i]
            is_active = self.active_key == key_code
            result.append((f" {ch:^3} ", is_active))
        return result

    def _make_black_key_line(self) -> list:
        result = []
        for i, label in enumerate(BLACK_KEY_LABELS):
            if label:
                key_code = BLACK_KEY_CODES[i]
                is_active = self.active_key == key_code
                result.append((f" {label:^3} ", is_active))
            else:
                result.append(("     ", False))
        return result

    def _make_black_hint_line(self) -> list:
        keys = [ord("w"), ord("e"), None, ord("t"), ord("y"), ord("u"), None]
        result = []
        for key_code in keys:
            if key_code:
                ch = chr(key_code)
                is_active = self.active_key == key_code
                result.append((f"  {ch}  ", is_active))
            else:
                result.append(("     ", False))
        return result

    @staticmethod
    def _print_key_line(segments, screen, y):
        """Print a row of piano key segments with highlighting for active key."""
        x = 2  # left margin
        _, num_cols = screen.getmaxyx()
        for text, is_active in segments:
            if is_active:
                attr = Attrs.REVERSE | Attrs.BOLD
            else:
                attr = Attrs.NORMAL
            if x + len(text) < num_cols:
                screen.addstr(y, x, text, attr)
            x += len(text)

    def on_key_stroke(self, event: KeyStroke):
        key = event.key

        if self.keymap.dispatch(key):
            self._update_display()
            return

        result = key_to_note_and_freq(key, self.octave)
        if result:
            display_name, freq = result
            if self._held_key == key:
                if self._sustained:
                    return  # Kitty — tone_start is already playing
                now = time.monotonic()
                if now - self._last_tone_time >= STACCATO_CYCLE_S:
                    self.serial.play_tone(freq, STACCATO_TONE_MS)
                    self._last_tone_time = now
                return
            self._play_note(key, display_name, freq)

    def on_key_release(self, event: KeyRelease):
        key = event.key
        if self._held_key == key:
            self._held_key = None   # clear first so stale repeats don't match
            self.active_key = None
            self.serial.stop_playback()
            self._update_display()

    def _play_note(self, key_code: int, display_name: str, freq: int):
        """Start a note — sustained (Kitty) or staccato beep (fallback)."""
        self._held_key = key_code
        self.active_key = key_code
        self.last_note = display_name
        self.status_message = ""

        if self._sustained:
            self.serial.tone_start(freq)
        else:
            self.serial.play_tone(freq, STACCATO_TONE_MS)
            self._last_tone_time = time.monotonic()

        if self.recorder.recording:
            if "#" in display_name:
                note_name = display_name[:2]
                octave = int(display_name[2:])
            else:
                note_name = display_name[0]
                octave = int(display_name[1:])
            self.recorder.add_note(note_name, octave, freq)

        self._update_display()

    def _octave_down(self):
        if self.octave > 3:
            self.octave -= 1

    def _octave_up(self):
        if self.octave < 7:
            self.octave += 1

    def _toggle_recording(self):
        if self.recorder.recording:
            self.recorder.stop()
            self.status_message = f"Recording stopped ({self.recorder.note_count} notes)"
        else:
            self.recorder.start()
            self.status_message = "Recording started"

    def _playback(self):
        """Export current recording to RTTTL and send to firmware."""
        if self.recorder.note_count == 0:
            self.status_message = "Nothing recorded"
            return
        rtttl = self.recorder.to_rtttl("Playback", self.bpm)
        if rtttl:
            self.serial.play_rtttl(rtttl)
            self.status_message = f"Playing back {self.recorder.note_count} notes..."

    def _save(self):
        """Save recording to file."""
        if self.recorder.note_count == 0:
            self.status_message = "Nothing to save"
            return
        path = self.recorder.save("BuzzerBoard", self.bpm)
        self.status_message = f"Saved to {path}"

    def _dump_log(self):
        """Fetch firmware command log in background, write to buzzerboard.log."""
        self.status_message = "Fetching log..."
        self._update_display()

        def _fetch():
            try:
                lines = self.serial.fetch_log()
                if not lines:
                    msg = "Log empty"
                else:
                    path = "buzzerboard.log"
                    with open(path, "a") as f:
                        f.write(f"--- log dump ({len(lines)} entries) ---\n")
                        for line in lines:
                            f.write(line + "\n")
                    msg = f"Log: {len(lines)} entries -> {path}"
            except Exception as e:
                msg = f"Log error: {e}"
            self.main_thread.submit_async(self._on_log_done, msg)

        threading.Thread(target=_fetch, daemon=True).start()

    def _on_log_done(self, msg: str):
        self.status_message = msg
        self._update_display()

    def _quit(self):
        if self._held_key is not None:
            self.serial.stop_playback()
            self._held_key = None
        self.application.pop_activity()

    def _update_display(self):
        self.display_state["top"]["items"] = self._top_items()
        self.display_state["bottom"]["items"] = self._bottom_items()
        self.refresh_screen()
