# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MeshCore is a lightweight, portable C++ library for creating multi-hop wireless mesh networks using LoRa radios. It targets embedded systems (ESP32, NRF52, STM32, RP2040) using the Arduino framework and PlatformIO build system.

## Build Commands

```bash
# List all available firmware targets
sh build.sh list

# Build a specific firmware target (requires FIRMWARE_VERSION env var)
export FIRMWARE_VERSION=v1.0.0
sh build.sh build-firmware RAK_4631_repeater

# Build with PlatformIO directly
pio run -e <environment_name>

# Build without debug logging
export DISABLE_DEBUG=1
sh build.sh build-firmware Heltec_v3_repeater

# Build firmware categories
sh build.sh build-companion-firmwares
sh build.sh build-repeater-firmwares
sh build.sh build-room-server-firmwares

# Build all targets matching a string
sh build.sh build-matching-firmwares RAK_4631

# Format code
clang-format -i <file>
```

There are no automated tests. Validation is done through physical device testing.

### Flashing ESP32 Boards

The default LoRa frequency is 869.525 MHz (EU). For US 915 MHz boards, override it:
```bash
export PLATFORMIO_BUILD_FLAGS="-D LORA_FREQ=906.875"
```

**macOS Tahoe (26.x) issue:** PlatformIO's bundled esptool (v4.5.1) ships pyserial 3.5 which crashes with `termios.error: (22, 'Invalid argument')` when opening CP2102 serial ports. `pio run -t upload` will fail. Workaround: install standalone esptool and flash manually:
```bash
# Install standalone esptool (has working pyserial)
pipx install esptool

# Build with pio as normal
pio run -e Heltec_v3_repeater

# Flash with standalone esptool instead of pio upload
esptool --port /dev/cu.usbserial-0001 --chip esp32s3 --baud 460800 \
  write-flash -z --flash-mode dio --flash-freq 80m --flash-size detect \
  0x0    .pio/build/Heltec_v3_repeater/bootloader.bin \
  0x8000 .pio/build/Heltec_v3_repeater/partitions.bin \
  0x10000 .pio/build/Heltec_v3_repeater/firmware.bin
```

I2C errors during boot (e.g. `i2cRead returned Error -1`) are normal — the firmware probes for optional sensors (RTC, BME280, etc.) that may not be connected.

## Architecture

### Layered Design (bottom-up)

1. **Radio** (`src/Dispatcher.h`) — Abstract interface for LoRa radios via RadioLib (SX1262, SX1268, etc.)
2. **Dispatcher** (`src/Dispatcher.cpp/.h`) — Packet queue management, send/receive scheduling, airtime budgets, CAD collision avoidance. No dynamic allocation.
3. **Mesh** (`src/Mesh.cpp/.h`) — Multi-hop routing logic. Handles flood/direct/transport routing, cryptographic verification, packet deduplication. Defines virtual methods that application subclasses override.
4. **Application Layer** (`src/helpers/BaseChatMesh.cpp/.h`, `examples/`) — High-level features: contact management, message history, group channels, ACLs, sensor telemetry.

### Key Source Files

- `src/MeshCore.h` — Core constants (key sizes, packet limits) and abstract base classes (`MainBoard`, `RTCClock`)
- `src/Packet.h` — Packet structure: header (route type + payload type + version), payload (184 bytes max), path accumulation (64 bytes max)
- `src/Identity.h` — Ed25519 identity, X25519 ECDH key exchange, AES-128 encryption
- `src/Utils.h` — SHA-256, AES-128, CRC utilities
- `src/helpers/CommonCLI.cpp/.h` — Shared serial CLI commands across firmware types
- `src/helpers/StaticPoolPacketManager.h` — Fixed-size packet pool (zero allocation at runtime)
- `src/helpers/SimpleMeshTables.h` — Packet deduplication via hash tracking

### Routing Types

Packets use a 2-bit route type in the header:
- `ROUTE_TYPE_FLOOD` — Broadcast with path accumulation
- `ROUTE_TYPE_DIRECT` — Unicast with supplied path
- `ROUTE_TYPE_TRANSPORT_FLOOD` / `ROUTE_TYPE_TRANSPORT_DIRECT` — Filtered variants using transport codes

