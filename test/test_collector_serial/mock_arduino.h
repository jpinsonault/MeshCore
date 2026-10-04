#pragma once

#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <vector>

// Controllable millis() for tests
static unsigned long mock_millis_value = 0;
inline unsigned long millis() { return mock_millis_value; }

// Minimal Stream base class matching Arduino's interface
class Stream {
public:
  virtual ~Stream() {}
  virtual int available() = 0;
  virtual int read() = 0;
  virtual int peek() = 0;
  virtual size_t write(uint8_t b) = 0;
  virtual size_t write(const uint8_t *buf, size_t len) {
    for (size_t i = 0; i < len; i++) write(buf[i]);
    return len;
  }
  virtual int availableForWrite() { return 1024; }
};

// Concrete mock: records writes, feeds pre-loaded reads
class MockStream : public Stream {
public:
  std::vector<uint8_t> written;   // all bytes written by device
  std::vector<uint8_t> to_read;   // bytes to feed on read()
  size_t read_pos = 0;
  int write_space = 1024;         // availableForWrite() return value

  int available() override {
    return (int)(to_read.size() - read_pos);
  }

  int read() override {
    if (read_pos >= to_read.size()) return -1;
    return to_read[read_pos++];
  }

  int peek() override {
    if (read_pos >= to_read.size()) return -1;
    return to_read[read_pos];
  }

  size_t write(uint8_t b) override {
    written.push_back(b);
    return 1;
  }

  size_t write(const uint8_t *buf, size_t len) override {
    for (size_t i = 0; i < len; i++) written.push_back(buf[i]);
    return len;
  }

  int availableForWrite() override { return write_space; }

  void clear() {
    written.clear();
    to_read.clear();
    read_pos = 0;
    write_space = 1024;
  }
};
