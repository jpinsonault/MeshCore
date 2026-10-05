#pragma once

#include <Arduino.h>
#include <string.h>

// Flash-backed overflow spool for the collector ring buffer.
//
// The RAM ring (CollectorSerial) holds the newest entries. When it would evict an unacked
// entry to make room, it spills that entry here instead of dropping it, so a host that is
// behind (or briefly disconnected) can recover far more than RAM alone holds. The spool is a
// circular log using the SAME on-wire entry format as the ring:
//     [uint16_t entry_len][uint8_t type][uint32_t seq][payload...]   (entry_len = 7 + payload)
// Entries never straddle the end — a 0xFFFF sentinel marks unused tail space (mirrors the ring).
//
// Ordering: the spool always holds strictly OLDER seqs than the RAM ring, so draining the spool
// first (oldest -> newest) preserves global seq order. Only when the spool ALSO overflows is an
// entry truly lost (counted in dropped()).
//
// The operations mirror CollectorSerial's tested ring logic (ringWrite / drain / ringDropHead /
// handleAck / handleResume) so correctness carries over by analogy.
//
// v1 (non-durable): head/tail/cursor live in RAM, so a reboot abandons spooled data. A durable
// header is a planned follow-up.

#define SPOOL_SENTINEL  0xFFFF

// Random-access byte store backing the spool. Firmware wraps a preallocated SPIFFS file
// (seek + read/write); native tests wrap an in-memory buffer. readAt/writeAt must cover the
// full [off, off+len) range and return false on any failure.
class SpoolStore {
public:
  virtual ~SpoolStore() {}
  virtual bool readAt(uint32_t off, uint8_t *buf, uint16_t len) = 0;
  virtual bool writeAt(uint32_t off, const uint8_t *buf, uint16_t len) = 0;
  virtual uint32_t capacity() const = 0;   // usable bytes (entries fit within this)
};

class FlashSpool {
  SpoolStore *_store;
  uint32_t _size;          // usable capacity (== store capacity)
  bool _valid;
  uint32_t _head;          // offset of oldest entry
  uint32_t _tail;          // next write offset
  uint32_t _send_cursor;   // offset of next entry to transmit
  uint32_t _count;         // entries held
  uint32_t _unsent;        // entries from send_cursor..tail not yet drained
  uint32_t _dropped;       // entries lost to spool overflow
  uint32_t _newest_seq;

  // Durability (optional): a tiny separate store holds a CRC'd header of the index + the owner's
  // next_seq, so a reboot recovers the spilled backlog instead of abandoning it. Entry bytes
  // already persist in the data file; only the RAM index + seq counter need saving.
  SpoolStore *_hdr;
  bool _dirty;
  uint32_t _restored_next_seq;

  static uint16_t crc16(const uint8_t *data, uint16_t len) {
    uint16_t crc = 0xFFFF;
    for (uint16_t i = 0; i < len; i++) {
      crc ^= (uint16_t)data[i] << 8;
      for (uint8_t j = 0; j < 8; j++) crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : (crc << 1);
    }
    return crc;
  }

  uint16_t entryLenAt(uint32_t off) {
    uint8_t b[2];
    if (!_store->readAt(off, b, 2)) return 0;
    return (uint16_t)(b[0] | (b[1] << 8));
  }
  uint32_t seqAt(uint32_t off) {
    uint8_t b[4];
    if (!_store->readAt(off + 3, b, 4)) return 0;
    uint32_t s;
    memcpy(&s, b, 4);
    return s;
  }

  // Evict the oldest entry (overflow only). Mirrors ring ringDropHead, incl. the _dropped count.
  void dropHead() {
    if (_count == 0) return;
    uint16_t entry_len = entryLenAt(_head);
    if (entry_len == SPOOL_SENTINEL) {
      if (_send_cursor == _head) _send_cursor = 0;
      _head = 0;
      entry_len = entryLenAt(_head);
    }
    if (_send_cursor == _head) {
      _send_cursor = _head + entry_len;
      if (_unsent > 0) _unsent--;
    }
    _head += entry_len;
    _count--;
    _dropped++;
    _dirty = true;
    // Never leave head resting on a trailing sentinel: the next append's fits-arithmetic reads
    // _head as the boundary of free space, so a sentinel there would let it overwrite live data.
    if (_count > 0 && _head != _tail && entryLenAt(_head) == SPOOL_SENTINEL) {
      if (_send_cursor == _head) _send_cursor = 0;
      _head = 0;
    }
  }