### Platform Abstraction

- `src/helpers/ESP32Board.cpp/.h`, `NRF52Board.cpp/.h` — Platform-specific `MainBoard` implementations
- `src/helpers/radiolib/` — RadioLib integration layer
- `src/helpers/bridges/` — Transport bridges (RS232, ESPNow)
- `src/helpers/sensors/` — Sensor drivers (GPS, BME280, etc.)
- `src/helpers/ui/` — Display drivers and UI components

### Variant System

`variants/` contains 69+ device-specific configurations. Each variant folder has:
- `platformio.ini` — PlatformIO environment definition (pins, flags, dependencies)
- Device-specific pin mappings and radio parameters

`boards/` contains board definition JSON files for PlatformIO.

### Example Firmware Applications

Each example in `examples/` is a complete firmware:
- `companion_radio` — BLE/WiFi/USB bridge for mobile app connectivity
- `simple_repeater` — Multi-hop relay node
- `simple_room_server` — BBS-style message board
- `simple_sensor` — Remote telemetry node
- `simple_secure_chat` — Terminal-based encrypted chat
- `kiss_modem` — Serial KISS protocol bridge

## Code Style

- 2-space indentation, no tabs
- 110 character column limit
- Attach braces (K&R style), `else`/`catch` on new line after closing brace
- Right-aligned pointers (`char *ptr` not `char* ptr`)
- Formatting defined in `.clang-format` — do NOT retroactively reformat existing code
- No dynamic memory allocation except during `setup()`/`begin()`
- Think embedded — keep code concise without unnecessary abstraction layers

## Collector Repeater (feature/collector-repeater branch)

### Goal

Build a passive mesh network observer: a repeater node that functions normally on the mesh
(relaying all traffic) while also dumping every packet it sees over USB serial to a host
computer. The host runs a long-lived Python service that stores all data in SQLite for
analysis — network topology, channel activity, routing patterns, node uptime, traffic mix.

### Architecture

**Firmware** (modified `simple_repeater`):
- Hooks into `logRxRaw()`, `logTx()`, `onAdvertRecv()` to capture all traffic
- Binary frame protocol v2 over serial: `[0xC0] [len_lo] [len_hi] [type] [seq(4B)] [payload...] [crc16(2B)]`
- 200KB ring buffer with sequence numbers, CRC-16 integrity, and ACK/RESUME handshake for reliable delivery
- Host-to-device frames: HOST_ACK (0xA0), HOST_RESUME (0xA1) for flow control
- Runtime-toggleable via CLI: `collector start|stop|status|diag`
- Frame types: RX_RAW (0xD0), TX_RAW (0xD1), ADVERTISEMENT (0xD2), HEARTBEAT (0xD3), DIAGNOSTICS (0xD4), HANDSHAKE (0xDF)
- Zero impact when disabled — just a bool check per packet

**Host** (Python + SQLite, `collector/` directory):
- Modular architecture: core service runs without TUI (headless RPi-ready)
- SQLite storage: raw_packets, advertisements, nodes, heartbeats, diagnostics
- pyos-based TUI with port selection and live dashboard
- JSON HTTP API for browser access from another machine
- 478 automated tests (protocol, store, TUI activities, diagnostics, reliable delivery, split view, hashtag channels)

### Current Status

