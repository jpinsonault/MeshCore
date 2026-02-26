/*
 * BuzzerBoard — Minimal Buzzer Firmware for Seeed SenseCAP T1000-E
 *
 * Accepts TONE and RTTTL commands over USB serial and BLE (Nordic UART Service).
 *
 * Protocol (newline-delimited):
 *   PING                       → +PONG
 *   TONE <freq_hz> <dur_ms>    → +OK
 *   STOP                       → +OK
 *   RTTTL <rtttl_string>       → +OK PLAYING
 *   STATUS                     → +STATUS playing=<0|1> buzzer=on
 */

#include <Arduino.h>
#include <Adafruit_TinyUSB.h>
#include <bluefruit.h>
#include <NonBlockingRtttl.h>
#include "variant.h"

#define FIRMWARE_VERSION "0.2.0"

// BLE configuration
BLEUart bleuart;
#define BLE_PIN_CODE   1812
#define BLE_DEVICE_NAME "BuzzerBoard"

// Startup melody
static const char STARTUP_MELODY[] = "BB:d=16,o=6,b=200:c,e,g";

// Command buffers (larger for RTTTL strings)
#define CMD_BUF_SIZE 512
static char cmd_buf[CMD_BUF_SIZE];
static uint16_t cmd_len = 0;
static char ble_cmd_buf[CMD_BUF_SIZE];
static uint16_t ble_cmd_len = 0;

// Forward declarations
static void handle_command(char* cmd, Stream* reply);
static void cmd_ping(Stream* reply);
static void cmd_tone(const char* args, Stream* reply);
static void cmd_stop(Stream* reply);
static void cmd_rtttl(const char* args, Stream* reply);
static void cmd_status(Stream* reply);
static void setup_ble();
static void disable_peripherals();
static void buzzer_on();
static void buzzer_off();

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

  Serial.begin(115200);
  delay(1000);  // longer delay for USB CDC enumeration

  setup_ble();

  Serial.println("+READY BuzzerBoard v" FIRMWARE_VERSION);

  // Play startup melody AFTER serial is up
  rtttl::begin(BUZZER_PIN, STARTUP_MELODY);
  while (!rtttl::done()) rtttl::play();
  buzzer_off();
}

// --- BLE setup ---

static void setup_ble() {
  Bluefruit.begin();
  Bluefruit.setTxPower(4);
  Bluefruit.setName(BLE_DEVICE_NAME);

  // Security: static PIN 1812, MITM protection
  char pin_str[8];
  snprintf(pin_str, sizeof(pin_str), "%lu", (unsigned long)BLE_PIN_CODE);
  Bluefruit.Security.setMITM(true);
  Bluefruit.Security.setPIN(pin_str);
  Bluefruit.Security.setIOCaps(true, false, false);  // display only

  // NUS (Nordic UART Service)
  bleuart.setPermission(SECMODE_ENC_WITH_MITM, SECMODE_ENC_WITH_MITM);
  bleuart.begin();

  // Advertising
  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addTxPower();
  Bluefruit.Advertising.addService(bleuart);
  Bluefruit.ScanResponse.addName();
  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.start(0);
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
}

// --- Command dispatch ---

static void handle_command(char* cmd, Stream* reply) {
  // Skip leading whitespace
  while (*cmd == ' ') cmd++;

  if (strncasecmp(cmd, "PING", 4) == 0) {
    cmd_ping(reply);
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

// --- Buzzer helpers ---

static void buzzer_on() {
  digitalWrite(BUZZER_EN, HIGH);
}

static void buzzer_off() {
  digitalWrite(BUZZER_EN, LOW);
  digitalWrite(BUZZER_PIN, LOW);
}
