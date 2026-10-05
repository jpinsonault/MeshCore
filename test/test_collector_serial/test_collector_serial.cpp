// C++ unit tests for CollectorSerial ring buffer (runs on host via PlatformIO native)
//
// The test directory contains Arduino.h and MeshCore.h shims that redirect
// to our mocks. PlatformIO adds the test dir to the include path, so these
// get picked up before the real headers.
//
// COLLECTOR_RING_SIZE=256 is set via build_flags in platformio.ini.

#include "CollectorSerial.h"
#include "CollectorSpool.h"
#include <unity.h>

// --- Helpers ---

static MockStream ms;

static void setup_cs(CollectorSerial &cs) {
  ms.clear();
  mock_millis_value = 0;
  cs = CollectorSerial();
  cs.begin(ms);
}

// Recompute CRC-16-CCITT on arbitrary data (same algorithm as CollectorSerial)
static uint16_t test_crc16(const uint8_t *data, uint16_t len) {
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

// Build a host->device frame (for processIncoming tests)
static std::vector<uint8_t> build_host_frame(uint8_t type, uint32_t seq, uint32_t payload_val) {
  // frame: [0xC0][len_lo][len_hi][type][seq(4)][payload(4)][crc(2)]
  // frame_len = 1(type) + 4(seq) + 4(payload) + 2(crc) = 11
  uint8_t body[9]; // type + seq + payload
  body[0] = type;
  memcpy(&body[1], &seq, 4);
  memcpy(&body[5], &payload_val, 4);
  uint16_t crc = test_crc16(body, 9);

  std::vector<uint8_t> frame;
  frame.push_back(0xC0);
  uint16_t frame_len = 11;
  frame.push_back(frame_len & 0xFF);
  frame.push_back(frame_len >> 8);
  for (int i = 0; i < 9; i++) frame.push_back(body[i]);
  frame.push_back(crc & 0xFF);
  frame.push_back(crc >> 8);
  return frame;
}

// Parse one v2 wire frame from a byte vector starting at offset.
struct ParsedFrame {
  uint8_t type;
  uint32_t seq;
  std::vector<uint8_t> payload;
  uint16_t wire_crc;
  bool valid_crc;
};

static bool parse_frame(const std::vector<uint8_t> &data, size_t &offset, ParsedFrame &out) {
  if (offset + 3 > data.size()) return false;
  if (data[offset] != 0xC0) return false;
  uint16_t frame_len = data[offset + 1] | ((uint16_t)data[offset + 2] << 8);
  if (offset + 3 + frame_len > data.size()) return false;

  const uint8_t *frame_data = &data[offset + 3];
  out.type = frame_data[0];
  memcpy(&out.seq, &frame_data[1], 4);

  uint16_t crc_data_len = frame_len - 2;
  out.payload.assign(frame_data + 5, frame_data + crc_data_len);
  out.wire_crc = frame_data[frame_len - 2] | ((uint16_t)frame_data[frame_len - 1] << 8);
  out.valid_crc = (test_crc16(frame_data, crc_data_len) == out.wire_crc);

  offset += 3 + frame_len;
  return true;
}

// ============================================================
// CRC-16-CCITT tests
// ============================================================

void test_crc_known_vector() {
  const uint8_t data[] = "123456789";
  uint16_t crc = test_crc16(data, 9);
  TEST_ASSERT_EQUAL_HEX16(0x29B1, crc);
}

void test_crc_empty() {
  uint16_t crc = test_crc16(nullptr, 0);
  TEST_ASSERT_EQUAL_HEX16(0xFFFF, crc);
}

void test_crc_single_byte() {
  uint8_t b = 0x42;
  uint16_t crc = test_crc16(&b, 1);
  TEST_ASSERT_NOT_EQUAL(0xFFFF, crc);
  TEST_ASSERT_TRUE(crc <= 0xFFFF);
}

void test_crc_bit_sensitivity() {
  uint8_t a[] = {0x00, 0x01, 0x02};
  uint8_t b[] = {0x00, 0x01, 0x03};  // flip one bit
  TEST_ASSERT_NOT_EQUAL(test_crc16(a, 3), test_crc16(b, 3));
}

// ============================================================
// Ring buffer — basic write/read
// ============================================================

void test_write_one_entry() {
  CollectorSerial cs;
  setup_cs(cs);
  uint8_t payload[] = {0xAA, 0xBB};
  cs.ringWrite(COLLECTOR_HEARTBEAT, payload, 2);

  TEST_ASSERT_EQUAL_UINT32(1, cs.getTotalEntries());
  TEST_ASSERT_EQUAL_UINT32(1, cs.getNewestSeq());
  TEST_ASSERT_EQUAL_UINT32(1, cs.getOldestSeq());
  TEST_ASSERT_TRUE(cs.hasBacklog());
}

void test_write_multiple_increments_seq() {
  CollectorSerial cs;
  setup_cs(cs);
  uint8_t p[] = {0x01};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 1);
  cs.ringWrite(COLLECTOR_TX_RAW, p, 1);
  cs.ringWrite(COLLECTOR_HEARTBEAT, p, 1);

  TEST_ASSERT_EQUAL_UINT32(3, cs.getTotalEntries());
  TEST_ASSERT_EQUAL_UINT32(1, cs.getOldestSeq());
  TEST_ASSERT_EQUAL_UINT32(3, cs.getNewestSeq());
}

void test_write_zero_length_payload() {
  CollectorSerial cs;
  setup_cs(cs);
  cs.ringWrite(COLLECTOR_HEARTBEAT, nullptr, 0);

  TEST_ASSERT_EQUAL_UINT32(1, cs.getTotalEntries());
  TEST_ASSERT_TRUE(cs.hasBacklog());
}

void test_drain_all_clears_backlog() {
  CollectorSerial cs;
  setup_cs(cs);
  uint8_t p[] = {0x01};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 1);
  cs.ringWrite(COLLECTOR_TX_RAW, p, 1);

  TEST_ASSERT_TRUE(cs.drain());
  TEST_ASSERT_TRUE(cs.drain());
  TEST_ASSERT_FALSE(cs.drain());
  TEST_ASSERT_FALSE(cs.hasBacklog());
}

void test_oldest_newest_seq() {
  CollectorSerial cs;
  setup_cs(cs);
  TEST_ASSERT_EQUAL_UINT32(0, cs.getNewestSeq());

  uint8_t p[] = {0x01};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 1);
  cs.ringWrite(COLLECTOR_TX_RAW, p, 1);
  cs.ringWrite(COLLECTOR_HEARTBEAT, p, 1);

  TEST_ASSERT_EQUAL_UINT32(1, cs.getOldestSeq());
  TEST_ASSERT_EQUAL_UINT32(3, cs.getNewestSeq());
}

// ============================================================
// Ring buffer — wrapping and sentinels
// ============================================================

void test_sentinel_written_at_wrap() {
  CollectorSerial cs;
  setup_cs(cs);

  // entry_len = 7 + 20 = 27 bytes. 256 / 27 = 9.48 entries before sentinel.
  uint8_t p[20];
  memset(p, 0xAA, sizeof(p));
  for (int i = 0; i < 10; i++) {
    cs.ringWrite(COLLECTOR_HEARTBEAT, p, 20);
  }
  TEST_ASSERT_EQUAL_UINT32(10, cs.getNewestSeq());
  TEST_ASSERT_TRUE(cs.getTotalEntries() > 0);
}

void test_entries_after_wrap_are_valid() {
  CollectorSerial cs;
  setup_cs(cs);

  // Phase 1: fill 9 entries (243 bytes), drain and ACK to advance head
  uint8_t p[20];
  memset(p, 0xBB, sizeof(p));
  for (int i = 0; i < 9; i++) cs.ringWrite(COLLECTOR_HEARTBEAT, p, 20);
  while (cs.drain()) {}
  cs.handleAck(9);

  // Phase 2: write entries that trigger sentinel at 243 and wrap to 0
  ms.written.clear();
  for (int i = 0; i < 5; i++) cs.ringWrite(COLLECTOR_HEARTBEAT, p, 20);

  // Drain wrapped entries and verify CRC
  int count = 0;
  while (cs.drain()) count++;
  TEST_ASSERT_EQUAL(5, count);

  size_t off = 0;
  while (off < ms.written.size()) {
    ParsedFrame pf;
    TEST_ASSERT_TRUE(parse_frame(ms.written, off, pf));
    TEST_ASSERT_TRUE(pf.valid_crc);
    count--;
  }
  TEST_ASSERT_EQUAL(0, count);
}

