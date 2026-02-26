/*
 * T1000-E Low-Power Test Firmware
 *
 * Standalone firmware to measure baseline sleep current of the
 * Seeed SenseCAP T1000-E (nRF52840 + LR1110) with all peripherals disabled.
 *
 * Wakes every 5 minutes to log battery voltage to RAM, then sleeps.
 * When USB is plugged in, auto-dumps the log over Serial and accepts commands.
 *
 * Button hold (5s) → shutdown melody → System OFF (wake on button press).
 *
 * No MeshCore, no LoRa, no BLE, no GPS.
 */

#include <Arduino.h>
#include <Adafruit_TinyUSB.h>
#include <NonBlockingRtttl.h>
#include "variant.h"

#define FIRMWARE_VERSION "0.1.0"

// --- Melodies (RTTTL) ---

static const char STARTUP_MELODY[]  = "Startup:d=4,o=5,b=160:16c6,16e6,8g6";
static const char SHUTDOWN_MELODY[] = "Shutdown:d=4,o=5,b=100:8g5,16e5,16c5";

// --- Log storage (circular buffer in RAM) ---

struct LogEntry {
  uint32_t timestamp_sec;  // seconds since boot
  uint16_t millivolts;     // battery voltage
};

#define MAX_LOG_ENTRIES 2048  // ~12KB, covers ~7 days at 5-min intervals
static LogEntry log_data[MAX_LOG_ENTRIES];
static uint16_t log_count = 0;
static uint16_t log_wrap_count = 0;

// --- Timing ---

#define SAMPLE_INTERVAL_MS  (5UL * 60UL * 1000UL)  // 5 minutes
#define SLEEP_CHUNK_MS      60000UL                  // 60s chunks for USB detection
#define BUTTON_HOLD_MS      5000UL                   // 5s hold to shut down
static unsigned long last_sample_ms = 0;

// --- Button state ---

static unsigned long button_press_start = 0;

// --- Forward declarations ---

static uint16_t read_battery_mv();
static void log_sample();
static void disable_peripherals();
static void handle_serial();
static void check_button();
static void do_shutdown();
static void cmd_dump();
static void cmd_clear();
static void cmd_status();
static void cmd_sample();
static void cmd_help();
static void led_rapid_blink(uint16_t duration_ms);
static bool usb_present();
static void buzzer_play(const char* melody);
static void buzzer_wait();
static void buzzer_off();

// --- Setup ---

void setup() {
  disable_peripherals();

  // Enable DC/DC converter for efficiency
  NRF_POWER->DCDCEN = 1;

  // Init button
  pinMode(BUTTON_PIN, INPUT);

  // Init buzzer and play startup melody
  pinMode(BUZZER_EN, OUTPUT);
  digitalWrite(BUZZER_EN, HIGH);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);
  buzzer_play(STARTUP_MELODY);
  buzzer_wait();
  buzzer_off();

  Serial.begin(115200);
  delay(500);  // allow serial to settle

  // Boot banner
  uint16_t mv = read_battery_mv();
  Serial.println();
  Serial.println("=== T1000-E Power Test ===");
  Serial.print("Firmware: v");
  Serial.println(FIRMWARE_VERSION);
  Serial.print("Battery:  ");
  Serial.print(mv);
  Serial.println(" mV");
  Serial.print("Max log:  ");
  Serial.print(MAX_LOG_ENTRIES);
  Serial.println(" entries");
  Serial.println("Button hold 5s to shut down");
  Serial.println("==========================");
  Serial.println();

  // Log first sample
  log_sample();
  last_sample_ms = millis();

  // Confirm boot with rapid LED blink
  led_rapid_blink(1000);
}

// --- Main loop ---

