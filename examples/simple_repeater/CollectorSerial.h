#pragma once

#include <Arduino.h>
#include <MeshCore.h>
#include "CollectorSpool.h"

// Frame marker byte — non-printable, won't collide with text CLI output
#define COLLECTOR_FRAME_START   0xC0

// Collector frame types (device -> PC)
#define COLLECTOR_RX_RAW        0xD0
#define COLLECTOR_TX_RAW        0xD1
#define COLLECTOR_ADVERTISEMENT 0xD2
#define COLLECTOR_HEARTBEAT     0xD3
#define COLLECTOR_DIAGNOSTICS   0xD4
#define COLLECTOR_BOOT_INFO     0xD5
#define COLLECTOR_RX_DUP        0xD6   // compact duplicate of a recent RX_RAW (flood seen on another path)
#define COLLECTOR_HANDSHAKE     0xDF

// Number of recent RX invariant-hashes tracked for flood-duplicate collapse. A flood arriving on
// several paths hashes identically (path/SNR/RSSI excluded), so repeats become compact RX_DUP frames.
#ifndef COLLECTOR_DEDUP_SLOTS
#define COLLECTOR_DEDUP_SLOTS   48
#endif

// Status coalescing kicks in once this many entries are unacked (client behind / stalled / absent,
// or a reconnecting client still catching up). Below it the client is keeping up, so periodic
// HEARTBEAT/DIAGNOSTICS flow normally (the host's liveness watchdog needs them on an idle mesh).
// Keyed on unacked depth, not drain progress: drain() advances even when the transport drops the
// bytes, so "unsent" collapses to ~0 under a stalled link and can't signal a behind client.
#ifndef COLLECTOR_STATUS_COALESCE_MIN
#define COLLECTOR_STATUS_COALESCE_MIN   64
#endif

// Reset causes reported in BOOT_INFO (stable wire values, mapped from the
// platform's reset-reason API so the host doesn't depend on IDF enum numbering).
#define COLLECTOR_RESET_UNKNOWN   0
#define COLLECTOR_RESET_POWERON   1
#define COLLECTOR_RESET_SW        2
#define COLLECTOR_RESET_PANIC     3
#define COLLECTOR_RESET_WDT_INT   4
#define COLLECTOR_RESET_WDT_TASK  5
#define COLLECTOR_RESET_WDT_OTHER 6
#define COLLECTOR_RESET_BROWNOUT  7
#define COLLECTOR_RESET_DEEPSLEEP 8
#define COLLECTOR_RESET_EXT       9
// BOOT_INFO flags bitfield
#define COLLECTOR_BOOT_PREV_ALIVE 0x01   // a prior-run snapshot survived (not a cold boot)

// Host -> Device frame types
#define HOST_ACK                0xA0
#define HOST_RESUME             0xA1

#define COLLECTOR_PROTOCOL_VER  2

#define COLLECTOR_HEARTBEAT_INTERVAL  10000  // milliseconds
#define COLLECTOR_DIAG_INTERVAL      30000  // milliseconds

#ifndef COLLECTOR_RING_SIZE
  #ifdef ESP32
    #define COLLECTOR_RING_SIZE   (200 * 1024)
  #else
    // nRF52 / RP2040 / STM32 repeaters have far less RAM (STM32WL: 64KB) and this header is
    // compiled into every *_repeater env. A 200KB ring would fail to allocate — or worse,
    // succeed and starve the app/stack. Keep the default small; variants can override upward.
    #define COLLECTOR_RING_SIZE   (8 * 1024)
  #endif
#endif
// Ring size when PSRAM isn't available (e.g. Heltec V3). WiFi builds need the heap for the network stack.
#ifndef COLLECTOR_RING_FALLBACK
#define COLLECTOR_RING_FALLBACK (150 * 1024)
#endif
#define RING_SENTINEL           0xFFFF

// Ring entry format: [uint16_t entry_len][uint8_t type][uint32_t seq][payload...]
// entry_len includes the 2-byte length field: entry_len = 7 + payload_len
// Entries never straddle the end of the buffer — a sentinel marks unused tail space.

class CollectorSerial {
  Stream *_serial;

