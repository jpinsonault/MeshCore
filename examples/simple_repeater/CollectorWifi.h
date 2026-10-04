#pragma once

// WiFi link for the collector repeater (ESP32 only, build with -D COLLECTOR_WIFI=1).
//
// - Joins a WiFi network (credentials set from the CLI, stored in /collector_wifi).
// - Serves the same byte stream as USB serial on a TCP port: CLI text lines plus collector frames.
//   A TCP client must send "auth <admin password>" before anything else is accepted.
// - Accepts network firmware updates (ArduinoOTA / espota), using the admin password from boot time.

#if defined(ESP32) && defined(COLLECTOR_WIFI)

#include <Arduino.h>
#include <WiFi.h>
#include <FS.h>

#ifndef COLLECTOR_WIFI_PORT
  #define COLLECTOR_WIFI_PORT  5005
#endif

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
  bool _authed;
  bool _services_started;
  bool _ota_active;
  uint32_t _session;
  char _host[32];
  char _ota_pass[17];

  void loadConfig();
  void saveConfig();
  void connect();
  void startServices();

public:
  CollectorWifi();

  void begin(fs::FS* fs, const char* node_name, const char* admin_pass);
  void loop();

  // The TCP client that collector output and CLI replies go to, or NULL when none is logged in.
  Stream* client();
  // Raw connected client, before auth; used to read its input.
  Stream* rawClient();
  bool isAuthed() const { return _authed; }
  uint32_t sessionId() const { return _session; }   // changes on every new TCP connection
  // Consumes an "auth <password>" line from an unauthenticated client. Returns true if the line was handled.
  bool handleAuth(const char* line, char* reply);

  bool isRunning() const { return _cfg.enabled && _cfg.ssid[0] != 0; }
  bool isOtaActive() const { return _ota_active; }

  // CLI: "wifi status|on|off|ssid <name>|pass <password>". Returns false if not a wifi command.
  bool handleCommand(const char* command, bool is_local, char* reply);
};

// Collector output sink: the logged-in TCP client if there is one, otherwise USB serial.
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
