#include "CollectorWifi.h"

#if defined(ESP32) && defined(COLLECTOR_WIFI)

#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <ESPAsyncWebServer.h>
#include <AsyncElegantOTA.h>
#include <helpers/TxtDataHelpers.h>

#define WIFI_CONFIG_FILE  "/collector_wifi"
#define LOGIN_BANNER      "MeshCore collector - send: auth <admin password>\r\n"

CollectorWifi collector_wifi;
CollectorLinkStream collector_link(collector_wifi, Serial);

// Constant-time string compare: equal length AND equal bytes, without an early-exit that would
// leak the matched-prefix length through timing. Returns true only on a full match.
static bool constTimeEquals(const char* a, const char* b) {
  size_t la = strlen(a), lb = strlen(b);
  uint8_t diff = (uint8_t)(la ^ lb);
  size_t n = la > lb ? la : lb;
  for (size_t i = 0; i < n; i++) {
    diff |= (uint8_t)((i < la ? a[i] : 0) ^ (i < lb ? b[i] : 0));
  }
  return diff == 0;
}

// ------------------------------------------------------------------ WsStream

WsStream::WsStream() : _rx_head(0), _rx_tail(0), _tx_len(0), ws(NULL), client_id(0) {
  _mux = portMUX_INITIALIZER_UNLOCKED;
}

void WsStream::reset() {
  portENTER_CRITICAL(&_mux);
  _rx_head = _rx_tail = 0;
  portEXIT_CRITICAL(&_mux);
}

void WsStream::push(const uint8_t* data, size_t len) {
  portENTER_CRITICAL(&_mux);
  for (size_t i = 0; i < len; i++) {
    uint16_t next = (_rx_head + 1) % sizeof(_rx);
    if (next == _rx_tail) break;   // full: drop the rest (the browser sends one short line at a time)
    _rx[_rx_head] = data[i];
    _rx_head = next;
  }
  portEXIT_CRITICAL(&_mux);
}

int WsStream::available() {
  portENTER_CRITICAL(&_mux);
  int n = (_rx_head - _rx_tail + sizeof(_rx)) % sizeof(_rx);
  portEXIT_CRITICAL(&_mux);
  return n;
}

int WsStream::peek() {
  portENTER_CRITICAL(&_mux);
  int b = (_rx_head == _rx_tail) ? -1 : _rx[_rx_tail];
  portEXIT_CRITICAL(&_mux);
  return b;
}

int WsStream::read() {
  portENTER_CRITICAL(&_mux);
  int b = -1;
  if (_rx_head != _rx_tail) {
    b = _rx[_rx_tail];
    _rx_tail = (_rx_tail + 1) % sizeof(_rx);
  }
  portEXIT_CRITICAL(&_mux);
  return b;
}

size_t WsStream::write(const uint8_t* buf, size_t len) {
  if (client_id == 0) return len;   // nobody listening
  size_t done = 0;
  while (done < len) {
    if (_tx_len == sizeof(_tx)) {
      flush();
      if (_tx_len == sizeof(_tx)) break;   // client's queue is full: drop (collector RESUME recovers frames)
    }
    size_t n = min(len - done, sizeof(_tx) - _tx_len);
    memcpy(&_tx[_tx_len], &buf[done], n);
    _tx_len += n;
    done += n;
  }
  return done;
}

void WsStream::flush() {
  uint32_t id = client_id;
  if (_tx_len == 0 || id == 0 || ws == NULL) return;
  if (!ws->availableForWrite(id)) return;   // keep buffering until the client drains
  ws->binary(id, _tx, _tx_len);
  _tx_len = 0;
}

// ------------------------------------------------------------------ CollectorWifi

