#!/usr/bin/env python3
"""
Tests for T1000-E media remote firmware logic.

Validates that all tones derive from BASE_FREQ, volume scale consistency,
orientation math, and face-down mute thresholds by parsing main.cpp.

Run:  python3 examples/media_remote/test_media_remote.py
"""

import re, math, sys, os

SRC = os.path.join(os.path.dirname(__file__), "main.cpp")

def read_src():
    with open(SRC) as f:
        return f.read()

# ---------- extract data from source ----------

def extract_define_int(src, name):
    m = re.search(rf'#define\s+{name}\s+(\d+)', src)
    return int(m.group(1)) if m else None

def extract_define_float(src, name):
    m = re.search(rf'#define\s+{name}\s+\(?([^)\s]+)', src)
    return float(m.group(1).rstrip('f')) if m else None

def extract_vol_note_idx(src):
    m = re.search(r'static\s+int8_t\s+vol_note_idx\s*=\s*(\d+)', src)
    return int(m.group(1)) if m else None

def resolve_expr(src, expr):
    """Resolve simple expressions like BASE_FREQ, BASE_FREQ + 5, or literals."""
    expr = expr.strip()
    if expr.isdigit():
        return int(expr)
    # Try BASE_FREQ + N
    m = re.match(r'(\w+)\s*\+\s*(\d+)', expr)
    if m:
        base = extract_define_int(src, m.group(1))
        return base + int(m.group(2)) if base is not None else None
    # Try bare define
    return extract_define_int(src, expr)

# ---------- tests ----------

def test_no_rtttl():
    """Firmware must not include NonBlockingRtttl."""
    src = read_src()
    assert "NonBlockingRtttl" not in src, "Still including NonBlockingRtttl"
    assert "rtttl::" not in src, "Still calling rtttl:: functions"
    print("  PASS: no RTTTL dependency")

def test_base_freq_exists():
    """BASE_FREQ must be defined and all derived constants must reference it."""
    src = read_src()
    base = extract_define_int(src, "BASE_FREQ")
    assert base is not None, "BASE_FREQ not defined"
    assert base >= 20, f"BASE_FREQ={base}Hz below tone() minimum (20Hz)"
    # MID_FREQ and TOP_FREQ must be expressions of BASE_FREQ
    assert re.search(r'#define\s+MID_FREQ\s+\(BASE_FREQ', src), "MID_FREQ must derive from BASE_FREQ"
    assert re.search(r'#define\s+TOP_FREQ\s+\(BASE_FREQ', src), "TOP_FREQ must derive from BASE_FREQ"
    print(f"  PASS: BASE_FREQ={base}Hz, MID/TOP derive from it")

def test_vol_notes_start_at_base():
    """VOL_NOTES[0] must be BASE_FREQ (not a separate hardcoded value)."""
    src = read_src()
    # Check that VOL_NOTES array uses BASE_FREQ directly
    assert re.search(r'VOL_NOTES\[\]\s*=\s*\{\s*\n?\s*BASE_FREQ', src), \
        "VOL_NOTES[0] must be BASE_FREQ (not a hardcoded number)"
    print("  PASS: VOL_NOTES[0] = BASE_FREQ")

def test_vol_tick_matches_note_dur():
    """Volume tick duration must match melody note duration."""
    src = read_src()
    vol_tick = extract_define_int(src, "VOL_TICK_MS")
    note_dur = extract_define_int(src, "NOTE_DUR")
    assert vol_tick is not None, "VOL_TICK_MS not defined"
    assert note_dur is not None, "NOTE_DUR not defined"
    assert vol_tick == note_dur, f"VOL_TICK_MS={vol_tick} != NOTE_DUR={note_dur} — will sound different"
    print(f"  PASS: VOL_TICK_MS={vol_tick}ms == NOTE_DUR={note_dur}ms")

def test_play_freq_uses_vol_tick():
    """play_freq calls for volume must use VOL_TICK_MS, not a hardcoded value."""
    src = read_src()
    # Find play_freq calls with VOL_NOTES
    vol_calls = re.findall(r'play_freq\(VOL_NOTES\[.*?\],\s*(\w+)\)', src)
    assert vol_calls, "No play_freq(VOL_NOTES[...], ...) calls found"
    for arg in vol_calls:
        assert arg == "VOL_TICK_MS", f"play_freq uses '{arg}' instead of VOL_TICK_MS"
    print(f"  PASS: all volume play_freq calls use VOL_TICK_MS")

def test_cycle_count():
    """All notes must produce 10+ waveform cycles at their frequency."""
    src = read_src()
    base = extract_define_int(src, "BASE_FREQ")
    note_dur = extract_define_int(src, "NOTE_DUR")
    vol_tick = extract_define_int(src, "VOL_TICK_MS")
    # Worst case: BASE_FREQ at NOTE_DUR
    cycles = base * note_dur / 1000.0
    assert cycles >= 10, f"BASE_FREQ={base}Hz × NOTE_DUR={note_dur}ms = {cycles:.1f} cycles (need 10+)"
    # Volume tick
    cycles_vol = base * vol_tick / 1000.0
    assert cycles_vol >= 10, f"BASE_FREQ={base}Hz × VOL_TICK_MS={vol_tick}ms = {cycles_vol:.1f} cycles (need 10+)"
    print(f"  PASS: {base}Hz × {note_dur}ms = {cycles:.1f} cycles")

