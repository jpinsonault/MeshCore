/*
 * BuzzerBoard — Minimal Buzzer Firmware for Seeed SenseCAP T1000-E
 *
 * Accepts TONE and RTTTL commands over USB serial and BLE (Nordic UART Service).
 *
 * Protocol (newline-delimited):
 *   PING                       → +PONG
 *   TONE <freq_hz> <dur_ms>    → +OK
 *   TONE_START <freq_hz>       → +OK  (plays until STOP)
 *   STOP                       → +OK
 *   RTTTL <rtttl_string>       → +OK PLAYING
 *   STATUS                     → +STATUS playing=<0|1> buzzer=on
 *
 * Power management:
 *   - BLE advertises at 1s for 60s, then 5s thereafter
 *   - After 1 hour of inactivity → deep sleep (button press to wake)
 *   - Long press button (4s) → shutdown chime + deep sleep
 */

#include <Arduino.h>
#include <Adafruit_TinyUSB.h>
#include <bluefruit.h>
#include <NonBlockingRtttl.h>
#include "variant.h"

#define FIRMWARE_VERSION "0.3.0"

// BLE configuration
BLEUart bleuart;
#define BLE_DEVICE_NAME "BuzzerBoard"

// Advertising
#define ADV_FAST_TIMEOUT_S  60      // fast mode (1s interval) for 60 seconds, then 5s

// Power management
#define INACTIVITY_TIMEOUT_MS  3600000UL  // 1 hour
#define LONG_PRESS_MS          4000       // 4 seconds

// Melodies
static const char STARTUP_MELODY[]  = "BB:d=16,o=6,b=200:c,e,g";
static const char SHUTDOWN_MELODY[] = "SD:d=16,o=6,b=200:g,e,c";

// Command buffers (larger for RTTTL strings)
#define CMD_BUF_SIZE 512
static char cmd_buf[CMD_BUF_SIZE];
static uint16_t cmd_len = 0;
static char ble_cmd_buf[CMD_BUF_SIZE];
static uint16_t ble_cmd_len = 0;

// Diagnostic log ring buffer
#define LOG_MAX_ENTRIES 64
#define LOG_ENTRY_SIZE 64
static char log_ring[LOG_MAX_ENTRIES][LOG_ENTRY_SIZE];
static uint16_t log_head = 0;   // next write slot
static uint16_t log_count = 0;  // entries stored

static void log_command(const char* cmd) {
  snprintf(log_ring[log_head], LOG_ENTRY_SIZE, "t=%lu %s", millis(), cmd);
  log_head = (log_head + 1) % LOG_MAX_ENTRIES;
  if (log_count < LOG_MAX_ENTRIES) log_count++;
}

// Activity tracking
static unsigned long last_activity_ms = 0;

// Button state
static bool button_was_down = false;
static unsigned long button_down_ms = 0;

// Forward declarations
static void handle_command(char* cmd, Stream* reply);
static void cmd_ping(Stream* reply);
static void cmd_tone(const char* args, Stream* reply);
static void cmd_tone_start(const char* args, Stream* reply);
static void cmd_stop(Stream* reply);
static void cmd_rtttl(const char* args, Stream* reply);
static void cmd_status(Stream* reply);
static void cmd_log(Stream* reply);
static void setup_ble();
static void disable_peripherals();
static void buzzer_on();
static void buzzer_off();
static void reset_activity();
static void check_button();
static void check_inactivity();
static void go_to_sleep();
static void connect_callback(uint16_t conn_handle);
static void disconnect_callback(uint16_t conn_handle, uint8_t reason);

// --- Peripheral shutdown (from t1000e_power_test) ---

static void disable_peripherals() {
  // GPS off
  pinMode(GPS_EN, OUTPUT);
  pinMode(GPS_VRTC_EN, OUTPUT);
  pinMode(GPS_RESET, OUTPUT);
  pinMode(GPS_SLEEP_INT, OUTPUT);
  pinMode(GPS_RTC_INT, OUTPUT);
  digitalWrite(GPS_EN, LOW);
  digitalWrite(GPS_VRTC_EN, LOW);
  digitalWrite(GPS_RESET, LOW);
  digitalWrite(GPS_SLEEP_INT, LOW);
  digitalWrite(GPS_RTC_INT, LOW);

  // Sensor rails off
  pinMode(PIN_3V3_EN, OUTPUT);
  pinMode(PIN_3V3_ACC_EN, OUTPUT);
  pinMode(SENSOR_EN, OUTPUT);
  digitalWrite(PIN_3V3_EN, LOW);
  digitalWrite(PIN_3V3_ACC_EN, LOW);
  digitalWrite(SENSOR_EN, LOW);

  // LED off
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  // LR1110 radio: deselect and hold in reset
  pinMode(LORA_NSS, OUTPUT);
  pinMode(LORA_RESET, OUTPUT);
  digitalWrite(LORA_NSS, HIGH);
  digitalWrite(LORA_RESET, LOW);
}