  // Ring buffer for reliable delivery
  uint8_t *_ring;
  uint32_t _ring_size;
  bool _ring_valid;
  uint32_t _head;         // oldest entry
  uint32_t _tail;         // next write position
  uint32_t _send_cursor;  // next entry to transmit
  uint32_t _next_seq;     // next sequence number to assign
  uint32_t _acked_seq;    // highest seq ACKed by host
  uint32_t _dropped_count;
  uint32_t _total_entries;
  uint32_t _unsent_entries; // entries written but not yet drained

  // Flash overflow tier: holds entries evicted from the RAM ring (older seqs). No-op until a
  // SpoolStore is attached via attachSpool() — when absent, the ring drops on overflow as before.
  FlashSpool _spool;

  // Flood-duplicate collapse: FIFO window of recent RX invariant-hashes. Off unless setDedup(true).
  uint32_t _dedup_hashes[COLLECTOR_DEDUP_SLOTS];
  uint8_t _dedup_next;
  uint8_t _dedup_count;
  bool _dedup_enabled;

  static uint32_t fnv1a(const uint8_t *data, uint16_t len, uint32_t h) {
    for (uint16_t i = 0; i < len; i++) { h ^= data[i]; h *= 16777619u; }
    return h;
  }
  bool dedupSeenOrInsert(uint32_t h) {
    for (uint8_t i = 0; i < _dedup_count; i++) {
      if (_dedup_hashes[i] == h) return true;
    }
    _dedup_hashes[_dedup_next] = h;
    _dedup_next = (uint8_t)((_dedup_next + 1) % COLLECTOR_DEDUP_SLOTS);
    if (_dedup_count < COLLECTOR_DEDUP_SLOTS) _dedup_count++;
    return false;
  }

  static uint16_t crc16_ccitt(const uint8_t *data, uint16_t len) {
    uint16_t crc = 0xFFFF;
    for (uint16_t i = 0; i < len; i++) {
      crc ^= (uint16_t)data[i] << 8;
      for (uint8_t j = 0; j < 8; j++) {
        if (crc & 0x8000)
          crc = (crc << 1) ^ 0x1021;
        else
          crc <<= 1;
      }
    }
    return crc;
  }

  // Write v1-format frame header directly to serial (handshake only)
  void writeFrameHeaderDirect(uint8_t type, uint16_t payload_len) {
    uint16_t frame_len = 1 + payload_len;
    uint8_t hdr[4];
    hdr[0] = COLLECTOR_FRAME_START;
    hdr[1] = frame_len & 0xFF;
    hdr[2] = frame_len >> 8;
    hdr[3] = type;
    _serial->write(hdr, 4);
  }

  void ringDropHead() {
    if (_total_entries == 0) return;
    // Skip sentinel if present
    uint16_t entry_len;
    memcpy(&entry_len, &_ring[_head], 2);
    if (entry_len == RING_SENTINEL) {
      if (_send_cursor == _head) _send_cursor = 0;
      _head = 0;
      memcpy(&entry_len, &_ring[_head], 2);
    }
    // Spill the entry we're about to evict to the flash overflow tier (if attached) so a
    // behind/disconnected host can still recover it. Only a true drop (no spool, or the spool
    // also overflowed) counts toward _dropped_count; the spool tracks its own overflow drops.
    if (_spool.valid() && entry_len <= 262) {
      uint8_t evicted[262];
      memcpy(evicted, &_ring[_head], entry_len);   // entries never straddle the end: contiguous
      _spool.append(evicted, entry_len, readSeqAt(_head));
    } else {
      _dropped_count++;
    }
    // Advance send_cursor if it points to the entry being dropped
    if (_send_cursor == _head) {
      _send_cursor = _head + entry_len;
      if (_unsent_entries > 0) _unsent_entries--;
    }
    _head += entry_len;
    _total_entries--;
    // Never leave head resting on a trailing sentinel: ringWrite's fits-check reads _head as the
    // boundary of free space, so a sentinel there would let a new entry overwrite live data.
    // (Latent corruption when entry sizes align exactly to the wrap boundary.)
    if (_total_entries > 0 && _head != _tail) {
      uint16_t next_len;
      memcpy(&next_len, &_ring[_head], 2);
      if (next_len == RING_SENTINEL) {
        if (_send_cursor == _head) _send_cursor = 0;
        _head = 0;
      }
    }
  }

