#include "CollectorWifi.h"

#if defined(ESP32) && defined(COLLECTOR_WIFI)

#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <helpers/TxtDataHelpers.h>

#define WIFI_CONFIG_FILE  "/collector_wifi"

CollectorWifi collector_wifi;
CollectorLinkStream collector_link(collector_wifi, Serial);

CollectorWifi::CollectorWifi() {
  memset(&_cfg, 0, sizeof(_cfg));
  _cfg.port = COLLECTOR_WIFI_PORT;
  _fs = NULL;
  _admin_pass = "";
  _server = NULL;
  _authed = false;
  _services_started = false;
  _ota_active = false;
  _session = 0;
  _host[0] = 0;
  _ota_pass[0] = 0;
}

void CollectorWifi::loadConfig() {
  File f = _fs->open(WIFI_CONFIG_FILE, "r");
  if (f) {
    Config c;
    if (f.read((uint8_t *)&c, sizeof(c)) == sizeof(c)) {
      c.ssid[sizeof(c.ssid) - 1] = 0;
      c.pass[sizeof(c.pass) - 1] = 0;
      if (c.port == 0) c.port = COLLECTOR_WIFI_PORT;
      _cfg = c;
    }
    f.close();
  }
}

void CollectorWifi::saveConfig() {
  File f = _fs->open(WIFI_CONFIG_FILE, "w", true);
  if (f) {
    f.write((const uint8_t *)&_cfg, sizeof(_cfg));
    f.close();
  }
}

void CollectorWifi::begin(fs::FS* fs, const char* node_name, const char* admin_pass) {
  _fs = fs;
  _admin_pass = admin_pass;
  StrHelper::strncpy(_ota_pass, admin_pass, sizeof(_ota_pass));

  // mDNS/DHCP hostname from the node name: lowercase alphanumerics, everything else becomes '-'
  int n = 0;
  for (const char* p = node_name; *p && n < (int)sizeof(_host) - 1; p++) {
    char c = *p;
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
    bool ok = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
    if (ok) _host[n++] = c;
    else if (n > 0 && _host[n - 1] != '-') _host[n++] = '-';
  }
  while (n > 0 && _host[n - 1] == '-') n--;
  _host[n] = 0;
  if (n == 0) strcpy(_host, "meshcore-collector");

  loadConfig();
  if (isRunning()) connect();
}

void CollectorWifi::connect() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(_host);
  WiFi.setSleep(false);         // plugged in; keeps the CLI and stream responsive
  WiFi.setAutoReconnect(true);
  WiFi.begin(_cfg.ssid, _cfg.pass);
}

void CollectorWifi::startServices() {
  _server = new WiFiServer(_cfg.port);
  _server->begin();
  _server->setNoDelay(true);

  ArduinoOTA.setHostname(_host);
  if (_ota_pass[0]) ArduinoOTA.setPassword(_ota_pass);
  ArduinoOTA.onStart([this]() {
    _ota_active = true;
    if (_client) _client.stop();   // the transfer blocks the loop; drop the stream cleanly first
    _authed = false;
    Serial.println("OTA: update started");
  });
  ArduinoOTA.onEnd([]() { Serial.println("OTA: done, rebooting"); });
  ArduinoOTA.onError([this](ota_error_t err) {
    _ota_active = false;
    Serial.printf("OTA: error %u\n", (unsigned)err);
  });
  ArduinoOTA.begin();   // also starts mDNS as <host>.local
  MDNS.addService("meshcore", "tcp", _cfg.port);

  _services_started = true;
  Serial.printf("WiFi: %s  ip=%s  tcp port %u\n", _host, WiFi.localIP().toString().c_str(), _cfg.port);
}

void CollectorWifi::loop() {
  if (!isRunning()) return;
  if (!_services_started) {
    if (WiFi.status() == WL_CONNECTED) startServices();
    return;
  }

  ArduinoOTA.handle();

  // a new connection replaces the old one, so a dead session can't lock the port
  WiFiClient incoming = _server->available();
  if (incoming) {
    if (_client) _client.stop();
    _client = incoming;
    _client.setNoDelay(true);
    _authed = false;
    _session++;
    _client.print("MeshCore collector - send: auth <admin password>\r\n");
  }
  if (_client && !_client.connected()) {
    _client.stop();
    _authed = false;
  }
}

Stream* CollectorWifi::client() {
  return (_authed && _client.connected()) ? &_client : NULL;
}

Stream* CollectorWifi::rawClient() {
  return _client.connected() ? &_client : NULL;
}

bool CollectorWifi::handleAuth(const char* line, char* reply) {
  if (_authed) return false;
  if (memcmp(line, "auth ", 5) == 0 && _admin_pass[0] && strcmp(&line[5], _admin_pass) == 0) {
    _authed = true;
    strcpy(reply, "OK - authenticated");
  } else {
    strcpy(reply, "Err - auth required: auth <admin password>");
  }
  return true;
}

bool CollectorWifi::handleCommand(const char* command, bool is_local, char* reply) {
  if (memcmp(command, "wifi", 4) != 0 || (command[4] != 0 && command[4] != ' ')) return false;
  const char* sub = command + 4;
  while (*sub == ' ') sub++;

  if (*sub == 0 || strcmp(sub, "status") == 0) {
    if (!isRunning()) {
      sprintf(reply, "wifi off%s", _cfg.ssid[0] ? "" : " (no ssid set)");
    } else if (WiFi.status() == WL_CONNECTED) {
      snprintf(reply, 160, "wifi connected ssid=%s ip=%s rssi=%d host=%s.local port=%u client=%s heap=%u",
               _cfg.ssid, WiFi.localIP().toString().c_str(), (int)WiFi.RSSI(), _host, _cfg.port,
               client() ? "yes" : (rawClient() ? "unauthed" : "no"), (unsigned)ESP.getFreeHeap());
    } else {
      snprintf(reply, 160, "wifi connecting ssid=%s (status %d)", _cfg.ssid, (int)WiFi.status());
    }
    return true;
  }
  if (!is_local) {   // credentials and on/off only from USB serial or an authenticated TCP session
    strcpy(reply, "Err - wifi settings are local only");
    return true;
  }
  if (memcmp(sub, "ssid ", 5) == 0) {
    StrHelper::strncpy(_cfg.ssid, sub + 5, sizeof(_cfg.ssid));
    saveConfig();
    strcpy(reply, "OK - then: wifi on");
  } else if (memcmp(sub, "pass ", 5) == 0) {
    StrHelper::strncpy(_cfg.pass, sub + 5, sizeof(_cfg.pass));
    saveConfig();
    strcpy(reply, "OK - then: wifi on");
  } else if (strcmp(sub, "on") == 0) {
    if (_cfg.ssid[0] == 0) {
      strcpy(reply, "Err - set wifi ssid first");
    } else {
      _cfg.enabled = 1;
      saveConfig();
      WiFi.disconnect();
      connect();   // picks up changed credentials; services start (once) when the link is up
      strcpy(reply, "OK - connecting, check: wifi status");
    }
  } else if (strcmp(sub, "off") == 0) {
    _cfg.enabled = 0;
    saveConfig();
    if (_client) _client.stop();
    _authed = false;
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    strcpy(reply, "OK - wifi off");
  } else {
    strcpy(reply, "Err - use: wifi status|on|off|ssid <name>|pass <password>");
  }
  return true;
}

#endif