void test_drain_skips_sentinel() {
  CollectorSerial cs;
  setup_cs(cs);

  // Phase 1: fill and ACK to advance head near end of buffer
  uint8_t p[20];
  memset(p, 0xCC, sizeof(p));
  for (int i = 0; i < 9; i++) cs.ringWrite(COLLECTOR_HEARTBEAT, p, 20);
  while (cs.drain()) {}
  cs.handleAck(9);

  // Phase 2: write entries that cross sentinel boundary
  ms.written.clear();
  for (int i = 0; i < 7; i++) cs.ringWrite(COLLECTOR_HEARTBEAT, p, 20);

  int drained = 0;
  while (cs.drain()) drained++;
  TEST_ASSERT_EQUAL(7, drained);
  TEST_ASSERT_FALSE(cs.hasBacklog());
}

void test_handle_ack_skips_sentinel() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[20];
  memset(p, 0xDD, sizeof(p));
  for (int i = 0; i < 15; i++) {
    cs.ringWrite(COLLECTOR_HEARTBEAT, p, 20);
  }

  cs.handleAck(cs.getNewestSeq());
  TEST_ASSERT_EQUAL_UINT32(0, cs.getTotalEntries());
}

void test_handle_resume_skips_sentinel() {
  CollectorSerial cs;
  setup_cs(cs);

  // Phase 1: fill and ACK to advance head near end
  uint8_t p[20];
  memset(p, 0xEE, sizeof(p));
  for (int i = 0; i < 9; i++) cs.ringWrite(COLLECTOR_HEARTBEAT, p, 20);
  while (cs.drain()) {}
  cs.handleAck(9);

  // Phase 2: write entries that span sentinel boundary
  ms.written.clear();
  for (int i = 0; i < 5; i++) cs.ringWrite(COLLECTOR_HEARTBEAT, p, 20);
  while (cs.drain()) {}

  // RESUME from beginning — should replay across sentinel
  cs.handleResume(0);
  TEST_ASSERT_TRUE(cs.hasBacklog());

  ms.written.clear();
  int replayed = 0;
  while (cs.drain()) replayed++;
  TEST_ASSERT_EQUAL(5, replayed);
}

// ============================================================
// Ring buffer — overflow and eviction
// ============================================================

void test_overflow_evicts_oldest() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[60];
  memset(p, 0x11, sizeof(p));
  // entry_len = 67 bytes. 256/67 ≈ 3.8 → 4th entry triggers eviction
  for (int i = 0; i < 6; i++) {
    cs.ringWrite(COLLECTOR_HEARTBEAT, p, 60);
  }

  TEST_ASSERT_TRUE(cs.getDroppedCount() > 0);
  TEST_ASSERT_EQUAL_UINT32(6, cs.getNewestSeq());
  TEST_ASSERT_TRUE(cs.getOldestSeq() > 1);
}

void test_send_cursor_advances_on_eviction() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[60];
  memset(p, 0x22, sizeof(p));

  // Write 4 entries: s4 triggers sentinel + wrap + eviction of s1
  cs.ringWrite(COLLECTOR_HEARTBEAT, p, 60);
  cs.ringWrite(COLLECTOR_HEARTBEAT, p, 60);
  cs.ringWrite(COLLECTOR_HEARTBEAT, p, 60);
  cs.ringWrite(COLLECTOR_HEARTBEAT, p, 60);

  TEST_ASSERT_TRUE(cs.getDroppedCount() > 0);
  uint32_t oldest = cs.getOldestSeq();
  TEST_ASSERT_TRUE(oldest > 1);

  // ACK oldest to free space, then RESUME to drain surviving entries
  cs.handleAck(oldest);
  cs.handleResume(0);

  ms.written.clear();
  int drained = 0;
  while (cs.drain()) drained++;
  TEST_ASSERT_TRUE(drained > 0);

  // Verify evicted entry (s1) never appears in drained output
  size_t off = 0;
  while (off < ms.written.size()) {
    ParsedFrame pf;
    TEST_ASSERT_TRUE(parse_frame(ms.written, off, pf));
    TEST_ASSERT_TRUE(pf.seq > 1);
  }
}

void test_multiple_overflows_no_corruption() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[40];
  for (int i = 0; i < 50; i++) {
    memset(p, (uint8_t)i, sizeof(p));
    cs.ringWrite(COLLECTOR_HEARTBEAT, p, 40);
  }

  TEST_ASSERT_EQUAL_UINT32(50, cs.getNewestSeq());
  TEST_ASSERT_TRUE(cs.getTotalEntries() > 0);

  // Use RESUME to reset cursor, then drain and verify CRC
  cs.handleResume(0);
  while (cs.drain()) {}
  size_t off = 0;
  while (off < ms.written.size()) {
    ParsedFrame pf;
    TEST_ASSERT_TRUE(parse_frame(ms.written, off, pf));
    TEST_ASSERT_TRUE(pf.valid_crc);
  }
}

void test_newest_entries_drain_after_overflow() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[60];
  memset(p, 0x33, sizeof(p));
  for (int i = 0; i < 8; i++) {
    cs.ringWrite(COLLECTOR_HEARTBEAT, p, 60);
  }

  TEST_ASSERT_EQUAL_UINT32(8, cs.getNewestSeq());

  // RESUME and drain all surviving entries
  cs.handleResume(0);
  int drained = 0;
  while (cs.drain()) drained++;
  TEST_ASSERT_TRUE(drained > 0);

  // Verify last drained frame has seq == 8
  size_t off = 0;
  ParsedFrame last;
  while (off < ms.written.size()) {
    parse_frame(ms.written, off, last);
  }
  TEST_ASSERT_EQUAL_UINT32(8, last.seq);
}

void test_large_entry_multiple_evictions() {
  CollectorSerial cs;
  setup_cs(cs);

  // First fill with small entries
  uint8_t small[10];
  memset(small, 0x44, sizeof(small));
  for (int i = 0; i < 10; i++) {
    cs.ringWrite(COLLECTOR_HEARTBEAT, small, 10);
  }
  uint32_t entries_before = cs.getTotalEntries();
  uint32_t drops_before = cs.getDroppedCount();

  // Now write one huge entry that forces evicting multiple small ones
  uint8_t big[200];
  memset(big, 0x55, sizeof(big));
  cs.ringWrite(COLLECTOR_RX_RAW, big, 200);

  TEST_ASSERT_TRUE(cs.getDroppedCount() > drops_before);
  TEST_ASSERT_TRUE(cs.getTotalEntries() < entries_before);
  TEST_ASSERT_EQUAL_UINT32(11, cs.getNewestSeq());
}

// ============================================================
// drain() — wire format
// ============================================================

void test_drain_wire_format() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0xDE, 0xAD, 0xBE, 0xEF};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 4);
  cs.drain();

  // Expected: [0xC0][len_lo][len_hi][type][seq(4)][payload(4)][crc(2)]
  // entry_len = 7 + 4 = 11, wire_len = 11
  // total output: 3 (header) + 11 (frame) = 14
  TEST_ASSERT_EQUAL(14, (int)ms.written.size());
  TEST_ASSERT_EQUAL_HEX8(0xC0, ms.written[0]);
  TEST_ASSERT_EQUAL_HEX8(11, ms.written[1]);
  TEST_ASSERT_EQUAL_HEX8(0, ms.written[2]);
  TEST_ASSERT_EQUAL_HEX8(COLLECTOR_RX_RAW, ms.written[3]);

  uint32_t seq;
  memcpy(&seq, &ms.written[4], 4);
  TEST_ASSERT_EQUAL_UINT32(1, seq);

  TEST_ASSERT_EQUAL_HEX8(0xDE, ms.written[8]);
  TEST_ASSERT_EQUAL_HEX8(0xAD, ms.written[9]);
  TEST_ASSERT_EQUAL_HEX8(0xBE, ms.written[10]);
  TEST_ASSERT_EQUAL_HEX8(0xEF, ms.written[11]);
}

