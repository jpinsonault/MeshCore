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


# Home row = white keys starting at C
KEY_TO_NOTE = {
    ord("a"): "C",
    ord("s"): "D",
    ord("d"): "E",
    ord("f"): "F",
    ord("g"): "G",
    ord("h"): "A",
    ord("j"): "B",
    ord("k"): "C+",  # C one octave up
}

# Top row = sharps/flats (black keys)
KEY_TO_NOTE_SHARP = {
    ord("w"): "C#",
    ord("e"): "D#",
    ord("t"): "F#",
    ord("y"): "G#",
    ord("u"): "A#",
}


def key_to_note_and_freq(key_code: int, octave: int) -> tuple | None:
    """Given a key code and current octave, return (display_name, frequency) or None."""
    if key_code in KEY_TO_NOTE:
        name = KEY_TO_NOTE[key_code]
        if name == "C+":
            return (f"C{octave + 1}", note_freq("C", octave + 1))
        return (f"{name}{octave}", note_freq(name, octave))
    if key_code in KEY_TO_NOTE_SHARP:
        name = KEY_TO_NOTE_SHARP[key_code]
        return (f"{name}{octave}", note_freq(name, octave))
    return None


# RTTTL note letter mapping (for export)
RTTTL_NOTE_MAP = {
    "C": "c",
    "C#": "c#",
    "D": "d",
    "D#": "d#",
    "E": "e",
    "F": "f",
    "F#": "f#",
    "G": "g",
    "G#": "g#",
    "A": "a",
    "A#": "a#",
    "B": "b",
}

# All note keys combined
ALL_NOTE_KEYS = {**KEY_TO_NOTE, **KEY_TO_NOTE_SHARP}

# Piano display constants
WHITE_KEY_LABELS = ["C", "D", "E", "F", "G", "A", "B", "C+"]
WHITE_KEY_CODES = [ord("a"), ord("s"), ord("d"), ord("f"), ord("g"), ord("h"), ord("j"), ord("k")]
BLACK_KEY_LABELS = ["C#", "D#", "", "F#", "G#", "A#", ""]
BLACK_KEY_CODES = [ord("w"), ord("e"), None, ord("t"), ord("y"), ord("u"), None]
