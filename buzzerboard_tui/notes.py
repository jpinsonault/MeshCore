"""Note frequency tables and keyboard-to-note mapping."""

# Standard A4 = 440 Hz, equal temperament
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_freq(name: str, octave: int) -> int:
    """Return the frequency in Hz for a note name and octave.

    E.g., note_freq("A", 4) -> 440, note_freq("C", 5) -> 523
    """
    semitone_index = NOTE_NAMES.index(name)
    # A is index 9 in our list
    semitones_from_a4 = (octave - 4) * 12 + (semitone_index - 9)
    return int(round(440.0 * (2.0 ** (semitones_from_a4 / 12.0))))


# ── Home-row-centric keyboard mapping ────────────────────────────────
#
# Fingers rest on the home row.  Every movement radiates outward:
#
#   UP (QWERTY row)  = sharp of that note   (curl finger up)
#   HOME              = natural note          (at rest)
#   DOWN (bottom row) = same note, octave below (extend finger down)
#
#   Sharps:     [w] [e]    [t] [y] [u]    [o] [p]
#   Naturals: [a] [s] [d] [f] [g] [h] [j] [k] [l] [;]
#   Lower:      [z] [x] [c] [v] [b] [n] [m]
#
#   r skipped (no E#)          i skipped (no B#)
#
# Each entry maps key_code -> (note_name, octave_offset).
# Offset 0 = base octave, -1 = below, +1 = above.

KEY_NOTE_MAP = {
    # Home row — naturals (offset 0)
    ord("a"): ("C", 0),
    ord("s"): ("D", 0),
    ord("d"): ("E", 0),
    ord("f"): ("F", 0),
    ord("g"): ("G", 0),
    ord("h"): ("A", 0),
    ord("j"): ("B", 0),
    # Home row — upper extension (offset +1)
    ord("k"): ("C", 1),
    ord("l"): ("D", 1),
    ord(";"): ("E", 1),
    # QWERTY row — sharps (offset 0)
    ord("w"): ("C#", 0),
    ord("e"): ("D#", 0),
    # r skipped (no E#)
    ord("t"): ("F#", 0),
    ord("y"): ("G#", 0),
    ord("u"): ("A#", 0),
    # i skipped (no B#)
    # QWERTY row — upper extension sharps (offset +1)
    ord("o"): ("C#", 1),
    ord("p"): ("D#", 1),
    # Bottom row — lower octave naturals (offset -1)
    ord("z"): ("C", -1),
    ord("x"): ("D", -1),
    ord("c"): ("E", -1),
    ord("v"): ("F", -1),
    ord("b"): ("G", -1),
    ord("n"): ("A", -1),
    ord("m"): ("B", -1),
}


def key_to_note_and_freq(key_code: int, octave: int) -> tuple | None:
    """Given a key code and base octave, return (display_name, frequency) or None."""
    entry = KEY_NOTE_MAP.get(key_code)
    if entry is None:
        return None
    name, offset = entry
    actual_octave = octave + offset
    return (f"{name}{actual_octave}", note_freq(name, actual_octave))


# RTTTL note letter mapping (for export)
RTTTL_NOTE_MAP = {
    "C": "c", "C#": "c#", "D": "d", "D#": "d#", "E": "e", "F": "f",
    "F#": "f#", "G": "g", "G#": "g#", "A": "a", "A#": "a#", "B": "b",
}

# All note keys combined
ALL_NOTE_KEYS = KEY_NOTE_MAP


# ── Physical keyboard layout (for rendering) ─────────────────────────
#
# Column positions from real ANSI keyboard geometry.
# Home row anchored at col 4, pitch = 7 chars (6-char keycap + 1 gap).
# QWERTY starts 2 chars left (¼ key stagger).
# Bottom starts 4 chars right (½ key stagger from home).
#
# Each entry: (col, note_label, key_char, key_code)

KEY_WIDTH = 6   # ╭────╮

# QWERTY row — sharps (above home row)
QWERTY_KEYS = [
    (9,  "C#", "w", ord("w")),
    (16, "D#", "e", ord("e")),
    # r skipped (no E#)
    (30, "F#", "t", ord("t")),
    (37, "G#", "y", ord("y")),
    (44, "A#", "u", ord("u")),
    # i skipped (no B#)
    (58, "C#", "o", ord("o")),
    (65, "D#", "p", ord("p")),
]

# Home row — naturals (center)
HOME_KEYS = [
    (4,  "C",  "a", ord("a")),
    (11, "D",  "s", ord("s")),
    (18, "E",  "d", ord("d")),
    (25, "F",  "f", ord("f")),
    (32, "G",  "g", ord("g")),
    (39, "A",  "h", ord("h")),
    (46, "B",  "j", ord("j")),
    (53, "C",  "k", ord("k")),
    (60, "D",  "l", ord("l")),
    (67, "E",  ";", ord(";")),
]

# Bottom row — lower octave naturals (below home row)
BOTTOM_KEYS = [
    (8,  "C",  "z", ord("z")),
    (15, "D",  "x", ord("x")),
    (22, "E",  "c", ord("c")),
    (29, "F",  "v", ord("v")),
    (36, "G",  "b", ord("b")),
    (43, "A",  "n", ord("n")),
    (50, "B",  "m", ord("m")),
]