void test_drain_crc_valid() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01, 0x02, 0x03};
  cs.ringWrite(COLLECTOR_TX_RAW, p, 3);
  cs.drain();

  size_t off = 0;
  ParsedFrame pf;
  TEST_ASSERT_TRUE(parse_frame(ms.written, off, pf));
  TEST_ASSERT_TRUE(pf.valid_crc);
}

// drain() deliberately ignores availableForWrite(): on ESP32-S3 it returns 0 when no
// UART TX ring buffer is configured, which left every frame stuck in the ring (d60bae11).
void test_drain_ignores_available_for_write() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 1);

  ms.write_space = 0;
  TEST_ASSERT_TRUE(cs.drain());
  TEST_ASSERT_FALSE(cs.hasBacklog());
}

void test_drain_empty_returns_false() {
  CollectorSerial cs;
  setup_cs(cs);
  TEST_ASSERT_FALSE(cs.drain());
}

void test_drain_fifo_order() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p1[] = {0x11};
  uint8_t p2[] = {0x22};
  uint8_t p3[] = {0x33};
  cs.ringWrite(COLLECTOR_RX_RAW, p1, 1);
  cs.ringWrite(COLLECTOR_TX_RAW, p2, 1);
  cs.ringWrite(COLLECTOR_HEARTBEAT, p3, 1);

  while (cs.drain()) {}

  size_t off = 0;
  uint32_t prev_seq = 0;
  int count = 0;
  while (off < ms.written.size()) {
    ParsedFrame pf;
    TEST_ASSERT_TRUE(parse_frame(ms.written, off, pf));
    TEST_ASSERT_TRUE(pf.seq > prev_seq);
    prev_seq = pf.seq;
    count++;
  }
  TEST_ASSERT_EQUAL(3, count);
}

// ============================================================
// handleAck()
// ============================================================

void test_ack_reclaims_entries() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 1);
  cs.ringWrite(COLLECTOR_TX_RAW, p, 1);
  cs.ringWrite(COLLECTOR_HEARTBEAT, p, 1);

  cs.handleAck(2);
  TEST_ASSERT_EQUAL_UINT32(1, cs.getTotalEntries());
  TEST_ASSERT_EQUAL_UINT32(3, cs.getOldestSeq());
}

void test_ack_entries_above_survive() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  for (int i = 0; i < 5; i++) cs.ringWrite(COLLECTOR_RX_RAW, p, 1);

  cs.handleAck(3);
  TEST_ASSERT_EQUAL_UINT32(2, cs.getTotalEntries());
  TEST_ASSERT_EQUAL_UINT32(4, cs.getOldestSeq());
}

void test_ack_higher_than_all_empties() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 1);
  cs.ringWrite(COLLECTOR_TX_RAW, p, 1);

  cs.handleAck(100);
  TEST_ASSERT_EQUAL_UINT32(0, cs.getTotalEntries());
}

void test_ack_zero_no_reclaim() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 1);

  cs.handleAck(0);
  TEST_ASSERT_EQUAL_UINT32(1, cs.getTotalEntries());
}

void test_ack_across_sentinel() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[20];
  memset(p, 0xAA, sizeof(p));
  for (int i = 0; i < 15; i++) {
    cs.ringWrite(COLLECTOR_HEARTBEAT, p, 20);
  }

  uint32_t oldest = cs.getOldestSeq();
  uint32_t newest = cs.getNewestSeq();
  uint32_t mid = oldest + (newest - oldest) / 2;
  cs.handleAck(mid);
  TEST_ASSERT_TRUE(cs.getOldestSeq() > mid);
}

void test_ack_idempotent() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 1);
  cs.ringWrite(COLLECTOR_TX_RAW, p, 1);
  cs.ringWrite(COLLECTOR_HEARTBEAT, p, 1);

  cs.handleAck(2);
  uint32_t entries_after_first = cs.getTotalEntries();
  cs.handleAck(2);
  TEST_ASSERT_EQUAL_UINT32(entries_after_first, cs.getTotalEntries());
}

// ============================================================
// handleResume()
// ============================================================

void test_resume_zero_replays_all() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 1);
  cs.ringWrite(COLLECTOR_TX_RAW, p, 1);
  cs.ringWrite(COLLECTOR_HEARTBEAT, p, 1);

  while (cs.drain()) {}
  TEST_ASSERT_FALSE(cs.hasBacklog());

  cs.handleResume(0);
  TEST_ASSERT_TRUE(cs.hasBacklog());

  int replayed = 0;
  while (cs.drain()) replayed++;
  TEST_ASSERT_EQUAL(3, replayed);
}

void test_resume_n_starts_after_n() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  for (int i = 0; i < 5; i++) cs.ringWrite(COLLECTOR_RX_RAW, p, 1);

  while (cs.drain()) {}
  ms.written.clear();

  cs.handleResume(3);  // replay seq 4, 5
  int replayed = 0;
  while (cs.drain()) replayed++;
  TEST_ASSERT_EQUAL(2, replayed);

  size_t off = 0;
  ParsedFrame pf;
  TEST_ASSERT_TRUE(parse_frame(ms.written, off, pf));
  TEST_ASSERT_EQUAL_UINT32(4, pf.seq);
}

void test_resume_past_newest_nothing_to_replay() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 1);
  cs.ringWrite(COLLECTOR_TX_RAW, p, 1);

  while (cs.drain()) {}

  cs.handleResume(100);
  TEST_ASSERT_FALSE(cs.hasBacklog());
}

void test_resume_after_ack() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  for (int i = 0; i < 5; i++) cs.ringWrite(COLLECTOR_RX_RAW, p, 1);

  while (cs.drain()) {}
  cs.handleAck(3);

  cs.handleResume(0);
  ms.written.clear();

  int replayed = 0;
  while (cs.drain()) replayed++;
  TEST_ASSERT_EQUAL(2, replayed);

  size_t off = 0;
  ParsedFrame pf;
  TEST_ASSERT_TRUE(parse_frame(ms.written, off, pf));
  TEST_ASSERT_EQUAL_UINT32(4, pf.seq);
}

void test_resume_across_sentinel() {
  CollectorSerial cs;
  setup_cs(cs);

  // Phase 1: fill and ACK to advance head near end
  uint8_t p[20];
  memset(p, 0xAA, sizeof(p));
  for (int i = 0; i < 9; i++) cs.ringWrite(COLLECTOR_HEARTBEAT, p, 20);
  while (cs.drain()) {}
  cs.handleAck(9);

  // Phase 2: write entries that span sentinel boundary
  ms.written.clear();
  for (int i = 0; i < 5; i++) cs.ringWrite(COLLECTOR_HEARTBEAT, p, 20);
  while (cs.drain()) {}
  TEST_ASSERT_FALSE(cs.hasBacklog());

  uint32_t oldest = cs.getOldestSeq();
  cs.handleResume(oldest + 1);
  TEST_ASSERT_TRUE(cs.hasBacklog());

  ms.written.clear();
  int replayed = 0;
  while (cs.drain()) replayed++;
  TEST_ASSERT_EQUAL(3, replayed);
}

// ============================================================
// processIncoming()
// ============================================================

void test_process_incoming_ack() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  for (int i = 0; i < 5; i++) cs.ringWrite(COLLECTOR_RX_RAW, p, 1);

  auto frame = build_host_frame(HOST_ACK, 0, 3);
  ms.to_read = frame;
  ms.read_pos = 0;

  cs.processIncoming(ms);
  TEST_ASSERT_EQUAL_UINT32(2, cs.getTotalEntries());
}

void test_process_incoming_resume() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  for (int i = 0; i < 5; i++) cs.ringWrite(COLLECTOR_RX_RAW, p, 1);

  while (cs.drain()) {}
  TEST_ASSERT_FALSE(cs.hasBacklog());

  auto frame = build_host_frame(HOST_RESUME, 0, 3);
  ms.to_read = frame;
  ms.read_pos = 0;

  cs.processIncoming(ms);
  TEST_ASSERT_TRUE(cs.hasBacklog());
}

