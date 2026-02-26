/*
 * BuzzerBoard — Minimal Buzzer Firmware for Seeed SenseCAP T1000-E
 *
 * No LoRa, no BLE, no GPS, no mesh.
 * Accepts TONE and RTTTL commands over USB serial.
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
#include <NonBlockingRtttl.h>
#include "variant.h"

#define FIRMWARE_VERSION "0.1.0"

// Startup melody
static const char STARTUP_MELODY[] = "BB:d=16,o=6,b=200:c,e,g";

// Serial command buffer (larger for RTTTL strings)
#define CMD_BUF_SIZE 512
static char cmd_buf[CMD_BUF_SIZE];
static uint16_t cmd_len = 0;

// Forward declarations
static void handle_command(char* cmd);
static void cmd_ping();
static void cmd_tone(const char* args);
static void cmd_stop();
static void cmd_rtttl(const char* args);
static void cmd_status();
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

  Serial.println("+READY BuzzerBoard v" FIRMWARE_VERSION);

  // Play startup melody AFTER serial is up
  rtttl::begin(BUZZER_PIN, STARTUP_MELODY);
  while (!rtttl::done()) rtttl::play();
  buzzer_off();
}

// --- Main loop ---

void loop() {
  // Advance RTTTL if playing
  if (!rtttl::done()) rtttl::play();

  // Non-blocking serial read
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r' || c == '\n') {
      if (cmd_len > 0) {
        cmd_buf[cmd_len] = '\0';
        handle_command(cmd_buf);
        cmd_len = 0;
      }
    } else if (cmd_len < CMD_BUF_SIZE - 1) {
      cmd_buf[cmd_len++] = c;
    }
  }
}

// --- Command dispatch ---

static void handle_command(char* cmd) {
  // Skip leading whitespace
  while (*cmd == ' ') cmd++;

  if (strncasecmp(cmd, "PING", 4) == 0) {
    cmd_ping();
  } else if (strncasecmp(cmd, "TONE ", 5) == 0) {
    cmd_tone(cmd + 5);
  } else if (strncasecmp(cmd, "STOP", 4) == 0) {
    cmd_stop();
  } else if (strncasecmp(cmd, "RTTTL ", 6) == 0) {
    cmd_rtttl(cmd + 6);
  } else if (strncasecmp(cmd, "STATUS", 6) == 0) {
    cmd_status();
  } else {
    Serial.print("+ERR Unknown command: ");
    Serial.println(cmd);
  }
}

// --- Commands ---

static void cmd_ping() {
  Serial.println("+PONG");
}

static void cmd_tone(const char* args) {
  int freq = 0, dur = 0;
  if (sscanf(args, "%d %d", &freq, &dur) != 2 || freq < 20 || freq > 20000 || dur < 1) {
    Serial.println("+ERR Invalid TONE args");
    return;
  }
  // Stop any RTTTL playback first
  if (!rtttl::done()) rtttl::stop();
  // Enable buzzer gate and play
  buzzer_on();
  tone(BUZZER_PIN, freq, dur);
  Serial.println("+OK");
}

static void cmd_stop() {
  if (!rtttl::done()) rtttl::stop();
  noTone(BUZZER_PIN);
  buzzer_off();
  Serial.println("+OK");
}

static void cmd_rtttl(const char* args) {
  if (!args || strlen(args) < 5) {
    Serial.println("+ERR Empty RTTTL");
    return;
  }
  // Copy to static buffer since rtttl::begin reads from the pointer over time
  static char rtttl_buf[CMD_BUF_SIZE];
  strncpy(rtttl_buf, args, CMD_BUF_SIZE - 1);
  rtttl_buf[CMD_BUF_SIZE - 1] = '\0';

  buzzer_on();
  rtttl::begin(BUZZER_PIN, rtttl_buf);
  Serial.println("+OK PLAYING");
}

static void cmd_status() {
  Serial.print("+STATUS playing=");
  Serial.print(rtttl::done() ? "0" : "1");
  Serial.println(" buzzer=on");
}

// --- Buzzer helpers ---

static void buzzer_on() {
  digitalWrite(BUZZER_EN, HIGH);
}

static void buzzer_off() {
  digitalWrite(BUZZER_EN, LOW);
  digitalWrite(BUZZER_PIN, LOW);
}
