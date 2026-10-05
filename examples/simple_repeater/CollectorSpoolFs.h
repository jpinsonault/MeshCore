#pragma once

#include <Arduino.h>
#include <FS.h>
#include "CollectorSpool.h"

// SpoolStore backed by a fixed-size file on an Arduino filesystem (SPIFFS/LittleFS/InternalFS).
//
// The file is preallocated to its full size ONCE (first boot, or if the size changed) and reused
// thereafter — rewriting it every boot would be slow and burn a flash-erase cycle each time. v1 is
// non-durable: the RAM-held ring indices start empty every boot, so any stale bytes left in the
// file from a previous run are never read as valid entries (peekSend/readAt only touch the region
// written this session).
class FsSpoolStore : public SpoolStore {
  File _f;
  uint32_t _size;
  bool _ok;

public:
  FsSpoolStore() : _size(0), _ok(false) {}

  // Returns the usable size actually secured (0 on failure). Caller should clamp `size` to the
  // filesystem's free space before calling.
  uint32_t begin(fs::FS &fs, const char *path, uint32_t size) {
    _size = 0;
    _ok = false;
    if (size < 64) return 0;

    // Reuse an existing, large-enough preallocation.
    File f = fs.open(path, "r+");
    if (f && (uint32_t)f.size() >= size) {
      _f = f;
      _size = size;
      _ok = true;
      return _size;
    }
    if (f) f.close();

    // Create + preallocate once.
    File w = fs.open(path, "w", true);
    if (!w) return 0;
    uint8_t zeros[256];
    memset(zeros, 0, sizeof(zeros));
    uint32_t written = 0;
    while (written < size) {
      uint32_t chunk = (size - written < sizeof(zeros)) ? (size - written) : (uint32_t)sizeof(zeros);
      if (w.write(zeros, chunk) != chunk) { w.close(); fs.remove(path); return 0; }  // out of space
      written += chunk;
    }
    w.close();

    _f = fs.open(path, "r+");
    if (!_f || (uint32_t)_f.size() < size) return 0;
    _size = size;
    _ok = true;
    return _size;
  }

  bool readAt(uint32_t off, uint8_t *buf, uint16_t len) override {
    if (!_ok || (uint32_t)off + len > _size) return false;
    if (!_f.seek(off)) return false;
    return _f.read(buf, len) == (int)len;
  }

  bool writeAt(uint32_t off, const uint8_t *buf, uint16_t len) override {
    if (!_ok || (uint32_t)off + len > _size) return false;
    if (!_f.seek(off)) return false;
    // No per-write flush: v1 is non-durable (a reboot abandons the spool), so flushing every
    // spilled entry only adds SPIFFS latency to the hot path — which throttles loop() during a
    // burst and can cause real RX packets to be dropped at capture. Reads use the same File
    // handle, so they observe these writes without a flush.
    return _f.write(buf, len) == (size_t)len;
  }

  uint32_t capacity() const override { return _size; }
};
