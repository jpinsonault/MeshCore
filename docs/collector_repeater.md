# Collector Repeater

A repeater that relays mesh traffic normally and also streams every packet it hears to a computer, which stores
it in SQLite (`collector/`). The firmware lives in `examples/simple_repeater`, so every `*_repeater` build has it.
The collector is off until a host sends `collector start`.

The WiFi build, `Heltec_v3_collector_wifi`, can also be reached over your network, configured and flashed
without a cable. That means a few ways of connecting that a stock repeater doesn't have. This page covers them.

## Ways to connect

Every link carries the same byte stream: CLI text (echoed input, `  -> reply` lines) with binary collector frames
mixed in. All commands work on every link, including the ones the CLI docs mark as serial-only.

| Link | Address | Login | Used by |
|---|---|---|---|
| USB serial | `COMx` / `/dev/tty…`, 115200 baud | none | first-time setup, `pio` flashing, any serial terminal |
| TCP | `<host>:5005` | `auth <admin password>` | the Python collector (`socket://` port) |
| WebSocket | `ws://<host>/ws` (binary messages) | `auth <admin password>` | the config page, `python -m collector.remote` |
| Firmware upload | espota on UDP 3232, or `http://<host>/update` | admin password (`/update`: user `admin`) | network flashing |
| LoRa remote admin | the MeshCore app | admin password | stock repeater management |

- `<host>` is the node name in lowercase with other characters turned into `-`, plus `.local`
  (e.g. "My Repeater" becomes `my-repeater.local`), or the node's IP. `wifi status` prints both.
- Each network link takes one client. A new connection replaces the old one, so a dead session can't lock it.
  `collector.remote` uses the WebSocket, so it doesn't kick a collector off TCP.
- Collector frames go to the first logged-in link (TCP, then WebSocket), otherwise to USB.
- Plain HTTP on your LAN: the password crosses the network unencrypted. Fine at home, not on shared networks.

## Credentials: `.env`

Device credentials live in a gitignored `.env` at the repo root, so tools can use them without typing them:

```
MESHCORE_HOST=my-repeater.local
MESHCORE_PASSWORD=<admin password>
```

Copy `.env.example` to start. `collector.remote` and the collector both read it; real environment variables win.

- Network firmware updates check the admin password the node had **at boot**. After changing the password,
  reboot before flashing over the network.
- On Windows, Python can stall resolving `.local` names (it tries IPv6 first). If it does, put the IP in `.env`.

## First-time setup (USB)

1. Flash over USB and confirm it landed. One upload once reported success without writing the app:
   ```bash
   pio run -e Heltec_v3_collector_wifi -t upload --upload-port COM3
   pio pkg exec -p tool-esptoolpy -- esptool.py --chip esp32s3 --port COM3 \
     verify_flash 0x10000 .pio/build/Heltec_v3_collector_wifi/firmware.bin
   ```
   Settings, identity and ACL survive a firmware flash. **Never run `pio run -t uploadfs`**: it replaces the whole
   filesystem, wiping the node's key, settings, ACL and WiFi config.
2. Join WiFi, either from the config page's WiFi card (below) or in a serial terminal:
   ```
   wifi ssid <network name>
   wifi pass <wifi password>
   wifi on
   wifi status
   ```
3. Change the admin password from the default (`password`), then `reboot`.
4. Fill in `.env`. From then on the cable is only needed for power.

## Day to day

**Commands over WiFi**
```bash
collector/.venv/Scripts/python -m collector.remote cmd "wifi status" neighbors stats-packets
```

**Flash over WiFi** (prints the version before and after):
```bash
pio run -e Heltec_v3_collector_wifi
collector/.venv/Scripts/python -m collector.remote flash            # or: flash path/to/firmware.bin
```
Alternatives: open `http://<host>/update` in a browser, or
`PLATFORMIO_UPLOAD_FLAGS="--auth=<pw>" pio run -e Heltec_v3_collector_wifi -t upload --upload-port <ip>`.
Windows Firewall may ask to allow Python the first time, because the node connects back to fetch the image.

**Config page.** The fork at `../config.meshcore.io` (branch `collector-wifi`, github.com/jpinsonault/config.meshcore.io)
adds **Connect WiFi** next to Connect USB, plus a WiFi card that's shown when the node supports it. Serve it locally:
```bash
python -m http.server 8000 --directory ../config.meshcore.io
```
Then open `http://localhost:8000` in Chrome or Edge. USB needs Web Serial, which some browsers (e.g. DuckDuckGo) lack.

**Collector over WiFi**
```bash
collector/.venv/Scripts/python -m collector --port socket://<host>:5005     # password from .env
collector/.venv/Scripts/python -m collector.server --port socket://<host>:5005
```
The node keeps buffering while no collector is connected (128KB ring, roughly 1,000–2,500 packets) and replays
from where the host left off when it reconnects.

## CLI additions

| Command | Notes |
|---|---|
| `wifi status` | connection, IP, RSSI, hostname, which links are logged in, free heap |
| `wifi ssid <name>` / `wifi pass <password>` | stored in `/collector_wifi`; local links only (not LoRa) |
| `wifi on` / `wifi off` | `on` reconnects with the stored credentials |
| `start ota` | with WiFi up, points to `/update` instead of starting the stock hotspot (it would clash on port 80) |
| `collector start\|stop\|status\|diag\|inject\|send\|screen` | collector control (see `CLAUDE.md`) |

## Gotchas

- **Clock.** The Heltec V3 has no battery-backed clock. After a power loss it restarts at 15 May 2024 (soft reboots
  and network flashing keep the time). Set it with `collector.remote cmd "time $(date +%s)"` or `clock sync` from the
  app. `time` only moves the clock forward.
- **Memory.** No PSRAM, so the ring buffer is capped at 128KB (`COLLECTOR_RING_FALLBACK`). With WiFi and the web
  server up, about 73KB of heap is left. Watch `heap=` in `wifi status` after adding features.
- **Commands that print straight to USB** (`get acl`, `log`) show their output only on USB serial, not on network links.
- **Windows `pio`.** If `pio` fails with "uv trampoline failed to canonicalize script path", run PlatformIO through
  its own Python instead: `%APPDATA%\uv\tools\platformio\Scripts\python.exe -m platformio run ...`.