CollectorWifi::CollectorWifi() {
  memset(&_cfg, 0, sizeof(_cfg));
  _cfg.port = COLLECTOR_WIFI_PORT;
  _fs = NULL;
  _admin_pass = "";
  _server = NULL;
  _http = NULL;
  for (int i = 0; i < COLLECTOR_WIFI_LINKS; i++) {
    _authed[i] = false;
    _session[i] = 0;
  }
  _ws_mux = portMUX_INITIALIZER_UNLOCKED;
  _services_started = false;
  _ota_active = false;
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

void CollectorWifi::startWeb() {
  _http = new AsyncWebServer(80);
  AsyncWebSocket* ws = new AsyncWebSocket("/ws");
  _ws.ws = ws;

  // Runs on the async TCP task: only touch the stream buffers and the volatile link state here.
  ws->onEvent([this](AsyncWebSocket* server, AsyncWebSocketClient* client, AwsEventType type, void* arg,
                     uint8_t* data, size_t len) {
    if (type == WS_EVT_CONNECT) {
      portENTER_CRITICAL(&_ws_mux);
      uint32_t old = _ws.client_id;
      _authed[WIFI_LINK_WS] = false;
      _ws.client_id = client->id();
      _session[WIFI_LINK_WS]++;
      portEXIT_CRITICAL(&_ws_mux);
      _ws.reset();                   // takes WsStream's own lock; keep it outside _ws_mux
      if (old) server->close(old);   // a new connection replaces the old one
      client->setCloseClientOnQueueFull(false);
      client->binary(LOGIN_BANNER);
    } else if (type == WS_EVT_DISCONNECT) {
      portENTER_CRITICAL(&_ws_mux);
      if (client->id() == _ws.client_id) {
        _ws.client_id = 0;
        _authed[WIFI_LINK_WS] = false;
      }
      portEXIT_CRITICAL(&_ws_mux);
    } else if (type == WS_EVT_DATA) {
      if (client->id() == _ws.client_id) _ws.push(data, len);
    }
  });
  _http->addHandler(ws);

  _http->on("/", HTTP_GET, [this](AsyncWebServerRequest* request) {
    char page[200];
    snprintf(page, sizeof(page),
             "MeshCore collector repeater (%s)\n\nws://%s.local/ws  - CLI + collector stream\n"
             "/update           - firmware upload\n", _host, _host);
    request->send(200, "text/plain", page);
  });

  // browser firmware upload; HTTP basic auth, user "admin". Only exposed when a password is set —
  // an empty basic-auth password means unauthenticated flashing. (The 'wifi on' guard already
  // requires a non-default password, so this is defense-in-depth.)
  if (_ota_pass[0]) {
    static char ota_id[48];
    snprintf(ota_id, sizeof(ota_id), "%s (collector)", _host);
    AsyncElegantOTA.setID(ota_id);
    AsyncElegantOTA.begin(_http, "admin", _ota_pass);
  }

  _http->begin();
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
    _authed[WIFI_LINK_TCP] = false;
    Serial.println("OTA: update started");
  });
  ArduinoOTA.onEnd([]() { Serial.println("OTA: done, rebooting"); });
  ArduinoOTA.onError([this](ota_error_t err) {
    _ota_active = false;
    Serial.printf("OTA: error %u\n", (unsigned)err);
  });
  ArduinoOTA.begin();   // also starts mDNS as <host>.local
  MDNS.addService("meshcore", "tcp", _cfg.port);
  MDNS.addService("http", "tcp", 80);

  startWeb();

  _services_started = true;
  Serial.printf("WiFi: %s  ip=%s  tcp port %u, http/ws port 80\n", _host, WiFi.localIP().toString().c_str(),
                _cfg.port);
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
    _authed[WIFI_LINK_TCP] = false;
    _session[WIFI_LINK_TCP]++;
    _client.print(LOGIN_BANNER);
  }
  if (_client && !_client.connected()) {
    _client.stop();
    _authed[WIFI_LINK_TCP] = false;
  }

  _ws.flush();
  static unsigned long next_cleanup = 0;
  if (millis() > next_cleanup) {
    _ws.ws->cleanupClients(2);
    next_cleanup = millis() + 1000;
  }
}

Stream* CollectorWifi::client() {
  if (_authed[WIFI_LINK_TCP] && _client.connected()) return &_client;
  portENTER_CRITICAL(&_ws_mux);
  bool ws_ok = _authed[WIFI_LINK_WS] && _ws.client_id != 0;
  portEXIT_CRITICAL(&_ws_mux);
  if (ws_ok) return &_ws;
  return NULL;
}