- [x] Binary frame protocol (CollectorSerial.h)
- [x] Firmware hooks (logRxRaw, logTx, onAdvertRecv, heartbeat, CLI commands)
- [x] Python test script validating the device-to-PC API (collector_test.py)
- [x] Protocol module — frame constants, parsers, FrameReader (protocol.py)
- [x] SQLite schema and storage layer (store.py)
- [x] Standalone collector core — serial + store, no TUI dependency (core.py)
- [x] pyos TUI — port selection with persistence, live dashboard (app.py)
- [x] JSON HTTP API server for headless mode (api.py)
- [x] Automated UI tests using pyos testing harness (tests/)
- [x] Channel key configuration and group message decoding
- [x] Channel browser TUI activity with live updates
- [x] Dashboard enhancements: packet rate, traffic sparkline, payload type tracking
- [x] Packet detail view with hex dump (ENTER on packet)
- [x] Node detail view with SNR sparkline (ENTER on node)
- [x] Context-aware help overlay (? key from any screen)
- [x] Debug log viewer with live firmware text (d key)
- [x] WebSocket + HTTP server for real-time streaming (server.py)
- [x] Message search in channel browser (/ key) and HTTP API
- [x] Dashboard state persistence across screen transitions (service stays running on segue)
- [x] OS diagnostics frame (0xD4): MCU temp, heap, radio metrics, error flags, dedup stats
- [x] System diagnostics TUI screen (s key) with sparklines and human-readable formatting
- [x] /api/diagnostics endpoint and SQLite storage (schema v3)
- [x] Reliable delivery v2: ring buffer (200KB), seq numbers, CRC-16, ACK/RESUME handshake (schema v4)
- [x] Side-by-side split panel layout (SplitView component) for dashboard and channel browser
- [x] Hashtag channel support: `#name` entries auto-derive encryption key via SHA-256
- [x] IRC-style chat interface (ChatActivity) as new main screen with /commands
- [x] Channel cracker: passive dictionary attack on hashtag channels, retroactive decrypt, /crack command
- [ ] Analysis queries / richer dashboard views

### Key Files

- `examples/simple_repeater/CollectorSerial.h` — Binary frame protocol, ring buffer, CRC-16, reliable delivery
- `examples/simple_repeater/MyMesh.cpp` — Firmware hooks, drain() loop, handleCollectorFrame()
- `collector/protocol.py` — Frame constants, parsers, FrameReader state machine
- `collector/store.py` — SQLite schema and query methods
- `collector/core.py` — Standalone CollectorCore (serial + store, callback-driven)
- `collector/mesh_service.py` — pyos Service wrapper around CollectorCore
- `collector/activities/port_select.py` — Serial port picker with remembered selection
- `collector/crypto.py` — Channel decryption (AES-128-ECB, HMAC-SHA256 MAC), hashtag key derivation
- `collector/split_view.py` — Reusable side-by-side split panel component (SplitView)
- `collector/cracker.py` — Passive channel cracker: dictionary attack, hash table, retroactive decrypt, cache
- `collector/activities/chat.py` — IRC-style main screen with /commands, rooms sidebar, message panel
- `collector/activities/dashboard.py` — Live mesh traffic dashboard (secondary, via /nodes or /packets)
- `collector/activities/channels.py` — Channel message browser with live updates and search
- `collector/app.py` — TUI entry point (`python -m collector`)
- `collector/api.py` — JSON API server (`python -m collector.api`)
- `collector/server.py` — WebSocket + HTTP server (`python -m collector.server`)
- `collector/activities/packet_detail.py` — Packet detail view with hex dump
- `collector/activities/node_detail.py` — Node detail view with SNR sparkline
- `collector/activities/help_overlay.py` — Context-aware help screen (? key)
- `collector/activities/system_diag.py` — OS diagnostics screen (MCU temp, heap, radio, errors)
- `collector/activities/debug_log.py` — Live firmware debug log viewer
- `collector/tests/` — 574 automated tests (protocol, store, crypto, config, core integration, TUI activities, server, search, diagnostics, reliable delivery, split view, hashtag channels, chat interface, channel cracker)
- `collector/collector_test.py` — Device-to-PC API validation test

### Running

```bash
# Activate the collector venv
source collector/.venv/bin/activate

# TUI mode (interactive port selection)
python -m collector

# TUI mode (skip to dashboard)
python -m collector --port /dev/cu.usbserial-0001

# Headless mode (JSON API on port 8080)
python -m collector.api --port /dev/cu.usbserial-0001

# WebSocket + HTTP server (streams events to ws://0.0.0.0:8081)
python -m collector.server --port /dev/cu.usbserial-0001

# TUI with WebSocket server enabled
python -m collector --port /dev/cu.usbserial-0001 --serve 8081

# Run tests
python -m pytest collector/tests/ -v
```

## Contributing

- Submit PRs to the **`dev`** branch (not `main`)
- Open an issue first for anything impactful or architectural
- Protocol documentation lives in `docs/` (packet_format.md, companion_protocol.md, etc.)