  // --- Durable header (magic + index + owner next_seq, CRC'd) ---
  static const uint32_t HDR_MAGIC = 0x314C5053u;   // "SPL1"
  bool restoreHeader() {
    uint8_t b[34];
    if (!_hdr->readAt(0, b, 34)) return false;
    uint32_t magic;
    memcpy(&magic, b, 4);
    if (magic != HDR_MAGIC || b[4] != 1) return false;
    uint16_t want = (uint16_t)(b[32] | (b[33] << 8));
    if (crc16(b, 32) != want) return false;
    uint32_t head, tail, count, newest, nextseq;
    memcpy(&head, b + 8, 4); memcpy(&tail, b + 12, 4);
    memcpy(&count, b + 20, 4); memcpy(&newest, b + 24, 4); memcpy(&nextseq, b + 28, 4);
    if (head >= _size || tail >= _size) return false;   // corrupt / stale against this file size
    _head = head; _tail = tail; _count = count; _newest_seq = newest; _restored_next_seq = nextseq;
    _dropped = 0;
    return true;
  }

public:
  FlashSpool() : _store(nullptr), _size(0), _valid(false), _head(0), _tail(0), _send_cursor(0),
                 _count(0), _unsent(0), _dropped(0), _newest_seq(0),
                 _hdr(nullptr), _dirty(false), _restored_next_seq(0) {}

  // hdr (optional) makes the spool durable across reboots: a valid header restores the spilled
  // backlog; otherwise the spool starts empty (non-durable).
  void begin(SpoolStore *store, SpoolStore *hdr = nullptr) {
    _store = store;
    _hdr = hdr;
    _size = store ? store->capacity() : 0;
    _valid = (_store != nullptr && _size >= 64);
    _dirty = false;
    _restored_next_seq = 0;
    if (_valid && _hdr && restoreHeader()) {
      // Index recovered from flash. ACK state is RAM-only (lost on reboot), so replay the whole
      // surviving backlog from the oldest entry; the host's RESUME trims what it already committed.
      _send_cursor = _head;
      _unsent = _count;
      return;
    }
    _head = _tail = _send_cursor = 0;
    _count = _unsent = _dropped = 0;
    _newest_seq = 0;
  }

  // Write the current index + owner next_seq to the header store (small, call periodically when
  // dirty()). Cheap enough for its own tiny file; clears the dirty flag.
  void persistHeader(uint32_t owner_next_seq) {
    if (!_hdr) return;
    uint8_t b[34];
    memset(b, 0, sizeof(b));
    uint32_t magic = HDR_MAGIC;
    memcpy(b, &magic, 4);
    b[4] = 1;
    memcpy(b + 8, &_head, 4); memcpy(b + 12, &_tail, 4); memcpy(b + 16, &_send_cursor, 4);
    memcpy(b + 20, &_count, 4); memcpy(b + 24, &_newest_seq, 4); memcpy(b + 28, &owner_next_seq, 4);
    uint16_t crc = crc16(b, 32);
    b[32] = (uint8_t)(crc & 0xFF);
    b[33] = (uint8_t)(crc >> 8);
    _hdr->writeAt(0, b, 34);
    _dirty = false;
  }

  bool valid() const { return _valid; }
  bool durable() const { return _hdr != nullptr; }
  bool dirty() const { return _dirty; }
  uint32_t restoredNextSeq() const { return _restored_next_seq; }
  bool hasUnsent() const { return _valid && _unsent > 0; }
  uint32_t count() const { return _count; }
  uint32_t unsent() const { return _unsent; }
  uint32_t dropped() const { return _dropped; }
  uint32_t newestSeq() const { return _newest_seq; }
  uint32_t oldestSeq() {   // reads the store; valid only while count() > 0
    if (_count == 0) return 0;
    uint32_t off = _head;
    if (entryLenAt(off) == SPOOL_SENTINEL) off = 0;
    return seqAt(off);
  }