// --- Setup ---

void setup() {
  // Blink LED to prove we booted (visible even without serial)
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
  delay(2000);  // hold LED on for 2 seconds — proves we got past setup
  digitalWrite(LED_PIN, LOW);

  disable_peripherals();

  // Enable DC/DC converter for efficiency
  NRF_POWER->DCDCEN = 1;

  // Init buzzer
  pinMode(BUZZER_EN, OUTPUT);
  digitalWrite(BUZZER_EN, HIGH);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);

  // Init button
  pinMode(BUTTON_PIN, INPUT);

  Serial.begin(115200);
  delay(1000);  // longer delay for USB CDC enumeration

  setup_ble();

  Serial.println("+READY BuzzerBoard v" FIRMWARE_VERSION);

  // Play startup melody AFTER serial is up
  rtttl::begin(BUZZER_PIN, STARTUP_MELODY);
  while (!rtttl::done()) rtttl::play();
  buzzer_off();

  reset_activity();
}

// --- BLE setup ---

static void setup_ble() {
  Bluefruit.begin();
  Bluefruit.setTxPower(4);
  Bluefruit.setName(BLE_DEVICE_NAME);

  // NUS (Nordic UART Service) — no security (it's a buzzer)
  bleuart.begin();

  // Connection callbacks
  Bluefruit.Periph.setConnectCallback(connect_callback);
  Bluefruit.Periph.setDisconnectCallback(disconnect_callback);

  // Advertising: 1s fast for 60s, then 5s slow
  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addTxPower();
  Bluefruit.Advertising.addService(bleuart);
  Bluefruit.ScanResponse.addName();
  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.setIntervalMS(1000, 5000);
  Bluefruit.Advertising.setFastTimeout(ADV_FAST_TIMEOUT_S);
  Bluefruit.Advertising.start(0);
}

static void connect_callback(uint16_t conn_handle) {
  (void)conn_handle;
  reset_activity();
}

static void disconnect_callback(uint16_t conn_handle, uint8_t reason) {
  (void)conn_handle;
  (void)reason;
  reset_activity();
}

// --- Main loop ---

void loop() {
  // Advance RTTTL if playing
  if (!rtttl::done()) rtttl::play();

  // USB Serial commands
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r' || c == '\n') {
      if (cmd_len > 0) {
        cmd_buf[cmd_len] = '\0';
        handle_command(cmd_buf, &Serial);
        cmd_len = 0;
      }
    } else if (cmd_len < CMD_BUF_SIZE - 1) {
      cmd_buf[cmd_len++] = c;
    }
  }

  // BLE commands
  while (bleuart.available()) {
    char c = bleuart.read();
    if (c == '\r' || c == '\n') {
      if (ble_cmd_len > 0) {
        ble_cmd_buf[ble_cmd_len] = '\0';
        handle_command(ble_cmd_buf, &bleuart);
        ble_cmd_len = 0;
      }
    } else if (ble_cmd_len < CMD_BUF_SIZE - 1) {
      ble_cmd_buf[ble_cmd_len++] = c;
    }
  }

  // Advertising LED blink — short flash every 2s when not connected
  {
    static unsigned long blink_start_ms = 0;
    static bool blink_on = false;
    unsigned long now = millis();
    if (!Bluefruit.connected()) {
      if (!blink_on && (now - blink_start_ms >= 2000)) {
        digitalWrite(LED_PIN, HIGH);
        blink_on = true;
        blink_start_ms = now;
      } else if (blink_on && (now - blink_start_ms >= 30)) {
        digitalWrite(LED_PIN, LOW);
        blink_on = false;
      }
    } else if (blink_on) {
      digitalWrite(LED_PIN, LOW);
      blink_on = false;
    }
  }

  // Button and power management
  check_button();
  check_inactivity();
}

// --- Command dispatch ---

static void handle_command(char* cmd, Stream* reply) {
  // Skip leading whitespace
  while (*cmd == ' ') cmd++;

  reset_activity();

  log_command(cmd);

  if (strncasecmp(cmd, "LOG", 3) == 0) {
    cmd_log(reply);
  } else if (strncasecmp(cmd, "PING", 4) == 0) {
    cmd_ping(reply);
  } else if (strncasecmp(cmd, "TONE_START ", 11) == 0) {
    cmd_tone_start(cmd + 11, reply);
  } else if (strncasecmp(cmd, "TONE ", 5) == 0) {
    cmd_tone(cmd + 5, reply);
  } else if (strncasecmp(cmd, "STOP", 4) == 0) {
    cmd_stop(reply);
  } else if (strncasecmp(cmd, "RTTTL ", 6) == 0) {
    cmd_rtttl(cmd + 6, reply);
  } else if (strncasecmp(cmd, "STATUS", 6) == 0) {
    cmd_status(reply);
  } else {
    reply->print("+ERR Unknown command: ");
    reply->println(cmd);
  }
}