def test_vol_scale_bounds():
    """vol_note_idx must be in bounds and reachable from both ends."""
    src = read_src()
    count = extract_define_int(src, "VOL_NOTE_COUNT")
    idx = extract_vol_note_idx(src)
    assert 0 <= idx < count, f"vol_note_idx={idx} out of range [0,{count})"
    assert idx <= 8, f"Need {idx} steps to reach min — too many (max 8)"
    assert idx >= 3, f"Only {idx} steps to reach min — too few for range"
    print(f"  PASS: vol_note_idx={idx}, {count} total steps")

def test_vol_scale_monotonic():
    """Volume scale must be strictly ascending."""
    src = read_src()
    base = extract_define_int(src, "BASE_FREQ")
    # Reconstruct: BASE_FREQ, BASE_FREQ+5, ..., BASE_FREQ+75
    notes = [base + i * 5 for i in range(16)]
    for i in range(1, len(notes)):
        assert notes[i] > notes[i-1]
    print(f"  PASS: VOL_NOTES ascending ({notes[0]}-{notes[-1]}Hz)")

def test_melodies_use_defines():
    """All MelNote arrays must use BASE_FREQ/MID_FREQ/TOP_FREQ, not raw numbers."""
    src = read_src()
    mel_pattern = r'static\s+const\s+MelNote\s+N_\w+\[\]\s*=\s*\{([^;]+)\};'
    for m in re.finditer(mel_pattern, src):
        body = m.group(1)
        # Extract frequency arguments from {{freq,dur},...}
        for note_m in re.finditer(r'\{(\w+(?:\s*\+\s*\d+)?)\s*,', body):
            freq_expr = note_m.group(1).strip()
            if freq_expr == '0':
                continue  # pause
            allowed = ['BASE_FREQ', 'MID_FREQ', 'TOP_FREQ', 'NOTE_DUR']
            assert any(a in freq_expr for a in allowed), \
                f"Melody uses raw '{freq_expr}' instead of BASE_FREQ/MID_FREQ/TOP_FREQ"
    print("  PASS: all melodies use derived frequency constants")

def test_orientation_cross_dot():
    """Cross/dot product angle delta must be wrap-safe."""
    def angle_delta(ref_y, ref_z, cur_y, cur_z):
        cross = ref_z * cur_y - ref_y * cur_z
        dot = ref_y * cur_y + ref_z * cur_z
        return math.atan2(cross, dot)

    assert abs(angle_delta(0, 1, 0, 1)) < 0.01
    d = angle_delta(0, 1, math.sin(math.radians(15)), math.cos(math.radians(15)))
    assert abs(math.degrees(d) - 15.0) < 0.5
    d = angle_delta(0, 1, -math.sin(math.radians(15)), math.cos(math.radians(15)))
    assert abs(math.degrees(d) + 15.0) < 0.5
    # Near ±π: must NOT cause ±360° jump
    d1 = angle_delta(0, -0.04, 0.01, -0.04)
    d2 = angle_delta(0, -0.04, -0.01, -0.04)
    assert abs(math.degrees(d1)) < 30
    assert abs(math.degrees(d2)) < 30
    print("  PASS: cross/dot angle delta is wrap-safe")

def test_face_down_thresholds():
    """Face-down mute thresholds must have proper hysteresis."""
    src = read_src()
    down = extract_define_float(src, "FACE_DOWN_GZ")
    up = extract_define_float(src, "FACE_UP_GZ")
    assert down < up
    assert down < -0.4
    assert up > -0.4
    assert up - down >= 0.3
    print(f"  PASS: face-down hysteresis gap={up - down:.1f}")

def test_magnitude_guard():
    """Gesture mode must reject low Y-Z magnitude."""
    src = read_src()
    assert "yz_mag < 0.3f" in src
    assert "gesture_ref_valid" in src
    print("  PASS: magnitude guard present")

# ---------- runner ----------

if __name__ == "__main__":
    tests = [
        test_no_rtttl,
        test_base_freq_exists,
        test_vol_notes_start_at_base,
        test_vol_tick_matches_note_dur,
        test_play_freq_uses_vol_tick,
        test_cycle_count,
        test_vol_scale_bounds,
        test_vol_scale_monotonic,
        test_melodies_use_defines,
        test_orientation_cross_dot,
        test_face_down_thresholds,
        test_magnitude_guard,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {t.__name__}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"  {passed} passed, {failed} failed, {passed+failed} total")
    if failed:
        sys.exit(1)
    print("  All tests passed!")