void loop() {
  // Always check for button hold shutdown
  check_button();

  // Advance any playing melody
  if (!rtttl::done()) rtttl::play();

  if (usb_present()) {
    // Interactive mode: handle serial commands
    handle_serial();
    delay(100);
  } else {
    // Sleep mode: sample every 5 minutes
    unsigned long now = millis();
    if (now - last_sample_ms >= SAMPLE_INTERVAL_MS) {
      log_sample();
      last_sample_ms = now;
      led_rapid_blink(1000);
    }
    // Sleep in 60s chunks (FreeRTOS tickless idle)
    // Shorter chunks allow faster USB detection and button response
    unsigned long remaining = SAMPLE_INTERVAL_MS - (millis() - last_sample_ms);
    unsigned long sleep_ms = min(remaining, SLEEP_CHUNK_MS);
    if (sleep_ms > 100) {
      delay(sleep_ms);
    }
  }
}

// --- Button hold detection ---

static void check_button() {
  if (digitalRead(BUTTON_PIN) == HIGH) {
    if (button_press_start == 0) {
      button_press_start = millis();
    } else if (millis() - button_press_start >= BUTTON_HOLD_MS) {
      do_shutdown();
    }
  } else {
    button_press_start = 0;
  }
}

// --- Shutdown sequence ---

static void do_shutdown() {
  Serial.println("Shutting down...");

  // Play shutdown melody
  digitalWrite(BUZZER_EN, HIGH);
  buzzer_play(SHUTDOWN_MELODY);
  buzzer_wait();
  buzzer_off();

  // LED on while waiting for button release
  digitalWrite(LED_PIN, HIGH);
  while (digitalRead(BUTTON_PIN) == HIGH) {
    delay(10);
  }
  digitalWrite(LED_PIN, LOW);

  // Disable everything
  disable_peripherals();

  // Configure button as wake source, then power off
  nrf_gpio_cfg_sense_input(g_ADigitalPinMap[BUTTON_PIN],
                           NRF_GPIO_PIN_NOPULL, NRF_GPIO_PIN_SENSE_HIGH);
  sd_power_system_off();
}

// --- Peripheral shutdown ---

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

  // Buzzer off
  pinMode(BUZZER_EN, OUTPUT);
  digitalWrite(BUZZER_EN, LOW);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);

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

  // LR1110 radio: deselect and hold in reset to minimize leakage
  pinMode(LORA_NSS, OUTPUT);
  pinMode(LORA_RESET, OUTPUT);
  digitalWrite(LORA_NSS, HIGH);   // deselect
  digitalWrite(LORA_RESET, LOW);  // hold in reset
}

// --- Battery reading (mirrors T1000eBoard::getBattMilliVolts) ---

static uint16_t read_battery_mv() {
  digitalWrite(PIN_3V3_EN, HIGH);
  analogReference(AR_INTERNAL_3_0);
  analogReadResolution(12);
  delay(10);  // settling time

  float volts = (analogRead(BATTERY_PIN) * ADC_MULTIPLIER * AREF_VOLTAGE) / 4096.0f;

  digitalWrite(PIN_3V3_EN, LOW);
  analogReference(AR_DEFAULT);
  analogReadResolution(10);

  return (uint16_t)(volts * 1000.0f);
}

// --- Logging ---

static void log_sample() {
  uint16_t mv = read_battery_mv();
  uint32_t ts = millis() / 1000;

  uint16_t idx;
  if (log_count < MAX_LOG_ENTRIES) {
    idx = log_count;
    log_count++;
  } else {
    // Circular buffer: overwrite oldest
    idx = log_wrap_count % MAX_LOG_ENTRIES;
    log_wrap_count++;
  }

  log_data[idx].timestamp_sec = ts;
  log_data[idx].millivolts = mv;
}

// --- USB detection ---

static bool usb_present() {
  return (NRF_POWER->USBREGSTATUS & POWER_USBREGSTATUS_VBUSDETECT_Msk) != 0;
}

// --- Buzzer helpers ---

static void buzzer_play(const char* melody) {
  if (rtttl::isPlaying()) rtttl::stop();
  rtttl::begin(BUZZER_PIN, melody);
}

static void buzzer_wait() {
  while (!rtttl::done()) rtttl::play();
}

static void buzzer_off() {
  rtttl::stop();
  digitalWrite(BUZZER_EN, LOW);
  digitalWrite(BUZZER_PIN, LOW);
}

// --- Serial command handling ---

static char cmd_buf[64];
static uint8_t cmd_pos = 0;