  uint32_t readSeqAt(uint32_t pos) {
    uint32_t seq;
    memcpy(&seq, &_ring[pos + 3], 4);
    return seq;
  }

public:
  CollectorSerial()
    : _serial(nullptr), _ring(nullptr), _ring_size(0), _ring_valid(false),
      _head(0), _tail(0), _send_cursor(0), _next_seq(1), _acked_seq(0),
      _dropped_count(0), _total_entries(0), _unsent_entries(0),
      _dedup_next(0), _dedup_count(0), _dedup_enabled(false) {}

  void begin(Stream &serial) {
    _serial = &serial;
    _ring_size = COLLECTOR_RING_SIZE;
#ifdef ESP32
    _ring = (uint8_t *)ps_malloc(_ring_size + 2);  // +2 ensures sentinel always fits
    if (!_ring) {
      // No PSRAM — fall back to regular heap with reduced size
      _ring_size = COLLECTOR_RING_FALLBACK;
      _ring = (uint8_t *)malloc(_ring_size + 2);
    }
#else
    _ring = (uint8_t *)malloc(_ring_size + 2);  // +2 ensures sentinel always fits
#endif
    _ring_valid = (_ring != nullptr);
    if (!_ring_valid) {
      // Reliable delivery (seq/CRC/ACK) is unavailable; ringWrite() degrades to v1
      // direct writes. Surface it instead of silently dropping to the fallback protocol.
      MESH_DEBUG_PRINTLN("CollectorSerial: ring alloc of %u bytes failed; reliable delivery disabled",
                         (unsigned)(_ring_size + 2));
    }
    _head = 0;
    _tail = 0;
    _send_cursor = 0;
    _next_seq = 1;
    _acked_seq = 0;
    _dropped_count = 0;
    _total_entries = 0;
    _unsent_entries = 0;
    _dedup_next = 0;
    _dedup_count = 0;
  }

  // --- Ring buffer operations ---

  bool ringWrite(uint8_t type, const uint8_t *payload, uint16_t len) {
    if (!_ring_valid) {
      // Fallback: v1 direct write (no seq, no CRC)
      writeFrameHeaderDirect(type, len);
      if (len > 0) _serial->write(payload, len);
      return true;
    }

    uint16_t entry_len = 7 + len;  // 2(len) + 1(type) + 4(seq) + payload

    // If entry won't fit between tail and end of buffer, write sentinel and wrap
    if (_tail + entry_len > _ring_size) {
      _ring[_tail] = 0xFF;
      _ring[_tail + 1] = 0xFF;
      _tail = 0;
    }

    // Drop oldest entries until there's room
    while (_total_entries > 0) {
      bool fits;
      if (_tail < _head) {
        fits = (_tail + entry_len <= _head);
      } else if (_tail > _head) {
        fits = true;  // space between tail and end guaranteed by sentinel wrap
      } else {
        fits = false;  // tail == head with entries present: buffer full
      }
      if (fits) break;
      ringDropHead();
    }

    // After total eviction, reset head and send_cursor to tail so they
    // point where the new entry will be (not inside stale/overwritten data)
    if (_total_entries == 0) {
      _head = _tail;
      _send_cursor = _tail;
    }

    // Write entry
    uint32_t seq = _next_seq++;
    _ring[_tail + 0] = entry_len & 0xFF;
    _ring[_tail + 1] = entry_len >> 8;
    _ring[_tail + 2] = type;
    memcpy(&_ring[_tail + 3], &seq, 4);
    if (len > 0) memcpy(&_ring[_tail + 7], payload, len);
    _tail += entry_len;
    _total_entries++;
    _unsent_entries++;
    return true;
  }

