#!/usr/bin/env python3
"""
T1000-E Accelerometer Axis Calibration Tool

Guides you through 6 orientations, records accelerometer data from
serial, and saves to a JSON file for analysis.

Usage: python calibrate_axes.py [serial_port]
  Default port: /dev/cu.usbmodem1112301
"""

import sys
import json
import time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/cu.usbmodem1112301"
BAUD = 115200
SAMPLES = 20  # samples per orientation
OUTPUT = "accel_calibration.json"

ORIENTATIONS = [
    ("front-up flat", "Lay it flat with the FRONT (button/buzzer/LED) facing UP toward the ceiling"),
    ("front-down flat", "Flip it so the front (button/buzzer/LED) faces DOWN into the table"),
    ("standing on bottom edge", "Stand it on its BOTTOM edge so the front face is vertical, like a phone standing up"),
    ("standing on top edge", "Flip that — stand it on its TOP edge, front still vertical but upside-down"),
    ("standing on left edge", "Stand it on its LEFT edge, front face vertical"),
    ("standing on right edge", "Stand it on its RIGHT edge, front face vertical"),
    ("tilted right ~45 deg", "Hold it front-up, tilt the RIGHT side DOWN about 45 degrees"),
    ("tilted left ~45 deg", "Hold it front-up, tilt the LEFT side DOWN about 45 degrees"),
    ("tilted forward ~45 deg", "Hold it front-up, tilt the TOP edge AWAY from you about 45 degrees"),
    ("tilted backward ~45 deg", "Hold it front-up, tilt the TOP edge TOWARD you about 45 degrees"),
]


def parse_accel_line(line: str):
    """Extract gX, gY, gZ from a debug log line like:
    RAW x=... FILT gX=-0.14 gY=-1.02 gZ=1.95 PM=...
    """
    try:
        parts = {}
        for token in line.split():
            if "=" in token:
                key, val = token.split("=", 1)
                parts[key] = val
        if "gX" in parts and "gY" in parts and "gZ" in parts:
            return float(parts["gX"]), float(parts["gY"]), float(parts["gZ"])
    except (ValueError, IndexError):
        pass
    return None


def drain_until_live(ser):
    """Read and discard lines until one takes real time to arrive.
    If readline() returns instantly, it was buffered. Once it takes
    >200ms, we know we're caught up to the live stream."""
    print("  Draining buffer...", end="", flush=True)
    while True:
        t0 = time.monotonic()
        ser.readline()
        elapsed = time.monotonic() - t0
        if elapsed > 0.2:
            break
    print(" ready.")


def collect_samples(ser, n):
    """Read n valid accelerometer samples from serial."""
    drain_until_live(ser)
    samples = []
    while len(samples) < n:
        line = ser.readline().decode("utf-8", errors="replace").strip()
        parsed = parse_accel_line(line)
        if parsed:
            samples.append(parsed)
            gx, gy, gz = parsed
            print(f"  [{len(samples):2d}/{n}] gX={gx:+.3f}  gY={gy:+.3f}  gZ={gz:+.3f}")
    return samples


def avg(samples):
    n = len(samples)
    return (
        sum(s[0] for s in samples) / n,
        sum(s[1] for s in samples) / n,
        sum(s[2] for s in samples) / n,
    )


def main():
    print(f"Connecting to {PORT} at {BAUD} baud...")
    try:
        ser = serial.Serial(PORT, BAUD, timeout=2)
    except serial.SerialException as e:
        print(f"Error: {e}")
        sys.exit(1)

    time.sleep(1)
    ser.reset_input_buffer()

    print(f"\n=== T1000-E Accelerometer Axis Calibration ===")
    print(f"Will record {SAMPLES} samples for each of {len(ORIENTATIONS)} orientations.\n")

    results = []

    for i, (name, instruction) in enumerate(ORIENTATIONS):
        print(f"\n--- Orientation {i+1}/{len(ORIENTATIONS)}: {name} ---")
        print(f"  {instruction}")
        input("  Press ENTER when ready (hold steady)... ")

        print(f"  Recording {SAMPLES} samples...")
        samples = collect_samples(ser, SAMPLES)
        mean = avg(samples)
        print(f"  Average: gX={mean[0]:+.3f}  gY={mean[1]:+.3f}  gZ={mean[2]:+.3f}")

        results.append({
            "orientation": name,
            "instruction": instruction,
            "samples": [{"gX": s[0], "gY": s[1], "gZ": s[2]} for s in samples],
            "mean": {"gX": mean[0], "gY": mean[1], "gZ": mean[2]},
        })

    ser.close()

    # Save results
    with open(OUTPUT, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n=== Saved to {OUTPUT} ===")

    # Print summary table
    print(f"\n{'Orientation':<40s}  {'gX':>7s}  {'gY':>7s}  {'gZ':>7s}")
    print("-" * 67)
    for r in results:
        m = r["mean"]
        print(f"{r['orientation']:<40s}  {m['gX']:+.3f}  {m['gY']:+.3f}  {m['gZ']:+.3f}")


if __name__ == "__main__":
    main()