Stream* CollectorWifi::linkInput(int link) {
  if (link == WIFI_LINK_TCP) return _client.connected() ? &_client : NULL;
  if (link == WIFI_LINK_WS) {
    portENTER_CRITICAL(&_ws_mux);
    bool has = _ws.client_id != 0;
    portEXIT_CRITICAL(&_ws_mux);
    return has ? &_ws : NULL;
  }
  return NULL;
}

bool CollectorWifi::handleAuth(int link, const char* line, char* reply) {
  if (_authed[link]) return false;
  // constant-time compare so a wrong password can't be recovered byte-by-byte via timing
  if (memcmp(line, "auth ", 5) == 0 && _admin_pass[0] && constTimeEquals(&line[5], _admin_pass)) {
    if (link == WIFI_LINK_WS) {
      portENTER_CRITICAL(&_ws_mux);
      _authed[link] = true;
      portEXIT_CRITICAL(&_ws_mux);
    } else {
      _authed[link] = true;
    }
    strcpy(reply, "OK - authenticated");
  } else {
    strcpy(reply, "Err - auth required: auth <admin password>");
  }
  return true;
}

bool CollectorWifi::handleCommand(const char* command, bool is_local, char* reply) {
  if (_services_started && strcmp(command, "start ota") == 0) {
    // the stock OTA server would collide with ours on port 80
    snprintf(reply, 160, "OK - WiFi is up: open http://%s.local/update (user admin, admin password)", _host);
    return true;
  }
  if (memcmp(command, "wifi", 4) != 0 || (command[4] != 0 && command[4] != ' ')) return false;
  const char* sub = command + 4;
  while (*sub == ' ') sub++;

  if (*sub == 0 || strcmp(sub, "status") == 0) {
    if (!isRunning()) {
      sprintf(reply, "wifi off%s", _cfg.ssid[0] ? "" : " (no ssid set)");
    } else if (WiFi.status() == WL_CONNECTED) {
      snprintf(reply, 160, "wifi connected ssid=%s ip=%s rssi=%d host=%s.local tcp=%s ws=%s heap=%u",
               _cfg.ssid, WiFi.localIP().toString().c_str(), (int)WiFi.RSSI(), _host,
               _authed[WIFI_LINK_TCP] ? "yes" : (linkInput(WIFI_LINK_TCP) ? "unauthed" : "no"),
               _authed[WIFI_LINK_WS] ? "yes" : (linkInput(WIFI_LINK_WS) ? "unauthed" : "no"),
               (unsigned)ESP.getFreeHeap());
    } else {
      snprintf(reply, 160, "wifi connecting ssid=%s (status %d)", _cfg.ssid, (int)WiFi.status());
    }
    return true;
  }
  if (!is_local) {   // credentials and on/off only from USB serial or a logged-in network link
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
    } else if (_admin_pass[0] == 0 || strcmp(_admin_pass, "password") == 0) {
      // The network links grant the full admin CLI + OTA flash behind this one password, in
      // cleartext over the LAN. Refuse to expose them while it's unset or the stock default.
      strcpy(reply, "Err - set a non-default admin password first (see 'password'), then: wifi on");
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
    // close the WebSocket client too (previously leaked), and force both links to re-auth
    portENTER_CRITICAL(&_ws_mux);
    uint32_t ws_id = _ws.client_id;
    _ws.client_id = 0;
    _authed[WIFI_LINK_TCP] = _authed[WIFI_LINK_WS] = false;
    portEXIT_CRITICAL(&_ws_mux);
    if (ws_id && _ws.ws) _ws.ws->close(ws_id);
    _ws.reset();
    _session[WIFI_LINK_TCP]++;
    _session[WIFI_LINK_WS]++;
    // Keep STA mode and the netif (and the bound server sockets) alive so a later `wifi on`
    // works without a reboot; full WIFI_OFF would destroy the listening sockets and
    // _services_started is never re-armed. loop() stops servicing clients once enabled=0.
    WiFi.disconnect();
    strcpy(reply, "OK - wifi off");
  } else {
    strcpy(reply, "Err - use: wifi status|on|off|ssid <name>|pass <password>");
  }
  return true;
}

#endif