  // Append a complete ring entry (entry_len bytes starting with the 2-byte length field).
  // Mirrors ring ringWrite. Returns false if invalid or the entry can't fit even when empty.
  bool append(const uint8_t *entry, uint16_t entry_len, uint32_t seq) {
    if (!_valid || entry_len < 7 || entry_len > _size) return false;

    if (_tail + entry_len > _size) {   // won't fit before the end: sentinel + wrap
      uint8_t sent[2] = {0xFF, 0xFF};
      _store->writeAt(_tail, sent, 2);
      _tail = 0;
    }

    while (_count > 0) {               // evict oldest until the entry fits
      bool fits;
      if (_tail < _head) fits = (_tail + entry_len <= _head);
      else if (_tail > _head) fits = true;
      else fits = false;               // tail == head with entries present: full
      if (fits) break;
      dropHead();
    }
    if (_count == 0) { _head = _tail; _send_cursor = _tail; }

    _store->writeAt(_tail, entry, entry_len);
    _tail += entry_len;
    _count++;
    _unsent++;
    _newest_seq = seq;
    _dirty = true;
    return true;
  }

  // Read the next unsent entry (for drain) into buf (hold >= 262). Does NOT advance — the caller
  // frames + sends it, then calls advanceSent(). Mirrors the read half of ring drain().
  bool peekSend(uint8_t *buf, uint16_t *out_len) {
    if (!_valid || _unsent == 0) return false;
    uint16_t entry_len = entryLenAt(_send_cursor);
    if (entry_len == SPOOL_SENTINEL) {
      _send_cursor = 0;
      entry_len = entryLenAt(_send_cursor);
    }
    if (entry_len < 7 || entry_len > _size) return false;
    if (!_store->readAt(_send_cursor, buf, entry_len)) return false;
    *out_len = entry_len;
    return true;
  }

  void advanceSent() {
    if (_unsent == 0) return;
    uint16_t entry_len = entryLenAt(_send_cursor);
    if (entry_len == SPOOL_SENTINEL) { _send_cursor = 0; entry_len = entryLenAt(_send_cursor); }
    _send_cursor += entry_len;
    _unsent--;
  }

  // Drop every entry with seq <= ack_seq (host committed them). NOT counted as dropped().
  // Mirrors ring handleAck, including the send-cursor resync. Returns true if the spool is now
  // empty, so the caller may continue applying the ack to the RAM ring.
  bool handleAck(uint32_t ack_seq) {
    bool cursor_valid = _unsent > 0;
    uint32_t cursor_seq = cursor_valid ? seqAt(_send_cursor) : 0;

    while (_count > 0) {
      uint16_t entry_len = entryLenAt(_head);
      if (entry_len == SPOOL_SENTINEL) { _head = 0; continue; }
      if (seqAt(_head) > ack_seq) break;
      _head += entry_len;
      _count--;
    }

    if (_count == 0) {
      _send_cursor = _head;
      _unsent = 0;
    } else if (cursor_valid && ack_seq >= cursor_seq) {
      _send_cursor = _head;
      // recompute unsent: everything remaining is now unsent again from head
      _unsent = _count;
    }
    _dirty = true;
    return _count == 0;
  }

  // Replay from the entry just after from_seq: reset the send cursor to the oldest entry whose
  // seq > from_seq, recomputing unsent. Mirrors ring handleResume. Used on host RESUME/reconnect.
  void handleResume(uint32_t from_seq) {
    _send_cursor = _head;
    uint32_t remaining = _count;
    while (remaining > 0) {
      uint16_t entry_len = entryLenAt(_send_cursor);
      if (entry_len == SPOOL_SENTINEL) { _send_cursor = 0; continue; }
      if (seqAt(_send_cursor) > from_seq) break;
      _send_cursor += entry_len;
      remaining--;
    }
    _unsent = remaining;
  }
};
