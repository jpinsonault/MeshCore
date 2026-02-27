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
    QWERTY_KEYS,
    HOME_KEYS,
    BOTTOM_KEYS,
    KEY_WIDTH,
)
from .recorder import Recorder

# Staccato timing
STACCATO_CYCLE_S = 0.15
STACCATO_TONE_MS = 100


class InstrumentActivity(Activity):

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self.application.subscribe(KeyRelease, self, self.on_key_release)

        self.octave = 4
        self.bpm = 120
        self.recorder = Recorder()
        self.last_note = ""
        self.active_key = None
        self.status_message = ""
        self._held_key = None
        self._last_tone_time = 0.0
        self._sustained = getattr(self.application, '_kitty_active', False)

        self.serial = self.application.service("buzzer_serial")

        self.keymap = KeyMap(
            {
                Keys.ESC: self._quit,
                Keys.F2: self._octave_down,
                Keys.F3: self._octave_up,
                Keys.F4: self._toggle_recording,
                Keys.F5: self._playback,
                Keys.F6: self._save,
            }
        )

        self.display_state = {
            "top": TopBar.display_state(items=self._top_items()),
            "piano": {
                "layout": {"flex": 1, "min_height": 10},
                "line_generator": self._render_piano,
            },
            "bottom": BottomBar.display_state(items=self._bottom_items()),
        }

    def _top_items(self) -> dict:
        rec_indicator = " [REC]" if self.recorder.recording else ""
        return {
            "title": "BuzzerBoard",
            "octave": f"Oct: {self.octave - 1}\u2013{self.octave + 1}",
            "bpm": f"BPM: {self.bpm}",
            "rec": rec_indicator,
        }

    def _bottom_items(self) -> dict:
        items = {
            "oct": "F2/F3: octave",
            "rec": "F4: record",
            "play": "F5: play",
            "save": "F6: save",
        }
        if self.status_message:
            items["status"] = self.status_message
        if self.last_note:
            items["note"] = self.last_note
        return items

    def _render_piano(self, context, remaining_height):
        """Render a three-row keyboard radiating from the home row."""
        lines = []

        # QWERTY row — sharps (above home)
        sharps = self._keycap_data(QWERTY_KEYS)
        lines += self._keycap_row(sharps)

        # Home row — naturals (center)
        naturals = self._keycap_data(HOME_KEYS)
        lines += self._keycap_row(naturals)

        # Bottom row — lower octave (below home)
        lower = self._keycap_data(BOTTOM_KEYS)
        lines += self._keycap_row(lower)

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

    def _keycap_row(self, caps):
        """Return 4 line-printers for one row of keycaps."""
        return [
            partial(self._print_keycap_borders, caps, "\u256d\u2500\u2500\u2500\u2500\u256e"),
            partial(self._print_keycap_content, caps, "note"),
            partial(self._print_keycap_content, caps, "hint"),
            partial(self._print_keycap_borders, caps, "\u2570\u2500\u2500\u2500\u2500\u256f"),
        ]

    def _keycap_data(self, keys):
        """Pre-compute keycap render data: [(col, note, hint, active), ...]."""
        return [
            (col, note, hint, self.active_key == code)
            for col, note, hint, code in keys
        ]

    @staticmethod
    def _print_keycap_borders(caps, border, screen, y):
        _, num_cols = screen.getmaxyx()
        for col, _, _, active in caps:
            if col + KEY_WIDTH < num_cols:
                attr = Attrs.BOLD if active else Attrs.NORMAL
                screen.addstr(y, col, border, attr)

    @staticmethod
    def _print_keycap_content(caps, which, screen, y):
        _, num_cols = screen.getmaxyx()
        for col, note, hint, active in caps:
            if col + KEY_WIDTH >= num_cols:
                continue
            if active:
                attr = Attrs.REVERSE | Attrs.BOLD
            elif which == "hint":
                attr = Attrs.DIM
            else:
                attr = Attrs.NORMAL
            text = note if which == "note" else hint
            screen.addstr(y, col, "\u2502", Attrs.BOLD if active else Attrs.NORMAL)
            screen.addstr(y, col + 1, f"{text:^4}", attr)
            screen.addstr(y, col + 5, "\u2502", Attrs.BOLD if active else Attrs.NORMAL)

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
                    return
                now = time.monotonic()
                if now - self._last_tone_time >= STACCATO_CYCLE_S:
                    self.serial.play_tone(freq, STACCATO_TONE_MS)
                    self._last_tone_time = now
                return
            self._play_note(key, display_name, freq)

    def on_key_release(self, event: KeyRelease):
        key = event.key
        if self._held_key == key:
            self._held_key = None
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
        if self.octave < 6:
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

    def _quit(self):
        if self._held_key is not None:
            self.serial.stop_playback()
            self._held_key = None
        self.application.pop_activity()

    def _update_display(self):
        self.display_state["top"]["items"] = self._top_items()
        self.display_state["bottom"]["items"] = self._bottom_items()
        self.refresh_screen()