  bool drain() {
    if (!_ring_valid) return false;

    // Flash overflow tier holds the older seqs — drain it first so the host receives entries in
    // strict sequence order. One frame per call, same as the RAM path.
    if (_spool.hasUnsent()) {
      uint8_t entry[262];
      uint16_t elen = 0;
      if (_spool.peekSend(entry, &elen)) {
        uint16_t scrc = crc16_ccitt(entry + 2, elen - 2);
        uint8_t shdr[3] = {COLLECTOR_FRAME_START, (uint8_t)(elen & 0xFF), (uint8_t)(elen >> 8)};
        _serial->write(shdr, 3);
        _serial->write(entry + 2, elen - 2);
        uint8_t scrcb[2] = {(uint8_t)(scrc & 0xFF), (uint8_t)(scrc >> 8)};
        _serial->write(scrcb, 2);
        _spool.advanceSent();
        return true;
      }
    }

    if (_unsent_entries == 0) return false;

    // Skip sentinel
    uint16_t entry_len;
    memcpy(&entry_len, &_ring[_send_cursor], 2);
    if (entry_len == RING_SENTINEL) {
      _send_cursor = 0;
      if (_send_cursor == _tail) return false;
      memcpy(&entry_len, &_ring[_send_cursor], 2);
    }

    uint16_t wire_len = entry_len;  // 1(type) + 4(seq) + payload + 2(crc) = 7 + payload = entry_len

    // Compute CRC over type + seq + payload (entry bytes 2..entry_len-1)
    uint16_t crc_data_len = entry_len - 2;
    uint16_t crc = crc16_ccitt(&_ring[_send_cursor + 2], crc_data_len);

    // Write v2 wire frame: [0xC0] [len_lo] [len_hi] [type] [seq] [payload] [crc16]
    uint8_t hdr[3];
    hdr[0] = COLLECTOR_FRAME_START;
    hdr[1] = wire_len & 0xFF;
    hdr[2] = wire_len >> 8;
    _serial->write(hdr, 3);
    _serial->write(&_ring[_send_cursor + 2], crc_data_len);  // type + seq + payload
    uint8_t crc_bytes[2] = {(uint8_t)(crc & 0xFF), (uint8_t)(crc >> 8)};
    _serial->write(crc_bytes, 2);

    _send_cursor += entry_len;
    _unsent_entries--;
    return true;
  }

  bool hasBacklog() const {
    return _ring_valid && (_unsent_entries > 0 || _spool.hasUnsent());
  }

  void handleAck(uint32_t ack_seq) {
    // Flash tier holds the older seqs: ack it first. RAM entries all carry higher seqs, so the
    // RAM loop below only reclaims anything once the spool is fully committed.
    _spool.handleAck(ack_seq);

    // Snapshot the send cursor's seq while it still points at a valid entry, so that
    // after dropping acked entries we can tell whether head advanced past it.
    bool cursor_valid = _unsent_entries > 0;
    uint32_t cursor_seq = cursor_valid ? readSeqAt(_send_cursor) : 0;

    while (_total_entries > 0) {
      uint16_t entry_len;
      memcpy(&entry_len, &_ring[_head], 2);
      if (entry_len == RING_SENTINEL) {
        _head = 0;
        continue;
      }
      if (readSeqAt(_head) > ack_seq) break;
      _head += entry_len;
      _total_entries--;
    }
    _acked_seq = ack_seq;

    // Keep the send cursor from falling behind head. A host that ACKs at or beyond
    // the cursor (buggy/aggressive host, or an ACK racing ahead of a RESUME) would
    // otherwise leave the cursor pointing into freed space that the next ringWrite()
    // overwrites, making drain() emit a garbage-length frame.
    if (_total_entries == 0) {
      _send_cursor = _head;
      _unsent_entries = 0;
    } else if (cursor_valid && ack_seq >= cursor_seq) {
      _send_cursor = _head;
      _unsent_entries = _total_entries;
    }
  }

  void handleResume(uint32_t from_seq) {
    // Replay the flash tier from from_seq too; drain() serves it before RAM, preserving order.
    _spool.handleResume(from_seq);
    _send_cursor = _head;
    uint32_t count = _total_entries;
    while (count > 0) {
      uint16_t entry_len;
      memcpy(&entry_len, &_ring[_send_cursor], 2);
      if (entry_len == RING_SENTINEL) {
        _send_cursor = 0;
        continue;
      }
      if (readSeqAt(_send_cursor) > from_seq) break;
      _send_cursor += entry_len;
      count--;
    }
    _unsent_entries = count;
  }