void test_process_incoming_bad_crc() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  for (int i = 0; i < 5; i++) cs.ringWrite(COLLECTOR_RX_RAW, p, 1);

  auto frame = build_host_frame(HOST_ACK, 0, 3);
  frame.back() ^= 0xFF;
  ms.to_read = frame;
  ms.read_pos = 0;

  cs.processIncoming(ms);
  TEST_ASSERT_EQUAL_UINT32(5, cs.getTotalEntries());
}

void test_process_incoming_truncated() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 1);

  ms.to_read = {0xC0, 0x0B};
  ms.read_pos = 0;
  // Set millis so deadline wraps via unsigned overflow — all millis() < deadline checks fail
  mock_millis_value = (unsigned long)-1 - 10;

  cs.processIncoming(ms);
  TEST_ASSERT_EQUAL_UINT32(1, cs.getTotalEntries());
  mock_millis_value = 0;
}

void test_process_incoming_wrong_start_byte() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 1);

  ms.to_read = {0x00, 0x0B, 0x00};
  ms.read_pos = 0;

  cs.processIncoming(ms);
  TEST_ASSERT_EQUAL_UINT32(1, cs.getTotalEntries());
}

// ============================================================
// sendHandshake()
// ============================================================

void test_handshake_v2_format() {
  CollectorSerial cs;
  setup_cs(cs);

  cs.sendHandshake();

  TEST_ASSERT_TRUE(ms.written.size() >= 22);
  TEST_ASSERT_EQUAL_HEX8(0xC0, ms.written[0]);
  TEST_ASSERT_EQUAL_HEX8(COLLECTOR_HANDSHAKE, ms.written[3]);
  TEST_ASSERT_EQUAL(0, memcmp(&ms.written[4], "COLLECTOR", 9));
  TEST_ASSERT_EQUAL(COLLECTOR_PROTOCOL_VER, ms.written[13]);
}

void test_handshake_seq_range() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 1);
  cs.ringWrite(COLLECTOR_TX_RAW, p, 1);
  cs.ringWrite(COLLECTOR_HEARTBEAT, p, 1);

  cs.sendHandshake();

  uint32_t oldest, newest;
  memcpy(&oldest, &ms.written[14], 4);
  memcpy(&newest, &ms.written[18], 4);
  TEST_ASSERT_EQUAL_UINT32(1, oldest);
  TEST_ASSERT_EQUAL_UINT32(3, newest);
}

void test_handshake_empty_shows_zero_seq() {
  CollectorSerial cs;
  setup_cs(cs);

  cs.sendHandshake();

  uint32_t oldest, newest;
  memcpy(&oldest, &ms.written[14], 4);
  memcpy(&newest, &ms.written[18], 4);
  TEST_ASSERT_EQUAL_UINT32(0, oldest);
  TEST_ASSERT_EQUAL_UINT32(0, newest);
}

// ============================================================
// Integration: write → drain → verify
// ============================================================

void test_integration_write_drain_verify() {
  CollectorSerial cs;
  setup_cs(cs);

  for (int i = 0; i < 5; i++) {
    uint8_t p[] = {(uint8_t)(i * 10), (uint8_t)(i * 10 + 1)};
    cs.ringWrite(COLLECTOR_RX_RAW, p, 2);
  }

  while (cs.drain()) {}

  size_t off = 0;
  uint32_t expected_seq = 1;
  while (off < ms.written.size()) {
    ParsedFrame pf;
    TEST_ASSERT_TRUE(parse_frame(ms.written, off, pf));
    TEST_ASSERT_TRUE(pf.valid_crc);
    TEST_ASSERT_EQUAL_UINT32(expected_seq, pf.seq);
    TEST_ASSERT_EQUAL_HEX8(COLLECTOR_RX_RAW, pf.type);
    TEST_ASSERT_EQUAL_HEX8((expected_seq - 1) * 10, pf.payload[0]);
    expected_seq++;
  }
  TEST_ASSERT_EQUAL_UINT32(6, expected_seq);
}

void test_integration_partial_drain_ack_continue() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[] = {0x01};
  for (int i = 0; i < 5; i++) cs.ringWrite(COLLECTOR_RX_RAW, p, 1);

  cs.drain();
  cs.drain();
  cs.drain();

  cs.handleAck(3);
  TEST_ASSERT_EQUAL_UINT32(2, cs.getTotalEntries());

  cs.ringWrite(COLLECTOR_TX_RAW, p, 1);
  cs.ringWrite(COLLECTOR_TX_RAW, p, 1);

  ms.written.clear();

  int drained = 0;
  while (cs.drain()) drained++;
  TEST_ASSERT_EQUAL(4, drained);

  size_t off = 0;
  uint32_t prev = 0;
  while (off < ms.written.size()) {
    ParsedFrame pf;
    TEST_ASSERT_TRUE(parse_frame(ms.written, off, pf));
    if (prev > 0) TEST_ASSERT_EQUAL_UINT32(prev + 1, pf.seq);
    prev = pf.seq;
  }
}

void test_integration_resume_replays_same() {
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p1[] = {0xAA};
  uint8_t p2[] = {0xBB};
  cs.ringWrite(COLLECTOR_RX_RAW, p1, 1);
  cs.ringWrite(COLLECTOR_TX_RAW, p2, 1);

  while (cs.drain()) {}
  std::vector<uint8_t> first_output = ms.written;

  cs.handleResume(0);
  ms.written.clear();
  while (cs.drain()) {}

  TEST_ASSERT_EQUAL(first_output.size(), ms.written.size());
  TEST_ASSERT_EQUAL(0, memcmp(first_output.data(), ms.written.data(), first_output.size()));
}

// ============================================================
// Bug fix: full-ring drain (unsent_entries counter)
// ============================================================

void test_full_ring_drain_works() {
  // Previously, when the ring was full (head==tail, entries>0),
  // drain() returned false because send_cursor==tail.
  // The _unsent_entries counter fixes this.
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[20];
  memset(p, 0xFF, sizeof(p));
  // Write 12 entries with 27-byte entries on 256-byte ring → overflow, ring full
  for (int i = 0; i < 12; i++) {
    cs.ringWrite(COLLECTOR_HEARTBEAT, p, 20);
  }

  // With the bug fix, hasBacklog should be true and drain should work
  TEST_ASSERT_TRUE(cs.hasBacklog());

  int drained = 0;
  while (cs.drain()) drained++;
  TEST_ASSERT_TRUE(drained > 0);
  TEST_ASSERT_FALSE(cs.hasBacklog());
}

void test_full_ring_resume_then_drain() {
  // After continuous overflow, RESUME(0) + drain should replay all surviving entries
  CollectorSerial cs;
  setup_cs(cs);

  uint8_t p[20];
  memset(p, 0xAA, sizeof(p));
  for (int i = 0; i < 15; i++) {
    cs.ringWrite(COLLECTOR_HEARTBEAT, p, 20);
  }

  // Drain whatever is available
  while (cs.drain()) {}

  // RESUME(0) should make all surviving entries available again
  cs.handleResume(0);
  TEST_ASSERT_TRUE(cs.hasBacklog());

  ms.written.clear();
  int replayed = 0;
  while (cs.drain()) replayed++;
  TEST_ASSERT_EQUAL((int)cs.getTotalEntries(), replayed);
}

void test_head_reset_after_total_eviction() {
  // Previously, when one large write evicted ALL entries, head was left
  // pointing inside the new entry. Now head is reset to tail.
  CollectorSerial cs;
  setup_cs(cs);

  // Fill with small entries
  uint8_t small[10];
  memset(small, 0x44, sizeof(small));
  for (int i = 0; i < 5; i++) cs.ringWrite(COLLECTOR_HEARTBEAT, small, 10);
  while (cs.drain()) {}

  // Write one huge entry that evicts everything
  uint8_t big[240];
  memset(big, 0x55, sizeof(big));
  cs.ringWrite(COLLECTOR_RX_RAW, big, 240);

  // Should be 1 entry, and drain should produce valid output
  TEST_ASSERT_EQUAL_UINT32(1, cs.getTotalEntries());
  TEST_ASSERT_EQUAL_UINT32(6, cs.getNewestSeq());
  TEST_ASSERT_EQUAL_UINT32(6, cs.getOldestSeq());

  ms.written.clear();
  TEST_ASSERT_TRUE(cs.drain());

  size_t off = 0;
  ParsedFrame pf;
  TEST_ASSERT_TRUE(parse_frame(ms.written, off, pf));
  TEST_ASSERT_TRUE(pf.valid_crc);
  TEST_ASSERT_EQUAL_UINT32(6, pf.seq);
}

