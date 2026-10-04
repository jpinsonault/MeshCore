#pragma once

// WiFi link for the collector repeater (ESP32 only, build with -D COLLECTOR_WIFI=1).
//
// - Joins a WiFi network (credentials set from the CLI, stored in /collector_wifi).
// - Serves the same byte stream as USB serial over two network links: CLI text lines plus collector frames.
//     link 0: raw TCP on port 5005 (the Python collector)
//     link 1: WebSocket at ws://<host>/ws, binary messages (browser config page)
//   A client must send "auth <admin password>" before anything else is accepted.
//   One client per link; a new connection replaces the old one.
// - Firmware updates over the network: ArduinoOTA (espota) and a browser upload page at /update,
//   both using the admin password from boot time.

#if defined(ESP32) && defined(COLLECTOR_WIFI)

#include <Arduino.h>
#include <WiFi.h>
#include <FS.h>

#ifndef COLLECTOR_WIFI_PORT
  #define COLLECTOR_WIFI_PORT  5005
#endif

#define COLLECTOR_WIFI_LINKS  2
#define WIFI_LINK_TCP         0
#define WIFI_LINK_WS          1

class AsyncWebServer;
class AsyncWebSocket;

// Stream over the single WebSocket client. Input is filled from the async TCP task and read from loop();
// output is buffered and sent as one binary message per flush().
class WsStream : public Stream {
  uint8_t _rx[512];
  volatile uint16_t _rx_head, _rx_tail;
  uint8_t _tx[512];
  uint16_t _tx_len;
  portMUX_TYPE _mux;
public:
  AsyncWebSocket* ws;
  volatile uint32_t client_id;   // 0 = no client

  WsStream();
  void reset();
  void push(const uint8_t* data, size_t len);   // async task
  void flush() override;

  int available() override;
  int read() override;
  int peek() override;
  size_t write(uint8_t b) override { return write(&b, 1); }
  size_t write(const uint8_t* buf, size_t len) override;
};

class CollectorWifi {
  struct Config {
    uint8_t  enabled;
    uint8_t  reserved;
    uint16_t port;
    char ssid[33];
    char pass[65];
  } _cfg;

  fs::FS* _fs;
  const char* _admin_pass;   // live pointer into NodePrefs, so a changed password applies to new logins
  WiFiServer* _server;
  WiFiClient _client;
  AsyncWebServer* _http;
  WsStream _ws;
  volatile bool _authed[COLLECTOR_WIFI_LINKS];
  volatile uint32_t _session[COLLECTOR_WIFI_LINKS];
  bool _services_started;
  bool _ota_active;
  char _host[32];
  char _ota_pass[17];

  void loadConfig();
  void saveConfig();
  void connect();
  void startServices();
  void startWeb();

public:
  CollectorWifi();

  void begin(fs::FS* fs, const char* node_name, const char* admin_pass);
  void loop();

  // Where collector output and the stream go: the first logged-in link (TCP, then WebSocket), else NULL.
  Stream* client();
  // A link's input stream while a client is connected (logged in or not), else NULL.
  Stream* linkInput(int link);
  bool linkAuthed(int link) const { return _authed[link]; }
  uint32_t linkSession(int link) const { return _session[link]; }   // changes on every new connection
  // Consumes an "auth <password>" line from a client that hasn't logged in. Returns true if handled.
  bool handleAuth(int link, const char* line, char* reply);

  bool isRunning() const { return _cfg.enabled && _cfg.ssid[0] != 0; }
  bool isOtaActive() const { return _ota_active; }

  // CLI: "wifi status|on|off|ssid <name>|pass <password>", and "start ota" while WiFi is up.
  // Returns false if not one of these.
  bool handleCommand(const char* command, bool is_local, char* reply);
};

// Collector output sink: the logged-in network client if there is one, otherwise USB serial.
class CollectorLinkStream : public Stream {
  CollectorWifi& _wifi;
  Stream& _usb;
public:
  CollectorLinkStream(CollectorWifi& wifi, Stream& usb) : _wifi(wifi), _usb(usb) { }

  size_t write(uint8_t b) override { return write(&b, 1); }
  size_t write(const uint8_t* buf, size_t len) override {
    Stream* c = _wifi.client();
    return c ? c->write(buf, len) : _usb.write(buf, len);
  }
  int available() override { return 0; }   // output only; input is read per-source in main.cpp
  int read() override { return -1; }
  int peek() override { return -1; }
};

extern CollectorWifi collector_wifi;
extern CollectorLinkStream collector_link;

#endif