  void processIncoming(Stream &s) {
    // Consume start byte (0xC0)
    if (s.read() != COLLECTOR_FRAME_START) return;

    // Host frames are tiny (11B ACK/RESUME) and arrive as a unit. Read only what's
    // already buffered — never spin waiting on the main loop. If the frame isn't fully
    // present yet (rare fragmentation, or line noise), drop it; the host's RESUME
    // handshake recovers any missed ACK. This avoids stalling loop() (and CAD/airtime
    // scheduling) for tens of ms on a partial or stray 0xC0.
    if (s.available() < 2) return;
    uint8_t len_buf[2];
    len_buf[0] = s.read();
    len_buf[1] = s.read();

    uint16_t frame_len = len_buf[0] | ((uint16_t)len_buf[1] << 8);
    if (frame_len < 7 || frame_len > 20) return;  // host frames are small

    if (s.available() < (int)frame_len) return;
    uint8_t data[20];
    for (int idx = 0; idx < (int)frame_len; idx++) data[idx] = s.read();

    // Validate CRC (last 2 bytes are CRC of everything before)
    uint16_t crc_len = frame_len - 2;
    uint16_t expected = data[frame_len - 2] | ((uint16_t)data[frame_len - 1] << 8);
    if (crc16_ccitt(data, crc_len) != expected) return;

    uint8_t type = data[0];
    // seq at data[1..4] — ignored for host->device frames

    if (type == HOST_ACK && frame_len >= 11) {
      uint32_t ack_seq;
      memcpy(&ack_seq, &data[5], 4);
      handleAck(ack_seq);
    }
    else if (type == HOST_RESUME && frame_len >= 11) {
      uint32_t from_seq;
      memcpy(&from_seq, &data[5], 4);
      handleResume(from_seq);
    }
  }

  // Attach a flash overflow store (firmware: a preallocated file; tests: in-memory). Optional —
  // without it, the ring drops on overflow exactly as before. Passing hdr makes it durable across
  // reboots: a recovered spool resumes _next_seq from the persisted high-water mark so seqs are
  // never reused (which would trip the host's seq-reset detection).
  void attachSpool(SpoolStore *store, SpoolStore *hdr = nullptr) {
    _spool.begin(store, hdr);
    if (_spool.restoredNextSeq() > _next_seq) _next_seq = _spool.restoredNextSeq();
    if (_spool.newestSeq() >= _next_seq) _next_seq = _spool.newestSeq() + 1;
  }

  // Persist the durable spool header (call periodically; cheap, no-op unless durable + dirty).
  void persistSpool() {
    if (_spool.durable() && _spool.dirty()) _spool.persistHeader(_next_seq);
  }

  // --- Accessors ---

  uint32_t getOldestSeq() {
    // The spool holds the oldest seqs when it's non-empty.
    if (_spool.count() > 0) return _spool.oldestSeq();
    if (_total_entries == 0) return _next_seq > 1 ? _next_seq - 1 : 0;
    uint32_t pos = _head;
    uint16_t entry_len;
    memcpy(&entry_len, &_ring[pos], 2);
    if (entry_len == RING_SENTINEL) pos = 0;
    return readSeqAt(pos);
  }

  uint32_t getNewestSeq() {
    return _next_seq > 1 ? _next_seq - 1 : 0;
  }

  uint32_t getDroppedCount() const { return _dropped_count + _spool.dropped(); }
  uint32_t getTotalEntries() const { return _total_entries; }
  uint32_t getSpoolCount() const { return _spool.count(); }
  bool spoolActive() const { return _spool.valid(); }

  // Entries assigned but not yet ACKed by the host — grows unbounded when the client stalls/leaves.
  uint32_t unackedDepth() const {
    uint32_t newest = _next_seq > 1 ? _next_seq - 1 : 0;
    return newest > _acked_seq ? newest - _acked_seq : 0;
  }
  // Whether ephemeral status should be dropped (client behind). Not when the ring is invalid
  // (v1 fallback: no ACK/seq tracking, so always send).
  bool statusCoalesce() const { return _ring_valid && unackedDepth() > COLLECTOR_STATUS_COALESCE_MIN; }

  // --- Frame senders (buffer payload, then write to ring) ---