// ============================================================
// Status-frame coalescing (HEARTBEAT/DIAGNOSTICS gated on backlog)
// ============================================================

static void send_dummy_heartbeat(CollectorSerial &cs) {
  cs.sendHeartbeat(1234, 4200, 10, 20, 30, 40, 5, 600);
}
static void send_dummy_diagnostics(CollectorSerial &cs) {
  cs.sendDiagnostics(25.5f, 80000, 60000, 300000, -120, -90, 40, 1000, 2000, 3, 0, 1, 2, 3, 100, 50);
}

// With a backlog present, status frames must NOT be buffered (no new entry, no seq consumed).
void test_status_skipped_when_backlog() {
  CollectorSerial cs;
  setup_cs(cs);
  uint8_t p[] = {0x01};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 1);   // creates a backlog
  TEST_ASSERT_TRUE(cs.hasBacklog());
  TEST_ASSERT_EQUAL_UINT32(1, cs.getTotalEntries());
  TEST_ASSERT_EQUAL_UINT32(1, cs.getNewestSeq());

  send_dummy_heartbeat(cs);
  send_dummy_diagnostics(cs);

  // Nothing added, no seq burned — real traffic is untouched.
  TEST_ASSERT_EQUAL_UINT32(1, cs.getTotalEntries());
  TEST_ASSERT_EQUAL_UINT32(1, cs.getNewestSeq());
}

// When caught up (no backlog), status frames are buffered normally with a real seq, and
// drain() emits a well-formed HEARTBEAT frame.
void test_status_written_when_caught_up() {
  CollectorSerial cs;
  setup_cs(cs);
  TEST_ASSERT_FALSE(cs.hasBacklog());

  send_dummy_heartbeat(cs);
  TEST_ASSERT_EQUAL_UINT32(1, cs.getTotalEntries());
  TEST_ASSERT_EQUAL_UINT32(1, cs.getNewestSeq());

  ms.written.clear();
  TEST_ASSERT_TRUE(cs.drain());
  size_t off = 0;
  ParsedFrame pf;
  TEST_ASSERT_TRUE(parse_frame(ms.written, off, pf));
  TEST_ASSERT_TRUE(pf.valid_crc);
  TEST_ASSERT_EQUAL_HEX8(COLLECTOR_HEARTBEAT, pf.type);
  TEST_ASSERT_EQUAL_UINT32(1, pf.seq);
  TEST_ASSERT_EQUAL_UINT32(27, (uint32_t)pf.payload.size());
}

// force=true (an explicit `collector status`/`diag`) must answer even under backlog.
void test_status_forced_when_backlog() {
  CollectorSerial cs;
  setup_cs(cs);
  uint8_t p[] = {0x01};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 1);   // backlog
  TEST_ASSERT_TRUE(cs.hasBacklog());
  TEST_ASSERT_EQUAL_UINT32(1, cs.getTotalEntries());

  cs.sendHeartbeat(1234, 4200, 10, 20, 30, 40, 5, 600, /*force=*/true);
  TEST_ASSERT_EQUAL_UINT32(2, cs.getTotalEntries());   // buffered despite backlog
  TEST_ASSERT_EQUAL_UINT32(2, cs.getNewestSeq());

  cs.sendDiagnostics(25.5f, 80000, 60000, 300000, -120, -90, 40, 1000, 2000, 3, 0, 1, 2, 3, 100, 50,
                     /*force=*/true);
  TEST_ASSERT_EQUAL_UINT32(3, cs.getTotalEntries());
}

// Status buffering resumes once the backlog drains, proving the gate is dynamic (not one-shot).
void test_status_resumes_after_drain() {
  CollectorSerial cs;
  setup_cs(cs);
  uint8_t p[] = {0x01};
  cs.ringWrite(COLLECTOR_RX_RAW, p, 1);   // backlog

  send_dummy_heartbeat(cs);                // skipped
  TEST_ASSERT_EQUAL_UINT32(1, cs.getTotalEntries());

  TEST_ASSERT_TRUE(cs.drain());            // RX drained -> caught up
  TEST_ASSERT_FALSE(cs.hasBacklog());

  send_dummy_diagnostics(cs);              // now buffered
  TEST_ASSERT_EQUAL_UINT32(2, cs.getTotalEntries());
  TEST_ASSERT_EQUAL_UINT32(2, cs.getNewestSeq());
}

// Uniform-size entries that align exactly to the wrap boundary used to leave _head resting on a
// sentinel, and the next ringWrite would overwrite a live entry (corruption / out-of-order drain).
void test_ring_uniform_wrap_no_corruption() {
  CollectorSerial cs;
  setup_cs(cs);
  const uint16_t plen = 20;   // entry = 27 bytes; ring = 256 -> repeated wraps + exact alignment
  uint8_t pay[plen];
  for (uint32_t s = 1; s <= 50; s++) {
    memset(pay, (uint8_t)s, plen);
    cs.ringWrite(COLLECTOR_RX_RAW, pay, plen);
  }
  ms.written.clear();
  while (cs.drain()) {}
  size_t off = 0;
  ParsedFrame pf;
  uint32_t prev = 0;
  int n = 0;
  while (off < ms.written.size()) {
    TEST_ASSERT_TRUE(parse_frame(ms.written, off, pf));
    TEST_ASSERT_TRUE(pf.valid_crc);
    TEST_ASSERT_TRUE(pf.seq > prev);                        // strictly increasing: no corruption
    TEST_ASSERT_EQUAL_HEX8((uint8_t)pf.seq, pf.payload[0]); // payload tagged with its own seq
    prev = pf.seq;
    n++;
  }
  TEST_ASSERT_EQUAL_UINT32(50, cs.getNewestSeq());
  TEST_ASSERT_EQUAL_UINT32(50, prev);   // newest entry always survives
  TEST_ASSERT_TRUE(n >= 2);
}

// ============================================================
// Flash spool (FlashSpool over an in-memory SpoolStore)
// ============================================================

struct MockSpoolStore : public SpoolStore {
  std::vector<uint8_t> buf;
  MockSpoolStore(uint32_t cap) { buf.assign(cap, 0); }
  bool readAt(uint32_t off, uint8_t *b, uint16_t len) override {
    if ((size_t)off + len > buf.size()) return false;
    memcpy(b, &buf[off], len); return true;
  }
  bool writeAt(uint32_t off, const uint8_t *b, uint16_t len) override {
    if ((size_t)off + len > buf.size()) return false;
    memcpy(&buf[off], b, len); return true;
  }
  uint32_t capacity() const override { return (uint32_t)buf.size(); }
};

// Build a ring-format entry: [len(2)][type(1)][seq(4)][payload(plen) filled with `fill`].
static std::vector<uint8_t> make_entry(uint8_t type, uint32_t seq, uint8_t fill, uint16_t plen) {
  uint16_t entry_len = 7 + plen;
  std::vector<uint8_t> e(entry_len);
  e[0] = entry_len & 0xFF; e[1] = entry_len >> 8;
  e[2] = type;
  memcpy(&e[3], &seq, 4);
  for (uint16_t i = 0; i < plen; i++) e[7 + i] = fill;
  return e;
}

static bool spool_append(FlashSpool &sp, uint8_t type, uint32_t seq, uint8_t fill, uint16_t plen) {
  std::vector<uint8_t> e = make_entry(type, seq, fill, plen);
  return sp.append(e.data(), (uint16_t)e.size(), seq);
}

// Drain one entry; returns its seq (or 0 if none) and sets *fill to its payload[0].
static uint32_t spool_drain_one(FlashSpool &sp, uint8_t *fill) {
  uint8_t buf[262];
  uint16_t len = 0;
  if (!sp.peekSend(buf, &len)) return 0;
  uint32_t seq; memcpy(&seq, &buf[3], 4);
  if (fill) *fill = (len > 7) ? buf[7] : 0;
  sp.advanceSent();
  return seq;
}

