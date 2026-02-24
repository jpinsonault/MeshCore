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
#define COLLECTOR_HANDSHAKE     0xDF

#define COLLECTOR_PROTOCOL_VER  1

#define COLLECTOR_HEARTBEAT_INTERVAL  10000  // milliseconds

class CollectorSerial {
  Stream *_serial;

  void writeFrameHeader(uint8_t type, uint16_t payload_len) {
    uint16_t frame_len = 1 + payload_len;
    uint8_t hdr[4];
    hdr[0] = COLLECTOR_FRAME_START;
    hdr[1] = frame_len & 0xFF;
    hdr[2] = frame_len >> 8;
    hdr[3] = type;
    _serial->write(hdr, 4);
  }

public:
  CollectorSerial() : _serial(nullptr) {}

  void begin(Stream &serial) { _serial = &serial; }

  void sendRxRaw(float snr, float rssi, const uint8_t *raw, int raw_len) {
    writeFrameHeader(COLLECTOR_RX_RAW, 2 + raw_len);
    uint8_t meta[2];
    meta[0] = (uint8_t)(int8_t)(snr * 4);
    meta[1] = (uint8_t)(int8_t)(rssi);
    _serial->write(meta, 2);
    _serial->write(raw, raw_len);
  }

  void sendTxRaw(const uint8_t *raw, int raw_len) {
    writeFrameHeader(COLLECTOR_TX_RAW, raw_len);
    _serial->write(raw, raw_len);
  }

  void sendAdvertisement(uint32_t timestamp, int8_t snr_x4,
                         const uint8_t *pub_key,
                         const uint8_t *app_data, size_t app_data_len) {
    if (app_data_len > MAX_ADVERT_DATA_SIZE) app_data_len = MAX_ADVERT_DATA_SIZE;
    writeFrameHeader(COLLECTOR_ADVERTISEMENT, 4 + 1 + PUB_KEY_SIZE + app_data_len);
    _serial->write((const uint8_t *)&timestamp, 4);
    _serial->write((const uint8_t *)&snr_x4, 1);
    _serial->write(pub_key, PUB_KEY_SIZE);
    if (app_data_len > 0) _serial->write(app_data, app_data_len);
  }

  void sendHeartbeat(uint32_t timestamp, uint16_t battery_mv,
                     uint32_t rx_flood, uint32_t rx_direct,
                     uint32_t tx_flood, uint32_t tx_direct,
                     uint8_t free_pkts, uint32_t uptime_secs) {
    writeFrameHeader(COLLECTOR_HEARTBEAT, 27);
    _serial->write((const uint8_t *)&timestamp, 4);
    _serial->write((const uint8_t *)&battery_mv, 2);
    _serial->write((const uint8_t *)&rx_flood, 4);
    _serial->write((const uint8_t *)&rx_direct, 4);
    _serial->write((const uint8_t *)&tx_flood, 4);
    _serial->write((const uint8_t *)&tx_direct, 4);
    _serial->write(&free_pkts, 1);
    _serial->write((const uint8_t *)&uptime_secs, 4);
  }

  void sendHandshake() {
    writeFrameHeader(COLLECTOR_HANDSHAKE, 10);
    _serial->write((const uint8_t *)"COLLECTOR", 9);
    uint8_t ver = COLLECTOR_PROTOCOL_VER;
    _serial->write(&ver, 1);
  }
};
