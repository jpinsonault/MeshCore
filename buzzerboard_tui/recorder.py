"""Note recorder with RTTTL export."""

import time
from pathlib import Path


class NoteEvent:
    __slots__ = ("name", "octave", "freq", "timestamp")

    def __init__(self, name: str, octave: int, freq: int, timestamp: float):
        self.name = name
        self.octave = octave
        self.freq = freq
        self.timestamp = timestamp


class Recorder:
    def __init__(self):
        self.events: list[NoteEvent] = []
        self.recording = False
        self._start_time = 0.0

    def start(self):
        self.events.clear()
        self.recording = True
        self._start_time = time.monotonic()

    def stop(self):
        self.recording = False

    def add_note(self, note_name: str, octave: int, freq: int):
        if not self.recording:
            return
        ts = time.monotonic() - self._start_time
        self.events.append(NoteEvent(note_name, octave, freq, ts))

    @property
    def note_count(self) -> int:
        return len(self.events)

    def to_rtttl(self, title: str = "Song", bpm: int = 120) -> str:
        """Export recorded notes to an RTTTL string.

        RTTTL format: name:d=default_duration,o=default_octave,b=bpm:notes

        Uses inter-note timing to determine duration, quantized to the nearest
        standard RTTTL duration value.
        """
        if not self.events:
            return ""

        from .notes import RTTTL_NOTE_MAP

        # RTTTL duration values: 1=whole, 2=half, 4=quarter, 8=eighth, 16=sixteenth, 32=thirty-second
        DURATIONS = [1, 2, 4, 8, 16, 32]

        # Quarter note duration in ms
        quarter_ms = 60000.0 / bpm
        whole_ms = quarter_ms * 4

        # Determine default octave (most common)
        octave_counts: dict[int, int] = {}
        for e in self.events:
            octave_counts[e.octave] = octave_counts.get(e.octave, 0) + 1
        default_oct = max(octave_counts, key=octave_counts.get) if octave_counts else 5

        # Build note strings using inter-note gaps as durations
        rtttl_notes = []
        for i, event in enumerate(self.events):
            if i + 1 < len(self.events):
                gap_ms = (self.events[i + 1].timestamp - event.timestamp) * 1000
            else:
                gap_ms = 200  # default for last note

            # Find closest RTTTL duration
            best_dur = 8
            best_diff = float("inf")
            for d in DURATIONS:
                dur_ms = whole_ms / d
                diff = abs(dur_ms - gap_ms)
                if diff < best_diff:
                    best_diff = diff
                    best_dur = d

            # Build note string
            note_letter = RTTTL_NOTE_MAP.get(event.name, "c")
            parts = []
            if best_dur != 4:  # 4 is conventional default
                parts.append(str(best_dur))
            parts.append(note_letter)
            if event.octave != default_oct:
                parts.append(str(event.octave))

            rtttl_notes.append("".join(parts))

        header = f"{title}:d=4,o={default_oct},b={bpm}"
        return f"{header}:{','.join(rtttl_notes)}"

    def save(self, title: str = "Song", bpm: int = 120) -> str:
        """Export RTTTL and save to ~/.buzzerboard/songs/<title>.rtttl.
        Returns the file path.
        """
        rtttl = self.to_rtttl(title, bpm)
        save_dir = Path.home() / ".buzzerboard" / "songs"
        save_dir.mkdir(parents=True, exist_ok=True)

        safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in title)
        path = save_dir / f"{safe_name}.rtttl"
        path.write_text(rtttl + "\n")
        return str(path)