void test_spool_append_drain_fifo() {
  MockSpoolStore store(4096);
  FlashSpool sp; sp.begin(&store);
  TEST_ASSERT_TRUE(sp.valid());
  TEST_ASSERT_TRUE(spool_append(sp, COLLECTOR_RX_RAW, 1, 0xA1, 20));
  TEST_ASSERT_TRUE(spool_append(sp, COLLECTOR_RX_RAW, 2, 0xB2, 30));
  TEST_ASSERT_TRUE(spool_append(sp, COLLECTOR_RX_RAW, 3, 0xC3, 10));
  TEST_ASSERT_EQUAL_UINT32(3, sp.count());
  TEST_ASSERT_EQUAL_UINT32(3, sp.unsent());
  TEST_ASSERT_EQUAL_UINT32(1, sp.oldestSeq());
  TEST_ASSERT_EQUAL_UINT32(3, sp.newestSeq());

  uint8_t fill;
  TEST_ASSERT_EQUAL_UINT32(1, spool_drain_one(sp, &fill)); TEST_ASSERT_EQUAL_HEX8(0xA1, fill);
  TEST_ASSERT_EQUAL_UINT32(2, spool_drain_one(sp, &fill)); TEST_ASSERT_EQUAL_HEX8(0xB2, fill);
  TEST_ASSERT_EQUAL_UINT32(3, spool_drain_one(sp, &fill)); TEST_ASSERT_EQUAL_HEX8(0xC3, fill);
  TEST_ASSERT_FALSE(sp.hasUnsent());
  TEST_ASSERT_EQUAL_UINT32(3, sp.count());   // drained != acked
}

void test_spool_overflow_evicts_oldest() {
  MockSpoolStore store(64);          // fits two 27-byte entries, not three
  FlashSpool sp; sp.begin(&store);
  TEST_ASSERT_TRUE(spool_append(sp, COLLECTOR_RX_RAW, 1, 0x11, 20));
  TEST_ASSERT_TRUE(spool_append(sp, COLLECTOR_RX_RAW, 2, 0x22, 20));
  TEST_ASSERT_EQUAL_UINT32(2, sp.count());
  TEST_ASSERT_TRUE(spool_append(sp, COLLECTOR_RX_RAW, 3, 0x33, 20));   // evicts seq 1
  TEST_ASSERT_EQUAL_UINT32(2, sp.count());
  TEST_ASSERT_EQUAL_UINT32(1, sp.dropped());
  TEST_ASSERT_EQUAL_UINT32(2, sp.oldestSeq());

  uint8_t fill;
  TEST_ASSERT_EQUAL_UINT32(2, spool_drain_one(sp, &fill)); TEST_ASSERT_EQUAL_HEX8(0x22, fill);
  TEST_ASSERT_EQUAL_UINT32(3, spool_drain_one(sp, &fill)); TEST_ASSERT_EQUAL_HEX8(0x33, fill);
}

void test_spool_wraparound_preserves_data() {
  MockSpoolStore store(100);         // small: forces a sentinel wrap after a few 27B entries
  FlashSpool sp; sp.begin(&store);
  // Append 8 entries; capacity ~3 live at a time, so this wraps and evicts repeatedly.
  for (uint32_t s = 1; s <= 8; s++) TEST_ASSERT_TRUE(spool_append(sp, COLLECTOR_RX_RAW, s, (uint8_t)s, 20));
  // Whatever survived must drain in strictly increasing seq with matching payload.
  uint32_t prev = 0; uint8_t fill; uint32_t seq;
  int drained = 0;
  while ((seq = spool_drain_one(sp, &fill)) != 0) {
    TEST_ASSERT_TRUE(seq > prev);
    TEST_ASSERT_EQUAL_HEX8((uint8_t)seq, fill);   // payload tagged with its seq
    prev = seq; drained++;
  }
  TEST_ASSERT_EQUAL_UINT32(8, sp.newestSeq());
  TEST_ASSERT_TRUE(drained >= 2);
  TEST_ASSERT_EQUAL_UINT32(8, prev);              // newest always survives
}

void test_spool_ack_drops_committed() {
  MockSpoolStore store(4096);
  FlashSpool sp; sp.begin(&store);
  for (uint32_t s = 1; s <= 5; s++) spool_append(sp, COLLECTOR_RX_RAW, s, (uint8_t)s, 10);
  bool emptied = sp.handleAck(3);
  TEST_ASSERT_FALSE(emptied);
  TEST_ASSERT_EQUAL_UINT32(2, sp.count());
  TEST_ASSERT_EQUAL_UINT32(4, sp.oldestSeq());
}

void test_spool_ack_empties_returns_true() {
  MockSpoolStore store(4096);
  FlashSpool sp; sp.begin(&store);
  for (uint32_t s = 1; s <= 3; s++) spool_append(sp, COLLECTOR_RX_RAW, s, (uint8_t)s, 10);
  TEST_ASSERT_TRUE(sp.handleAck(3));
  TEST_ASSERT_EQUAL_UINT32(0, sp.count());
  TEST_ASSERT_FALSE(sp.hasUnsent());
}

void test_spool_resume_replays_from() {
  MockSpoolStore store(4096);
  FlashSpool sp; sp.begin(&store);
  for (uint32_t s = 1; s <= 5; s++) spool_append(sp, COLLECTOR_RX_RAW, s, (uint8_t)s, 10);
  while (spool_drain_one(sp, nullptr)) {}         // drain all
  TEST_ASSERT_FALSE(sp.hasUnsent());
  sp.handleResume(2);                             // replay seq 3,4,5
  TEST_ASSERT_EQUAL_UINT32(3, sp.unsent());
  uint8_t fill;
  TEST_ASSERT_EQUAL_UINT32(3, spool_drain_one(sp, &fill));
}

void test_spool_resume_zero_replays_all() {
  MockSpoolStore store(4096);
  FlashSpool sp; sp.begin(&store);
  for (uint32_t s = 1; s <= 3; s++) spool_append(sp, COLLECTOR_RX_RAW, s, (uint8_t)s, 10);
  while (spool_drain_one(sp, nullptr)) {}
  sp.handleResume(0);
  TEST_ASSERT_EQUAL_UINT32(3, sp.unsent());
  TEST_ASSERT_EQUAL_UINT32(1, spool_drain_one(sp, nullptr));
}

// ============================================================
// Spool durability (header persist + recover across "reboot")
// ============================================================

// Persisted header lets a fresh FlashSpool (= reboot) recover the spilled backlog + next_seq.
void test_spool_durable_recovers_after_reboot() {
  MockSpoolStore data(4096);
  MockSpoolStore hdr(64);
  FlashSpool sp;
  sp.begin(&data, &hdr);
  TEST_ASSERT_TRUE(sp.durable());
  for (uint32_t s = 1; s <= 5; s++) spool_append(sp, COLLECTOR_RX_RAW, s, (uint8_t)s, 10);
  sp.persistHeader(100);   // owner next_seq high-water = 100

  FlashSpool sp2;          // reboot: same backing stores, fresh index
  sp2.begin(&data, &hdr);
  TEST_ASSERT_EQUAL_UINT32(5, sp2.count());
  TEST_ASSERT_EQUAL_UINT32(5, sp2.newestSeq());
  TEST_ASSERT_EQUAL_UINT32(100, sp2.restoredNextSeq());
  uint8_t fill;
  for (uint32_t s = 1; s <= 5; s++) {
    TEST_ASSERT_EQUAL_UINT32(s, spool_drain_one(sp2, &fill));
    TEST_ASSERT_EQUAL_HEX8((uint8_t)s, fill);
  }
}

// A garbage/blank header is rejected (CRC/magic) -> spool starts empty, never reads stale bytes.
void test_spool_durable_bad_header_starts_empty() {
  MockSpoolStore data(4096);
  MockSpoolStore hdr(64);   // all zeros -> bad magic
  FlashSpool sp;
  sp.begin(&data, &hdr);
  TEST_ASSERT_EQUAL_UINT32(0, sp.count());
  TEST_ASSERT_FALSE(sp.hasUnsent());
}