// --- Commands ---

static void cmd_ping(Stream* reply) {
  reply->println("+PONG");
}

static void cmd_tone(const char* args, Stream* reply) {
  int freq = 0, dur = 0;
  if (sscanf(args, "%d %d", &freq, &dur) != 2 || freq < 20 || freq > 20000 || dur < 1) {
    reply->println("+ERR Invalid TONE args");
    return;
  }
  // Stop any RTTTL playback first
  if (!rtttl::done()) rtttl::stop();
  // Enable buzzer gate and play
  buzzer_on();
  tone(BUZZER_PIN, freq, dur);
  reply->println("+OK");
}

static void cmd_tone_start(const char* args, Stream* reply) {
  int freq = 0;
  if (sscanf(args, "%d", &freq) != 1 || freq < 20 || freq > 20000) {
    reply->println("+ERR Invalid TONE_START args");
    return;
  }
  if (!rtttl::done()) rtttl::stop();
  buzzer_on();
  tone(BUZZER_PIN, freq);  // no duration = play indefinitely
  reply->println("+OK");
}

static void cmd_stop(Stream* reply) {
  if (!rtttl::done()) rtttl::stop();
  noTone(BUZZER_PIN);
  buzzer_off();
  reply->println("+OK");
}

static void cmd_rtttl(const char* args, Stream* reply) {
  if (!args || strlen(args) < 5) {
    reply->println("+ERR Empty RTTTL");
    return;
  }
  // Copy to static buffer since rtttl::begin reads from the pointer over time
  static char rtttl_buf[CMD_BUF_SIZE];
  strncpy(rtttl_buf, args, CMD_BUF_SIZE - 1);
  rtttl_buf[CMD_BUF_SIZE - 1] = '\0';

  buzzer_on();
  rtttl::begin(BUZZER_PIN, rtttl_buf);
  reply->println("+OK PLAYING");
}

static void cmd_status(Stream* reply) {
  reply->print("+STATUS playing=");
  reply->print(rtttl::done() ? "0" : "1");
  reply->println(" buzzer=on");
}

static void cmd_log(Stream* reply) {
  if (log_count == 0) {
    reply->println("+LOG EMPTY");
    return;
  }
  // Walk the ring buffer oldest-first
  uint16_t start = (log_count < LOG_MAX_ENTRIES) ? 0 : log_head;
  for (uint16_t i = 0; i < log_count; i++) {
    uint16_t idx = (start + i) % LOG_MAX_ENTRIES;
    reply->print("+LOG ");
    reply->println(log_ring[idx]);
  }
  reply->println("+LOG END");
  // Clear after dump
  log_count = 0;
  log_head = 0;
}

// --- Button handling ---

static void check_button() {
  bool is_down = (digitalRead(BUTTON_PIN) == HIGH);

  if (is_down && !button_was_down) {
    // Button just pressed
    button_down_ms = millis();
    button_was_down = true;
  } else if (is_down && button_was_down) {
    // Button held — check for long press
    if ((millis() - button_down_ms) >= LONG_PRESS_MS) {
      // Long press: shutdown with chime
      if (!rtttl::done()) rtttl::stop();
      noTone(BUZZER_PIN);
      buzzer_on();
      rtttl::begin(BUZZER_PIN, SHUTDOWN_MELODY);
      while (!rtttl::done()) rtttl::play();
      buzzer_off();

      // Wait for button release so it doesn't immediately wake
      while (digitalRead(BUTTON_PIN) == HIGH) delay(10);
      delay(100);  // debounce

      go_to_sleep();
    }
  } else if (!is_down && button_was_down) {
    // Button released (short press) — restart fast advertising
    button_was_down = false;
    reset_activity();
    Bluefruit.Advertising.restartOnDisconnect(true);
    Bluefruit.Advertising.start(0);
  }
}

// --- Power management ---

static void reset_activity() {
  last_activity_ms = millis();
}

static void check_inactivity() {
  // No auto-sleep — just keep advertising at the slow interval.
  // Long-press button (4s) is the only way to enter deep sleep.
  (void)last_activity_ms;
}

static void go_to_sleep() {
  // Silence everything
  if (!rtttl::done()) rtttl::stop();
  noTone(BUZZER_PIN);
  buzzer_off();

  // Stop BLE
  Bluefruit.Advertising.stop();

  // Configure button as wake source (active HIGH)
  nrf_gpio_cfg_sense_input(BUTTON_PIN, NRF_GPIO_PIN_NOPULL, NRF_GPIO_PIN_SENSE_HIGH);

  // Deep sleep — wakes via button, runs setup() again
  sd_power_system_off();
}

// --- Buzzer helpers ---

static void buzzer_on() {
  digitalWrite(BUZZER_EN, HIGH);
}

static void buzzer_off() {
  digitalWrite(BUZZER_EN, LOW);
  digitalWrite(BUZZER_PIN, LOW);
}