static void handle_serial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmd_pos > 0) {
        cmd_buf[cmd_pos] = '\0';
        String cmd = String(cmd_buf);
        cmd.trim();

        if (cmd == "dump")        cmd_dump();
        else if (cmd == "clear")  cmd_clear();
        else if (cmd == "status") cmd_status();
        else if (cmd == "sample") cmd_sample();
        else if (cmd == "help")   cmd_help();
        else {
          Serial.print("Unknown command: ");
          Serial.println(cmd);
          cmd_help();
        }
        cmd_pos = 0;
      }
    } else if (cmd_pos < sizeof(cmd_buf) - 1) {
      cmd_buf[cmd_pos++] = c;
    }
  }
}

// --- Commands ---

static void cmd_dump() {
  uint16_t total = log_count;
  if (log_wrap_count > 0) {
    total = MAX_LOG_ENTRIES;
  }

  Serial.println("seconds,millivolts");

  if (log_wrap_count > 0) {
    // Buffer has wrapped: print from oldest to newest
    uint16_t start = log_wrap_count % MAX_LOG_ENTRIES;
    for (uint16_t i = 0; i < MAX_LOG_ENTRIES; i++) {
      uint16_t idx = (start + i) % MAX_LOG_ENTRIES;
      Serial.print(log_data[idx].timestamp_sec);
      Serial.print(",");
      Serial.println(log_data[idx].millivolts);
    }
  } else {
    for (uint16_t i = 0; i < log_count; i++) {
      Serial.print(log_data[i].timestamp_sec);
      Serial.print(",");
      Serial.println(log_data[i].millivolts);
    }
  }

  Serial.print("# ");
  Serial.print(total);
  Serial.print(" entries");
  if (log_wrap_count > 0) {
    Serial.print(" (wrapped ");
    Serial.print(log_wrap_count);
    Serial.print(" times)");
  }
  Serial.println();
}

static void cmd_clear() {
  log_count = 0;
  log_wrap_count = 0;
  Serial.println("Log cleared.");
}

static void cmd_status() {
  uint32_t uptime = millis() / 1000;
  uint32_t hours = uptime / 3600;
  uint32_t mins = (uptime % 3600) / 60;
  uint32_t secs = uptime % 60;

  Serial.print("Uptime:    ");
  Serial.print(hours);
  Serial.print("h ");
  Serial.print(mins);
  Serial.print("m ");
  Serial.print(secs);
  Serial.println("s");

  Serial.print("Entries:   ");
  Serial.print(log_count);
  Serial.print(" / ");
  Serial.println(MAX_LOG_ENTRIES);

  if (log_wrap_count > 0) {
    Serial.print("Wraps:     ");
    Serial.println(log_wrap_count);
  }

  Serial.print("Battery:   ");
  Serial.print(read_battery_mv());
  Serial.println(" mV");

  Serial.print("Free RAM:  ");
  Serial.print(dbgHeapTotal() - dbgHeapUsed());
  Serial.println(" bytes");

  Serial.print("USB:       ");
  Serial.println(usb_present() ? "connected" : "not connected");
}

static void cmd_sample() {
  log_sample();
  uint16_t idx = (log_count > 0) ? log_count - 1 : 0;
  if (log_wrap_count > 0) {
    idx = (log_wrap_count - 1) % MAX_LOG_ENTRIES;
  }
  Serial.print("Sampled: ");
  Serial.print(log_data[idx].millivolts);
  Serial.print(" mV at ");
  Serial.print(log_data[idx].timestamp_sec);
  Serial.println("s");
}

static void cmd_help() {
  Serial.println("Commands:");
  Serial.println("  dump   - print full log as CSV");
  Serial.println("  clear  - reset log buffer");
  Serial.println("  status - show uptime, entries, battery, free RAM");
  Serial.println("  sample - take immediate reading");
  Serial.println("  help   - show this help");
}

// --- Utility ---

static void led_rapid_blink(uint16_t duration_ms) {
  unsigned long start = millis();
  while (millis() - start < duration_ms) {
    digitalWrite(LED_PIN, HIGH);
    delay(50);
    digitalWrite(LED_PIN, LOW);
    delay(50);
  }
}