// Without a header store, a reboot abandons the spool (non-durable v1 behavior).
void test_spool_non_durable_no_recovery() {
  MockSpoolStore data(4096);
  FlashSpool sp;
  sp.begin(&data);
  for (uint32_t s = 1; s <= 3; s++) spool_append(sp, COLLECTOR_RX_RAW, s, (uint8_t)s, 10);
  FlashSpool sp2;
  sp2.begin(&data);        // no hdr -> fresh
  TEST_ASSERT_EQUAL_UINT32(0, sp2.count());
}

// ============================================================
// Flood-duplicate collapse (RX_DUP)
// ============================================================

// Feed a synthetic received packet: [header][path_len][path...][payload...].
static void send_rx(CollectorSerial &cs, uint8_t header, const std::vector<uint8_t> &path,
                    const std::vector<uint8_t> &payload, float snr = 5, float rssi = -80) {
  std::vector<uint8_t> raw;
  raw.push_back(header);
  raw.push_back((uint8_t)path.size());
  for (auto b : path) raw.push_back(b);
  for (auto b : payload) raw.push_back(b);
  cs.sendRxRaw(snr, rssi, raw.data(), (int)raw.size());
}

// Same header+payload on a different path collapses to a compact RX_DUP carrying that path.
void test_dedup_collapses_flood_paths() {
  CollectorSerial cs;
  setup_cs(cs);
  cs.setDedup(true);
  std::vector<uint8_t> payload = {0x11, 0x22, 0x33, 0x44, 0x55};
  send_rx(cs, 0x12, {0xAA}, payload);         // first sighting: full RX_RAW
  send_rx(cs, 0x12, {0xBB, 0xCC}, payload);   // flood on another path: RX_DUP

  ms.written.clear();
  while (cs.drain()) {}
  size_t off = 0;
  ParsedFrame f1, f2;
  TEST_ASSERT_TRUE(parse_frame(ms.written, off, f1));
  TEST_ASSERT_EQUAL_HEX8(COLLECTOR_RX_RAW, f1.type);
  TEST_ASSERT_TRUE(parse_frame(ms.written, off, f2));
  TEST_ASSERT_EQUAL_HEX8(COLLECTOR_RX_DUP, f2.type);
  TEST_ASSERT_TRUE(f2.valid_crc);
  // RX_DUP payload: [snr][rssi][hash(4)][path_len][path...]
  TEST_ASSERT_EQUAL_UINT32(2, f2.payload[6]);
  TEST_ASSERT_EQUAL_HEX8(0xBB, f2.payload[7]);
  TEST_ASSERT_EQUAL_HEX8(0xCC, f2.payload[8]);
  TEST_ASSERT_TRUE(f2.payload.size() < f1.payload.size());   // compact
}

// Distinct payloads are never collapsed.
void test_dedup_distinct_not_collapsed() {
  CollectorSerial cs;
  setup_cs(cs);
  cs.setDedup(true);
  send_rx(cs, 0x12, {0xAA}, {0x01, 0x02, 0x03});
  send_rx(cs, 0x12, {0xAA}, {0x09, 0x08, 0x07});   // different payload, same path

  ms.written.clear();
  while (cs.drain()) {}
  size_t off = 0;
  ParsedFrame f1, f2;
  TEST_ASSERT_TRUE(parse_frame(ms.written, off, f1));
  TEST_ASSERT_TRUE(parse_frame(ms.written, off, f2));
  TEST_ASSERT_EQUAL_HEX8(COLLECTOR_RX_RAW, f1.type);
  TEST_ASSERT_EQUAL_HEX8(COLLECTOR_RX_RAW, f2.type);
}

// With dedup off, repeats pass through as full RX_RAW (no behavior change / no protocol surprise).
void test_dedup_disabled_passthrough() {
  CollectorSerial cs;
  setup_cs(cs);
  std::vector<uint8_t> payload = {0x11, 0x22, 0x33};
  send_rx(cs, 0x12, {0xAA}, payload);
  send_rx(cs, 0x12, {0xAA}, payload);   // identical, but dedup disabled

  ms.written.clear();
  while (cs.drain()) {}
  size_t off = 0;
  ParsedFrame f1, f2;
  TEST_ASSERT_TRUE(parse_frame(ms.written, off, f1));
  TEST_ASSERT_TRUE(parse_frame(ms.written, off, f2));
  TEST_ASSERT_EQUAL_HEX8(COLLECTOR_RX_RAW, f1.type);
  TEST_ASSERT_EQUAL_HEX8(COLLECTOR_RX_RAW, f2.type);
}

// ============================================================
// Two-tier integration: CollectorSerial + attached FlashSpool
// ============================================================

// Overflow the RAM ring with a spool attached: evicted entries spill to flash (not dropped),
// and draining yields EVERY entry in strict seq order across both tiers (the capacity win).
void test_integration_spill_and_drain_in_order() {
  CollectorSerial cs;
  setup_cs(cs);
  MockSpoolStore store(8192);
  cs.attachSpool(&store);
  TEST_ASSERT_TRUE(cs.spoolActive());

  const uint16_t plen = 20;         // 27-byte entries; ring is 256 -> most spill to flash
  const uint32_t N = 40;
  uint8_t pay[plen];
  for (uint32_t s = 1; s <= N; s++) {
    memset(pay, (uint8_t)s, plen);
    cs.ringWrite(COLLECTOR_RX_RAW, pay, plen);
  }
  TEST_ASSERT_TRUE(cs.getSpoolCount() > 0);          // entries were spilled, not dropped
  TEST_ASSERT_EQUAL_UINT32(0, cs.getDroppedCount()); // 8KB spool holds everything -> zero loss

  ms.written.clear();
  while (cs.drain()) {}
  size_t off = 0;
  ParsedFrame pf;
  uint32_t expect = 1;
  while (off < ms.written.size()) {
    TEST_ASSERT_TRUE(parse_frame(ms.written, off, pf));
    TEST_ASSERT_TRUE(pf.valid_crc);
    TEST_ASSERT_EQUAL_UINT32(expect, pf.seq);                 // strict 1..N order across tiers
    TEST_ASSERT_EQUAL_HEX8((uint8_t)expect, pf.payload[0]);
    expect++;
  }
  TEST_ASSERT_EQUAL_UINT32(N + 1, expect);                    // saw exactly 1..N, no gaps
}

// An ACK whose seq lands in the flash range reclaims flash entries; the rest drains in order.
void test_integration_ack_across_tiers() {
  CollectorSerial cs;
  setup_cs(cs);
  MockSpoolStore store(8192);
  cs.attachSpool(&store);
  uint8_t pay[20];
  for (uint32_t s = 1; s <= 40; s++) { memset(pay, (uint8_t)s, 20); cs.ringWrite(COLLECTOR_RX_RAW, pay, 20); }

  cs.handleAck(20);                 // commit seqs 1..20 (all in the flash tier)
  ms.written.clear();
  while (cs.drain()) {}
  size_t off = 0;
  ParsedFrame pf;
  uint32_t expect = 21;
  while (off < ms.written.size()) {
    TEST_ASSERT_TRUE(parse_frame(ms.written, off, pf));
    TEST_ASSERT_EQUAL_UINT32(expect, pf.seq);
    expect++;
  }
  TEST_ASSERT_EQUAL_UINT32(41, expect);   // 21..40 remained
}

// RESUME(0) after a full drain replays the entire two-tier backlog again, in order.
void test_integration_resume_replays_all() {
  CollectorSerial cs;
  setup_cs(cs);
  MockSpoolStore store(8192);
  cs.attachSpool(&store);
  uint8_t pay[20];
  for (uint32_t s = 1; s <= 40; s++) { memset(pay, (uint8_t)s, 20); cs.ringWrite(COLLECTOR_RX_RAW, pay, 20); }
  while (cs.drain()) {}
  TEST_ASSERT_FALSE(cs.hasBacklog());

  cs.handleResume(0);
  TEST_ASSERT_TRUE(cs.hasBacklog());
  ms.written.clear();
  while (cs.drain()) {}
  size_t off = 0;
  ParsedFrame pf;
  uint32_t expect = 1;
  while (off < ms.written.size()) {
    TEST_ASSERT_TRUE(parse_frame(ms.written, off, pf));
    TEST_ASSERT_EQUAL_UINT32(expect, pf.seq);
    expect++;
  }
  TEST_ASSERT_EQUAL_UINT32(41, expect);
}