  void sendRxRaw(float snr, float rssi, const uint8_t *raw, int raw_len) {
    if (raw_len < 0) raw_len = 0;
    if (raw_len > 258) raw_len = 258;  // buf = 2 header + 258 payload; clamp drives both copy and length

    // Flood-duplicate collapse: the same message floods in on several paths as entries that differ
    // only in path accumulation + SNR/RSSI. Hash the invariant part (header + payload, skipping the
    // [path_len][path]); a recent match becomes a compact RX_DUP (keeps the path for topology, drops
    // the redundant payload). Packet layout: [header(1)][path_len(1)][path(path_len)][payload...].
    if (_dedup_enabled && raw_len >= 2) {
      // path_len is PACKED (Packet.h): low 6 bits = hop count, high 2 bits =
      // bytes-per-hop - 1, so the on-wire path is hash_count*hash_size bytes.
      // Using the raw byte as a length skipped past the real payload for the
      // ~1/3 of packets with >1-byte-per-hop paths, so they were never deduped.
      uint8_t path_len = raw[1];
      uint16_t path_bytes = (uint16_t)(path_len & 63) * ((path_len >> 6) + 1);
      uint16_t payload_off = (uint16_t)2 + path_bytes;
      if (payload_off <= (uint16_t)raw_len) {
        uint32_t h = fnv1a(raw, 1, 2166136261u);                              // header
        h = fnv1a(raw + payload_off, (uint16_t)raw_len - payload_off, h);     // payload (after path)
        if (dedupSeenOrInsert(h)) {
          sendRxDup(snr, rssi, h, path_len, raw + 2, path_bytes);
          return;
        }
      }
    }

    uint8_t buf[260];
    buf[0] = (uint8_t)(int8_t)(snr * 4);
    buf[1] = (uint8_t)(int8_t)(rssi);
    if (raw_len > 0) memcpy(buf + 2, raw, raw_len);
    ringWrite(COLLECTOR_RX_RAW, buf, 2 + raw_len);
  }

  // Compact duplicate: [snr(1)][rssi(1)][inv_hash(4 LE)][path_len(1)][path(path_bytes)].
  // path_len is the PACKED byte (size|count); path_bytes = count*size is the actual length.
  void sendRxDup(float snr, float rssi, uint32_t inv_hash, uint8_t path_len, const uint8_t *path, uint16_t path_bytes) {
    if (path_bytes > 64) path_bytes = 64;   // MeshCore MAX_PATH_SIZE
    uint8_t buf[72];
    buf[0] = (uint8_t)(int8_t)(snr * 4);
    buf[1] = (uint8_t)(int8_t)(rssi);
    memcpy(buf + 2, &inv_hash, 4);
    buf[6] = path_len;                       // packed byte, so the host can decode size+count
    if (path_bytes > 0) memcpy(buf + 7, path, path_bytes);
    ringWrite(COLLECTOR_RX_DUP, buf, 7 + path_bytes);
  }

  void setDedup(bool on) { _dedup_enabled = on; }

  void sendTxRaw(const uint8_t *raw, int raw_len) {
    ringWrite(COLLECTOR_TX_RAW, raw, raw_len);
  }

  void sendAdvertisement(uint32_t timestamp, int8_t snr_x4,
                         const uint8_t *pub_key,
                         const uint8_t *app_data, size_t app_data_len) {
    if (app_data_len > MAX_ADVERT_DATA_SIZE) app_data_len = MAX_ADVERT_DATA_SIZE;
    uint8_t buf[128];
    int pos = 0;
    memcpy(buf + pos, &timestamp, 4); pos += 4;
    buf[pos++] = (uint8_t)snr_x4;
    memcpy(buf + pos, pub_key, PUB_KEY_SIZE); pos += PUB_KEY_SIZE;
    if (app_data_len > 0) { memcpy(buf + pos, app_data, app_data_len); pos += app_data_len; }
    ringWrite(COLLECTOR_ADVERTISEMENT, buf, pos);
  }

  // force=true always buffers (an explicit `collector status` must answer); the periodic
  // loop() heartbeat leaves it false so it coalesces away under backlog.
  void sendHeartbeat(uint32_t timestamp, uint16_t battery_mv,
                     uint32_t rx_flood, uint32_t rx_direct,
                     uint32_t tx_flood, uint32_t tx_direct,
                     uint8_t free_pkts, uint32_t uptime_secs, bool force = false) {
    // HEARTBEAT is ephemeral status — only the latest matters. While the client is behind (many
    // unacked entries) or absent, buffering it would just evict real RX/TX traffic; skip it and let
    // the next tick (<=10s) carry fresh status once the client catches up. A keeping-up client has
    // few unacked entries, so heartbeats still flow for the host's liveness watchdog.
    if (!force && statusCoalesce()) return;
    uint8_t buf[27];
    int pos = 0;
    memcpy(buf + pos, &timestamp, 4); pos += 4;
    memcpy(buf + pos, &battery_mv, 2); pos += 2;
    memcpy(buf + pos, &rx_flood, 4); pos += 4;
    memcpy(buf + pos, &rx_direct, 4); pos += 4;
    memcpy(buf + pos, &tx_flood, 4); pos += 4;
    memcpy(buf + pos, &tx_direct, 4); pos += 4;
    buf[pos++] = free_pkts;
    memcpy(buf + pos, &uptime_secs, 4); pos += 4;
    ringWrite(COLLECTOR_HEARTBEAT, buf, pos);
  }

