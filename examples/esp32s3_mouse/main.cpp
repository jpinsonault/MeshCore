// ESP32-S3 BLE Mouse with 6-axis IMU
//
// Waveshare ESP32-S3-Touch-LCD-1.28 as a BLE HID air mouse.
// QMI8658C IMU provides accel + gyro for proper sensor fusion.
// Gyro tracks fast rotation (no sensitivity dropoff at any angle),
// accel corrects drift over time via complementary filter.
//
// Controls:
//   Single click              → Left Click
//   Double click              → Right Click
//   Hold 400ms + tilt         → Move cursor (gyro-based, no dead angles)
//   Double-press + hold 3s    → Power off (deep sleep)
//   Triple-press + hold 3s    → Pairing mode (clear bonds)
//
// Hardware: ESP32-S3 + QMI8658C (I2C) + GC9A01 LCD (SPI)
// BLE HID via ESP32 NimBLE stack.

#include <Arduino.h>
#include <Wire.h>
#include <BleMouse.h>

#include "qmi8658c.h"

// -- Waveshare ESP32-S3-Touch-LCD-1.28 pins --
#define PIN_IMU_SDA    6
#define PIN_IMU_SCL    7
#define PIN_IMU_INT1   4
#define PIN_IMU_INT2   3
#define PIN_LCD_BL     2
#define PIN_BUTTON     0   // BOOT button (active LOW on ESP32-S3)

// -- BLE HID --
BleMouse bleMouse("ESP32-S3 Mouse", "Waveshare", 100);

// -- Button debounce (lockout) --
static uint8_t  btn_db_state = HIGH;  // active LOW on ESP32-S3
static uint32_t btn_db_lock  = 0;

// -- Button FSM --
enum BtnFSM { BTN_IDLE, BTN_DOWN, BTN_UP, BTN_GESTURE };
static BtnFSM   btn_fsm     = BTN_IDLE;
static uint8_t  btn_clicks   = 0;
static uint32_t btn_press_t  = 0;
static uint32_t btn_rel_t    = 0;

#define DEBOUNCE_MS    10
#define CLICK_WINDOW   300
#define LONGPRESS_MS   400
#define HOLD_3S_MS     3000

// -- Complementary filter state --
// Fuses gyro (fast, drifts) with accel (slow, stable) into clean orientation.
// Output: pitch and roll angles in radians.
#define COMP_ALPHA     0.98f   // gyro weight (0.98 = 98% gyro, 2% accel correction)
static float filt_pitch = 0;   // radians
static float filt_roll  = 0;   // radians
static bool  filt_init  = false;
static uint32_t filt_last_us = 0;

// -- Mouse gesture --
#define SCREEN_HALF_W    960.0f
#define SCREEN_HALF_H    540.0f
#define TILT_MAX_DEG_X   27.0f   // degrees of roll for full horizontal
#define TILT_MAX_DEG_Y   15.0f   // degrees of pitch for full vertical
#define DEG_TO_RAD       (3.14159f / 180.0f)
#define RAD_TO_DEG       (180.0f / 3.14159f)

static bool  mouse_active = false;
static float mouse_ref_pitch = 0;
static float mouse_ref_roll  = 0;
static float mouse_sent_x = 0, mouse_sent_y = 0;

// -- Connection tracking --
static bool ble_connected  = false;
static bool pairing_mode   = false;

// -- Debug --
static uint32_t debug_last = 0;
#define DEBUG_INTERVAL_MS 2000

// ===================== Debug log =====================

static char _logbuf[128];

#define log(fmt, ...) do { \
  snprintf(_logbuf, sizeof(_logbuf), fmt "\n", ##__VA_ARGS__); \
  Serial.print(_logbuf); \
} while(0)

// ===================== Smoothstep response curve =====================

static float smoothstep_signed(float x) {
  float s = (x >= 0) ? 1.0f : -1.0f;
  float t = fabsf(x);
  if (t <= 1.0f) {
    return s * t * t * (3.0f - 2.0f * t);
  }
  return s * (1.0f + (t - 1.0f) * 0.3f);
}