// When BOTH tiers overflow, the oldest is truly dropped (counted), the newest always survives,
// and whatever remains still drains contiguously (no corruption at the true-drop boundary).
void test_integration_both_tiers_full_drops_oldest() {
  CollectorSerial cs;
  setup_cs(cs);
  MockSpoolStore store(256);        // tiny spool AND tiny ring (256) -> both overflow
  cs.attachSpool(&store);
  uint8_t pay[20];
  const uint32_t N = 60;
  for (uint32_t s = 1; s <= N; s++) { memset(pay, (uint8_t)s, 20); cs.ringWrite(COLLECTOR_RX_RAW, pay, 20); }

  TEST_ASSERT_TRUE(cs.getDroppedCount() > 0);   // both tiers full -> real drops occurred

  ms.written.clear();
  while (cs.drain()) {}
  size_t off = 0;
  ParsedFrame pf;
  uint32_t prev = 0, first = 0;
  while (off < ms.written.size()) {
    TEST_ASSERT_TRUE(parse_frame(ms.written, off, pf));
    TEST_ASSERT_TRUE(pf.valid_crc);
    if (first == 0) first = pf.seq;
    if (prev != 0) TEST_ASSERT_EQUAL_UINT32(prev + 1, pf.seq);   // contiguous survivors
    prev = pf.seq;
  }
  TEST_ASSERT_EQUAL_UINT32(N, prev);     // newest always survives
  TEST_ASSERT_TRUE(first > 1);           // oldest was dropped (both tiers overflowed)
}

// Durable spool across a CollectorSerial "reboot": spilled entries survive and _next_seq resumes
// above the high-water mark so a post-reboot write never reuses a seq.
void test_integration_durable_reboot_resumes() {
  MockSpoolStore data(8192);
  MockSpoolStore hdr(64);

  CollectorSerial cs;
  setup_cs(cs);
  cs.attachSpool(&data, &hdr);
  uint8_t pay[20];
  for (uint32_t s = 1; s <= 40; s++) { memset(pay, (uint8_t)s, 20); cs.ringWrite(COLLECTOR_RX_RAW, pay, 20); }
  TEST_ASSERT_TRUE(cs.getSpoolCount() > 0);   // some spilled to flash
  cs.persistSpool();

  CollectorSerial cs2;                        // reboot: RAM ring gone, re-attach same flash
  setup_cs(cs2);
  cs2.attachSpool(&data, &hdr);

  // Spilled backlog recovered + replayable.
  ms.written.clear();
  bool any = false;
  uint32_t prev = 0;
  while (cs2.drain()) {
    any = true;
  }
  TEST_ASSERT_TRUE(any);
  // A new capture must get a seq above everything ever assigned (no reuse -> no host seq-reset).
  memset(pay, 0xEE, 20);
  cs2.ringWrite(COLLECTOR_RX_RAW, pay, 20);
  TEST_ASSERT_TRUE(cs2.getNewestSeq() > 40);
  (void)prev;
}

// ============================================================
// Unity test runner
// ============================================================

int main(int argc, char **argv) {
  UNITY_BEGIN();

  // CRC-16
  RUN_TEST(test_crc_known_vector);
  RUN_TEST(test_crc_empty);
  RUN_TEST(test_crc_single_byte);
  RUN_TEST(test_crc_bit_sensitivity);

  // Basic write/read
  RUN_TEST(test_write_one_entry);
  RUN_TEST(test_write_multiple_increments_seq);
  RUN_TEST(test_write_zero_length_payload);
  RUN_TEST(test_drain_all_clears_backlog);
  RUN_TEST(test_oldest_newest_seq);

  // Wrapping and sentinels
  RUN_TEST(test_sentinel_written_at_wrap);
  RUN_TEST(test_entries_after_wrap_are_valid);
  RUN_TEST(test_drain_skips_sentinel);
  RUN_TEST(test_handle_ack_skips_sentinel);
  RUN_TEST(test_handle_resume_skips_sentinel);

  // Overflow and eviction
  RUN_TEST(test_overflow_evicts_oldest);
  RUN_TEST(test_send_cursor_advances_on_eviction);
  RUN_TEST(test_multiple_overflows_no_corruption);
  RUN_TEST(test_newest_entries_drain_after_overflow);
  RUN_TEST(test_large_entry_multiple_evictions);

  // drain() wire format
  RUN_TEST(test_drain_wire_format);
  RUN_TEST(test_drain_crc_valid);
  RUN_TEST(test_drain_ignores_available_for_write);
  RUN_TEST(test_drain_empty_returns_false);
  RUN_TEST(test_drain_fifo_order);

  // handleAck()
  RUN_TEST(test_ack_reclaims_entries);
  RUN_TEST(test_ack_entries_above_survive);
  RUN_TEST(test_ack_higher_than_all_empties);
  RUN_TEST(test_ack_zero_no_reclaim);
  RUN_TEST(test_ack_across_sentinel);
  RUN_TEST(test_ack_idempotent);

  // handleResume()
  RUN_TEST(test_resume_zero_replays_all);
  RUN_TEST(test_resume_n_starts_after_n);
  RUN_TEST(test_resume_past_newest_nothing_to_replay);
  RUN_TEST(test_resume_after_ack);
  RUN_TEST(test_resume_across_sentinel);

  // processIncoming()
  RUN_TEST(test_process_incoming_ack);
  RUN_TEST(test_process_incoming_resume);
  RUN_TEST(test_process_incoming_bad_crc);
  RUN_TEST(test_process_incoming_truncated);
  RUN_TEST(test_process_incoming_wrong_start_byte);

  // sendHandshake()
  RUN_TEST(test_handshake_v2_format);
  RUN_TEST(test_handshake_seq_range);
  RUN_TEST(test_handshake_empty_shows_zero_seq);

  // Integration
  RUN_TEST(test_integration_write_drain_verify);
  RUN_TEST(test_integration_partial_drain_ack_continue);
  RUN_TEST(test_integration_resume_replays_same);

  // Status-frame coalescing
  RUN_TEST(test_status_skipped_when_backlog);
  RUN_TEST(test_status_written_when_caught_up);
  RUN_TEST(test_status_forced_when_backlog);
  RUN_TEST(test_status_resumes_after_drain);

  // Ring wrap-alignment corruption regression
  RUN_TEST(test_ring_uniform_wrap_no_corruption);

  // Flash spool
  RUN_TEST(test_spool_append_drain_fifo);
  RUN_TEST(test_spool_overflow_evicts_oldest);
  RUN_TEST(test_spool_wraparound_preserves_data);
  RUN_TEST(test_spool_ack_drops_committed);
  RUN_TEST(test_spool_ack_empties_returns_true);
  RUN_TEST(test_spool_resume_replays_from);
  RUN_TEST(test_spool_resume_zero_replays_all);

  // Spool durability
  RUN_TEST(test_spool_durable_recovers_after_reboot);
  RUN_TEST(test_spool_durable_bad_header_starts_empty);
  RUN_TEST(test_spool_non_durable_no_recovery);

  // Flood-duplicate collapse
  RUN_TEST(test_dedup_collapses_flood_paths);
  RUN_TEST(test_dedup_distinct_not_collapsed);
  RUN_TEST(test_dedup_disabled_passthrough);

  // Two-tier integration (ring + spool)
  RUN_TEST(test_integration_spill_and_drain_in_order);
  RUN_TEST(test_integration_ack_across_tiers);
  RUN_TEST(test_integration_resume_replays_all);
  RUN_TEST(test_integration_both_tiers_full_drops_oldest);
  RUN_TEST(test_integration_durable_reboot_resumes);

  // Bug fix regression tests
  RUN_TEST(test_full_ring_drain_works);
  RUN_TEST(test_full_ring_resume_then_drain);
  RUN_TEST(test_head_reset_after_total_eviction);

  return UNITY_END();
}
