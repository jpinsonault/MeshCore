# BuzzerBoard

Turn your Seeed SenseCAP T1000-E into a musical instrument. BuzzerBoard is a
minimal firmware for the T1000-E's piezo buzzer paired with a keyboard-driven
TUI that maps your home row to musical notes.

## Hardware Requirements

- Seeed SenseCAP T1000-E (nRF52840 + piezo buzzer)
- USB-C cable

## Flash the Firmware

```bash
cd /path/to/MeshCore
pio run -e t1000e_buzzerboard -t upload
```

## Install the TUI

```bash
pip install pyserial
```

The TUI uses the [pyos](https://github.com/nickoala/pyos) framework. Make sure
it is importable (e.g. via `pip install -e .` in the pyos repo).

## Run

```bash
python -m buzzerboard_tui
```

Select your serial port, then start playing.

## Keyboard Reference

### Notes (home row = white keys)

| Key | Note |
|-----|------|
| a   | C    |
| s   | D    |
| d   | E    |
| f   | F    |
| g   | G    |
| h   | A    |
| j   | B    |
| k   | C+1  |

### Sharps/flats (top row = black keys)

| Key | Note |
|-----|------|
| w   | C#   |
| e   | D#   |
| t   | F#   |
| y   | G#   |
| u   | A#   |

### Controls

| Key   | Action              |
|-------|---------------------|
| `[`   | Octave down         |
| `]`   | Octave up           |
| `/`   | Toggle recording    |
| `.`   | Playback recording  |
| `,`   | Save to RTTTL file  |
| ESC   | Quit                |

## Recording and Playback

1. Press `/` to start recording.
2. Play notes on the keyboard.
3. Press `/` again to stop recording.
4. Press `.` to play back through the buzzer.
5. Press `,` to save as an RTTTL file (`~/.buzzerboard/songs/`).

## RTTTL Export

Recordings are exported in [RTTTL](https://en.wikipedia.org/wiki/Ring_Tone_Text_Transfer_Language)
format — a compact text encoding for melodies originally used for Nokia ringtones.
The exported string can be sent directly to the firmware with the `RTTTL` serial
command, or loaded into any RTTTL-compatible player.

## Serial Protocol

The firmware accepts these text commands over USB serial (115200 baud, newline-delimited):

| Command                    | Response       |
|----------------------------|----------------|
| `PING`                     | `+PONG`        |
| `TONE <freq_hz> <dur_ms>`  | `+OK`          |
| `STOP`                     | `+OK`          |
| `RTTTL <rtttl_string>`     | `+OK PLAYING`  |
| `STATUS`                   | `+STATUS ...`  |