// ===================== Complementary filter =====================

static void update_imu() {
  qmi8658c::IMUData d = qmi8658c::read();

  uint32_t now_us = micros();
  if (!filt_init) {
    // Initialize from accel (gravity gives us initial pitch/roll)
    filt_pitch = atan2f(-d.accel.x, sqrtf(d.accel.y * d.accel.y + d.accel.z * d.accel.z));
    filt_roll  = atan2f(d.accel.y, d.accel.z);
    filt_init = true;
    filt_last_us = now_us;
    return;
  }

  float dt = (now_us - filt_last_us) * 1e-6f;
  filt_last_us = now_us;
  if (dt <= 0 || dt > 0.1f) return;  // skip bad dt

  // Gyro integration (degrees/sec → radians)
  float gyro_pitch = d.gyro.x * DEG_TO_RAD;  // rotation around X = pitch change
  float gyro_roll  = d.gyro.y * DEG_TO_RAD;  // rotation around Y = roll change

  // Accel-derived angles (stable reference)
  float accel_pitch = atan2f(-d.accel.x, sqrtf(d.accel.y * d.accel.y + d.accel.z * d.accel.z));
  float accel_roll  = atan2f(d.accel.y, d.accel.z);

  // Complementary filter: trust gyro for fast changes, accel corrects drift
  filt_pitch = COMP_ALPHA * (filt_pitch + gyro_pitch * dt) + (1.0f - COMP_ALPHA) * accel_pitch;
  filt_roll  = COMP_ALPHA * (filt_roll  + gyro_roll  * dt) + (1.0f - COMP_ALPHA) * accel_roll;
}

// ===================== Power off =====================

static void do_power_off() {
  log("POWER OFF");
  // Turn off LCD backlight
  digitalWrite(PIN_LCD_BL, LOW);
  delay(100);

  // Wait for button release
  while (digitalRead(PIN_BUTTON) == LOW) delay(10);
  delay(100);

  // Configure button as wake source (active LOW)
  esp_sleep_enable_ext0_wakeup((gpio_num_t)PIN_BUTTON, 0);
  esp_deep_sleep_start();
}

// ===================== Pairing mode =====================

static void do_enter_pairing() {
  log("PAIRING MODE");
  pairing_mode = true;
  // ESP32 BLE bonding clear would go here
  // For now, just restart advertising
}

// ===================== Button FSM =====================

static void update_button() {
  uint32_t now = millis();
  uint8_t raw = digitalRead(PIN_BUTTON);

  // Lockout debounce (button is active LOW on ESP32-S3)
  bool pressed = false, released = false;
  if (raw != btn_db_state && now - btn_db_lock >= DEBOUNCE_MS) {
    btn_db_state = raw;
    btn_db_lock = now;
    if (raw == LOW) pressed = true;
    else released = true;
  }

  switch (btn_fsm) {
    case BTN_IDLE:
      if (pressed) {
        btn_clicks = 0;
        btn_press_t = now;
        btn_fsm = BTN_DOWN;
      }
      break;

    case BTN_DOWN:
      if (released) {
        btn_clicks++;
        btn_rel_t = now;
        btn_fsm = BTN_UP;
      } else if (btn_clicks == 0 && now - btn_press_t > LONGPRESS_MS) {
        // First press held → mouse gesture
        mouse_active = true;
        mouse_ref_pitch = filt_pitch;
        mouse_ref_roll  = filt_roll;
        mouse_sent_x = 0;
        mouse_sent_y = 0;
        btn_fsm = BTN_GESTURE;
        log("MOUSE enter (pitch=%.1f roll=%.1f)",
            mouse_ref_pitch * RAD_TO_DEG, mouse_ref_roll * RAD_TO_DEG);
      } else if (btn_clicks >= 1) {
        if (btn_clicks == 1 && now - btn_press_t > HOLD_3S_MS) {
          do_power_off();
        } else if (btn_clicks == 2 && now - btn_press_t > HOLD_3S_MS) {
          do_enter_pairing();
          btn_fsm = BTN_IDLE;
        }
      }
      break;

    case BTN_UP:
      if (pressed) {
        btn_press_t = now;
        btn_fsm = BTN_DOWN;
      } else if (now - btn_rel_t > CLICK_WINDOW) {
        if (btn_clicks == 1) {
          if (ble_connected) {
            log("ACTION left_click");
            bleMouse.click(MOUSE_LEFT);
          }
        } else if (btn_clicks == 2) {
          if (ble_connected) {
            log("ACTION right_click");
            bleMouse.click(MOUSE_RIGHT);
          }
        }
        btn_fsm = BTN_IDLE;
      }
      break;

    case BTN_GESTURE:
      if (released) {
        mouse_active = false;
        log("MOUSE exit");
        btn_fsm = BTN_IDLE;
      }
      break;
  }
}