  void sendDiagnostics(float mcu_temp, uint32_t free_heap, uint32_t min_free_heap,
                       uint32_t total_heap, int16_t noise_floor, int16_t last_rssi,
                       int16_t last_snr_x4, uint32_t tx_airtime_ms, uint32_t rx_airtime_ms,
                       uint32_t recv_errors, uint16_t err_flags, uint16_t tx_queue_len,
                       uint16_t direct_dups, uint16_t flood_dups,
                       uint32_t n_recv, uint32_t n_sent, bool force = false) {
    // Ephemeral status (see sendHeartbeat): don't bury real traffic behind stale diagnostics.
    // force=true for an explicit `collector diag`; the periodic loop() diag coalesces when behind.
    if (!force && statusCoalesce()) return;
    uint8_t buf[50];
    int pos = 0;
    memcpy(buf + pos, &mcu_temp, 4); pos += 4;
    memcpy(buf + pos, &free_heap, 4); pos += 4;
    memcpy(buf + pos, &min_free_heap, 4); pos += 4;
    memcpy(buf + pos, &total_heap, 4); pos += 4;
    memcpy(buf + pos, &noise_floor, 2); pos += 2;
    memcpy(buf + pos, &last_rssi, 2); pos += 2;
    memcpy(buf + pos, &last_snr_x4, 2); pos += 2;
    memcpy(buf + pos, &tx_airtime_ms, 4); pos += 4;
    memcpy(buf + pos, &rx_airtime_ms, 4); pos += 4;
    memcpy(buf + pos, &recv_errors, 4); pos += 4;
    memcpy(buf + pos, &err_flags, 2); pos += 2;
    memcpy(buf + pos, &tx_queue_len, 2); pos += 2;
    memcpy(buf + pos, &direct_dups, 2); pos += 2;
    memcpy(buf + pos, &flood_dups, 2); pos += 2;
    memcpy(buf + pos, &n_recv, 4); pos += 4;
    memcpy(buf + pos, &n_sent, 4); pos += 4;
    ringWrite(COLLECTOR_DIAGNOSTICS, buf, pos);
  }

  // Durable boot/crash forensics, sent once per (re)connect. Tells the host why
  // the device last went down even though its RAM is gone — the reset cause plus
  // the last-known-alive stats that survived in RTC RAM across the reboot.
  void sendBootInfo(uint8_t reset_reason, uint8_t flags, uint16_t boot_count,
                    uint32_t prev_uptime_secs, uint32_t prev_heap_min,
                    int16_t prev_rssi, uint16_t prev_err_flags) {
    uint8_t buf[16];
    int pos = 0;
    buf[pos++] = reset_reason;
    buf[pos++] = flags;
    memcpy(buf + pos, &boot_count, 2); pos += 2;
    memcpy(buf + pos, &prev_uptime_secs, 4); pos += 4;
    memcpy(buf + pos, &prev_heap_min, 4); pos += 4;
    memcpy(buf + pos, &prev_rssi, 2); pos += 2;
    memcpy(buf + pos, &prev_err_flags, 2); pos += 2;
    ringWrite(COLLECTOR_BOOT_INFO, buf, pos);
  }

  void sendHandshake() {
    // Handshake uses v1 wire format — written directly to serial, not through ring buffer
    uint8_t ver = _ring_valid ? COLLECTOR_PROTOCOL_VER : 1;
    if (_ring_valid) {
      uint32_t oldest = getOldestSeq();
      uint32_t newest = getNewestSeq();
      writeFrameHeaderDirect(COLLECTOR_HANDSHAKE, 18);  // 9 + 1 + 4 + 4
      _serial->write((const uint8_t *)"COLLECTOR", 9);
      _serial->write(&ver, 1);
      _serial->write((const uint8_t *)&oldest, 4);
      _serial->write((const uint8_t *)&newest, 4);
    } else {
      writeFrameHeaderDirect(COLLECTOR_HANDSHAKE, 10);  // 9 + 1
      _serial->write((const uint8_t *)"COLLECTOR", 9);
      _serial->write(&ver, 1);
    }
  }
};
