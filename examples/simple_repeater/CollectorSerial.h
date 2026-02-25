#pragma once

#include <Arduino.h>
#include <MeshCore.h>

// Frame marker byte — non-printable, won't collide with text CLI output
#define COLLECTOR_FRAME_START   0xC0

// Collector frame types (device -> PC)
#define COLLECTOR_RX_RAW        0xD0
#define COLLECTOR_TX_RAW        0xD1
#define COLLECTOR_ADVERTISEMENT 0xD2
#define COLLECTOR_HEARTBEAT     0xD3
#define COLLECTOR_DIAGNOSTICS   0xD4
#define COLLECTOR_HANDSHAKE     0xDF

// Host -> Device frame types
#define HOST_ACK                0xA0
#define HOST_RESUME             0xA1

#define COLLECTOR_PROTOCOL_VER  2

#define COLLECTOR_HEARTBEAT_INTERVAL  10000  // milliseconds
#define COLLECTOR_DIAG_INTERVAL      30000  // milliseconds

#ifndef COLLECTOR_RING_SIZE
#define COLLECTOR_RING_SIZE     (200 * 1024)
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
    // Advance send_cursor if it points to the entry being dropped
    if (_send_cursor == _head) {
      _send_cursor = _head + entry_len;
      if (_unsent_entries > 0) _unsent_entries--;
    }
    _head += entry_len;
    _total_entries--;
    _dropped_count++;
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
      _dropped_count(0), _total_entries(0), _unsent_entries(0) {}

  void begin(Stream &serial) {
    _serial = &serial;
    _ring_size = COLLECTOR_RING_SIZE;
#ifdef ESP32
    _ring = (uint8_t *)ps_malloc(_ring_size + 2);  // +2 ensures sentinel always fits
    if (!_ring) {
      // No PSRAM — fall back to regular heap with reduced size
      _ring_size = 150 * 1024;
      _ring = (uint8_t *)malloc(_ring_size + 2);
    }
#else
    _ring = (uint8_t *)malloc(_ring_size + 2);
#endif
    _ring_valid = (_ring != nullptr);
    _head = 0;
    _tail = 0;
    _send_cursor = 0;
    _next_seq = 1;
    _acked_seq = 0;
    _dropped_count = 0;
    _total_entries = 0;
    _unsent_entries = 0;
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
    if (!_ring_valid || _unsent_entries == 0) return false;

    // Skip sentinel
    uint16_t entry_len;
    memcpy(&entry_len, &_ring[_send_cursor], 2);
    if (entry_len == RING_SENTINEL) {
      _send_cursor = 0;
      if (_send_cursor == _tail) return false;
      memcpy(&entry_len, &_ring[_send_cursor], 2);
    }

    // Check backpressure — need room for 0xC0 + len(2) + frame data
    uint16_t wire_len = entry_len;  // 1(type) + 4(seq) + payload + 2(crc) = 7 + payload = entry_len
    if (_serial->availableForWrite() < (int)(3 + wire_len)) return false;

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
    return _ring_valid && _unsent_entries > 0;
  }

  void handleAck(uint32_t ack_seq) {
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
  }

  void handleResume(uint32_t from_seq) {
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

    unsigned long deadline = millis() + 50;
    uint8_t len_buf[2];
    int idx = 0;
    while (idx < 2 && millis() < deadline) {
      if (s.available()) len_buf[idx++] = s.read();
    }
    if (idx < 2) return;

    uint16_t frame_len = len_buf[0] | ((uint16_t)len_buf[1] << 8);
    if (frame_len < 7 || frame_len > 20) return;  // host frames are small

    uint8_t data[20];
    idx = 0;
    while (idx < (int)frame_len && millis() < deadline) {
      if (s.available()) data[idx++] = s.read();
    }
    if (idx < (int)frame_len) return;

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

  // --- Accessors ---

  uint32_t getOldestSeq() {
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

  uint32_t getDroppedCount() const { return _dropped_count; }
  uint32_t getTotalEntries() const { return _total_entries; }

  // --- Frame senders (buffer payload, then write to ring) ---

  void sendRxRaw(float snr, float rssi, const uint8_t *raw, int raw_len) {
    uint8_t buf[260];
    buf[0] = (uint8_t)(int8_t)(snr * 4);
    buf[1] = (uint8_t)(int8_t)(rssi);
    if (raw_len > 0 && raw_len <= 258) memcpy(buf + 2, raw, raw_len);
    ringWrite(COLLECTOR_RX_RAW, buf, 2 + raw_len);
  }

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

  void sendHeartbeat(uint32_t timestamp, uint16_t battery_mv,
                     uint32_t rx_flood, uint32_t rx_direct,
                     uint32_t tx_flood, uint32_t tx_direct,
                     uint8_t free_pkts, uint32_t uptime_secs) {
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
                       uint32_t n_recv, uint32_t n_sent) {
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