// ===================== Mouse gesture =====================

static void update_mouse() {
  if (!mouse_active || !ble_connected) return;

  // Delta from reference in degrees
  float d_roll_deg  = (filt_roll  - mouse_ref_roll)  * RAD_TO_DEG;
  float d_pitch_deg = (filt_pitch - mouse_ref_pitch) * RAD_TO_DEG;

  // Smoothstep + scale to virtual screen
  float pos_x =  smoothstep_signed(d_roll_deg  / TILT_MAX_DEG_X) * SCREEN_HALF_W;
  float pos_y = -smoothstep_signed(d_pitch_deg / TILT_MAX_DEG_Y) * SCREEN_HALF_H;

  // Delta from last sent position
  float dx_f = pos_x - mouse_sent_x;
  float dy_f = pos_y - mouse_sent_y;

  int raw_dx = (int)dx_f;
  int raw_dy = (int)dy_f;
  if (raw_dx > 127) raw_dx = 127;
  if (raw_dx < -127) raw_dx = -127;
  if (raw_dy > 127) raw_dy = 127;
  if (raw_dy < -127) raw_dy = -127;

  int8_t dx = (int8_t)raw_dx;
  int8_t dy = (int8_t)raw_dy;

  if (dx != 0 || dy != 0) {
    bleMouse.move(dx, dy);
    mouse_sent_x += dx;
    mouse_sent_y += dy;
  }
}

// ===================== Arduino entry points =====================

void setup() {
  Serial.begin(115200);
  delay(500);

  // LCD backlight on
  pinMode(PIN_LCD_BL, OUTPUT);
  digitalWrite(PIN_LCD_BL, HIGH);

  // Button (active LOW, has internal pull-up on ESP32-S3 BOOT pin)
  pinMode(PIN_BUTTON, INPUT_PULLUP);

  // I2C for IMU
  Wire.begin(PIN_IMU_SDA, PIN_IMU_SCL);
  Wire.setClock(400000);

  bool imu_ok = qmi8658c::init();
  log("=== ESP32-S3 BLE Mouse ===");
  log("IMU: %s (ID=0x%02X)", imu_ok ? "OK" : "FAIL", qmi8658c::readReg(0x00));

  // BLE HID
  bleMouse.begin();
  log("Advertising as 'ESP32-S3 Mouse'...");
}

void loop() {
  // Update IMU + complementary filter
  update_imu();

  // Track BLE connection
  ble_connected = bleMouse.isConnected();

  update_button();
  update_mouse();

  // Periodic debug
  uint32_t now = millis();
  if (now - debug_last >= DEBUG_INTERVAL_MS) {
    debug_last = now;
    log("pitch=%.1f roll=%.1f %s",
        filt_pitch * RAD_TO_DEG, filt_roll * RAD_TO_DEG,
        ble_connected ? "BLE" : "ADV");
  }

  delay(4);  // ~250Hz loop to match IMU ODR
}
