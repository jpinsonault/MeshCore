// T1000-E BLE Mouse
//
// BLE HID mouse for Seeed SenseCAP T1000-E.
// Advertises as a standard Bluetooth mouse — any phone/laptop/tablet
// can pair from normal Bluetooth settings (no app needed).
//
// Controls:
//   Single click              → Left Click
//   Double click              → Right Click
//   Hold 400ms + tilt         → Move cursor (tilt-to-move with power curve)
//   Double-press + hold 3s    → Power off (deep sleep, wake on button)
//   Triple-press + hold 3s    → Pairing mode (clear bonds, re-advertise)

#include <Arduino.h>
#include <bluefruit.h>
#include <Wire.h>
#include "nrf_gpio.h"

#include "variant.h"
#include "qma6100p.h"

// -- T1000-E pins --
#define PIN_LED     24  // Green LED, active HIGH
#define PIN_BUTTON  6   // User button, active HIGH when pressed
#define PIN_BZR     25  // Buzzer signal
#define PIN_BZR_EN  37  // Buzzer enable gate (HIGH = enabled)

// -- BLE objects --
BLEHidAdafruit blehid;
BLEDis         bledis;
BLEUart        bleuart;

// -- Tiny non-blocking melody player (no octave limits, unlike RTTTL) --
// Each note: {frequency_hz, duration_ms}. freq=0 → pause.
struct MelNote { uint16_t freq; uint16_t dur; };
struct Melody  { const MelNote* notes; uint8_t len; };

static const MelNote* mel_notes = nullptr;
static uint8_t  mel_len = 0;
static uint8_t  mel_idx = 0;
static uint32_t mel_end = 0;

static void mel_play() {
  if (!mel_notes) return;
  if (millis() < mel_end) return;
  if (mel_idx >= mel_len) {
    noTone(PIN_BZR);
    mel_notes = nullptr;
    return;
  }
  noTone(PIN_BZR);
  const MelNote& n = mel_notes[mel_idx++];
  if (n.freq > 0) tone(PIN_BZR, n.freq, n.dur);
  mel_end = millis() + n.dur;
}
static bool mel_done() { return mel_notes == nullptr; }

// -- Tone system --
#define BASE_FREQ    82   // Hz — floor for click pitch
#define MID_FREQ     (BASE_FREQ + 5)
#define TOP_FREQ     (BASE_FREQ + 11)
#define NOTE_DUR     123  // ms — standard note (12+ cycles at 82Hz)

static const MelNote N_STARTUP[]  = {{BASE_FREQ,NOTE_DUR},{BASE_FREQ,NOTE_DUR},{MID_FREQ,NOTE_DUR}};
static const MelNote N_CONNECT[]  = {{BASE_FREQ,NOTE_DUR},{MID_FREQ,NOTE_DUR}};
static const MelNote N_CLICK[]    = {{BASE_FREQ,NOTE_DUR}};
static const MelNote N_DBLCLICK[] = {{BASE_FREQ,NOTE_DUR},{0,50},{BASE_FREQ,NOTE_DUR}};
static const MelNote N_POWER_OFF[]= {{MID_FREQ,150},{BASE_FREQ,150},{BASE_FREQ,150},{BASE_FREQ,600}};
static const MelNote N_PAIRING[]  = {{MID_FREQ,188},{BASE_FREQ,188},{BASE_FREQ,188},{BASE_FREQ,750}};
static const MelNote N_NOT_CONN[] = {{MID_FREQ,NOTE_DUR},{BASE_FREQ,NOTE_DUR}};
static const MelNote N_HOLD[]     = {{BASE_FREQ,NOTE_DUR},{BASE_FREQ,NOTE_DUR},{MID_FREQ,NOTE_DUR},{MID_FREQ,NOTE_DUR},{MID_FREQ,NOTE_DUR}};

static const Melody M_STARTUP   = {N_STARTUP,   3};
static const Melody M_CONNECT   = {N_CONNECT,   2};
static const Melody M_CLICK     = {N_CLICK,     1};
static const Melody M_DBLCLICK  = {N_DBLCLICK,  3};
static const Melody M_POWER_OFF = {N_POWER_OFF, 4};
static const Melody M_PAIRING   = {N_PAIRING,   4};
static const Melody M_NOT_CONN  = {N_NOT_CONN,  2};
static const Melody M_HOLD      = {N_HOLD,      5};

// -- Button debounce (lockout: accept first edge, ignore bounces for N ms) --
static uint8_t  btn_db_state = LOW;
static uint32_t btn_db_lock  = 0;

// -- Button FSM --
enum BtnFSM { BTN_IDLE, BTN_DOWN, BTN_UP, BTN_GESTURE };
static BtnFSM   btn_fsm     = BTN_IDLE;
static uint8_t  btn_clicks   = 0;
static uint32_t btn_press_t  = 0;
static uint32_t btn_rel_t    = 0;
static uint8_t  btn_hold_step = 0;

#define DEBOUNCE_MS    10
#define CLICK_WINDOW   300
#define LONGPRESS_MS   400
#define HOLD_3S_MS     3000

#define HOLD_SCALE_LEN   5
#define HOLD_SCALE_START 500
#define HOLD_SCALE_STEP  500

// -- 2-pole filtered accelerometer (offset-corrected) --
// Two cascaded EMAs ≈ 2nd-order Butterworth. Each pole at α=0.12 gives
// ~2.5Hz effective cutoff — kills hand tremor (8-12Hz) at the source.
// Adds ~60ms group delay, acceptable for a pointing device.
#define ACCEL_OFFSET_X  (-0.155f)
#define ACCEL_OFFSET_Y  (-0.805f)
#define ACCEL_OFFSET_Z  (1.055f)
#define LP_ALPHA  0.12f
// Pole 1
static float lp1_gx = 0, lp1_gy = 0, lp1_gz = 0;
// Pole 2 (output)
static float filt_gx = 0, filt_gy = 0, filt_gz = 0;
static bool  filt_init = false;

// -- Mouse gesture (g-delta model) --
// Map raw gravity deltas on device body axes to screen position.
// Offsets cancel in the subtraction, making this robust to calibration errors.
// Smoothstep response curve: compresses center AND edges.
#define SCREEN_HALF_W    960.0f
#define SCREEN_HALF_H    540.0f
#define TILT_MAX_G_X     0.45f   // left/right: full range at ~26.6° tilt
#define TILT_MAX_G_Y     0.25f   // forward/back: less wrist range available

static bool  mouse_active = false;
static float mouse_ref_gx = 0, mouse_ref_gy = 0;
static float mouse_sent_x = 0, mouse_sent_y = 0;

// -- LED state --
static uint32_t led_last       = 0;
static bool     led_on         = false;
static uint8_t  led_blink_cnt  = 0;

// -- Connection tracking --
static bool     ble_connected  = false;
static bool     pairing_mode   = false;
static uint32_t connect_t      = 0;
static bool     connect_beeped = false;

// -- Debug --
static uint32_t debug_last     = 0;
#define DEBUG_INTERVAL_MS  2000

// ===================== Debug log (Serial + BLE UART) =====================

static char _logbuf[128];

#define log(fmt, ...) do { \
  snprintf(_logbuf, sizeof(_logbuf), fmt "\n", ##__VA_ARGS__); \
  Serial.print(_logbuf); \
  if (bleuart.notifyEnabled()) bleuart.print(_logbuf); \
} while(0)

// ===================== Peripheral shutdown =====================

static void disable_peripherals() {
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

  pinMode(PIN_3V3_EN, OUTPUT);
  pinMode(SENSOR_EN, OUTPUT);
  digitalWrite(PIN_3V3_EN, LOW);
  digitalWrite(SENSOR_EN, LOW);

  pinMode(LORA_NSS, OUTPUT);
  pinMode(LORA_RESET, OUTPUT);
  digitalWrite(LORA_NSS, HIGH);
  digitalWrite(LORA_RESET, LOW);
}

// ===================== Buzzer helpers =====================

static void buzzer_on()  { digitalWrite(PIN_BZR_EN, HIGH); }
static void buzzer_off() { noTone(PIN_BZR); digitalWrite(PIN_BZR_EN, LOW); digitalWrite(PIN_BZR, LOW); }

static void beep(const Melody& m) {
  buzzer_on();
  mel_notes = m.notes;
  mel_len = m.len;
  mel_idx = 0;
  mel_end = 0;
}

static void play_freq(uint16_t freq_hz, uint16_t dur_ms) {
  buzzer_on();
  tone(PIN_BZR, freq_hz, dur_ms);
}

// ===================== HID mouse helpers =====================

static void hid_mouse_move(int8_t dx, int8_t dy) {
  for (uint16_t conn_hdl = 0; conn_hdl < BLE_MAX_CONNECTION; conn_hdl++) {
    BLEConnection* connection = Bluefruit.Connection(conn_hdl);
    if (connection && connection->connected() && connection->secured()) {
      blehid.mouseMove(conn_hdl, dx, dy);
    }
  }
}

static void hid_mouse_click(uint8_t button) {
  for (uint16_t conn_hdl = 0; conn_hdl < BLE_MAX_CONNECTION; conn_hdl++) {
    BLEConnection* connection = Bluefruit.Connection(conn_hdl);
    if (connection && connection->connected() && connection->secured()) {
      blehid.mouseButtonPress(conn_hdl, button);
      delay(10);
      blehid.mouseButtonRelease(conn_hdl);
    }
  }
}

// ===================== Power off (deep sleep) =====================

static void do_power_off() {
  log("POWER OFF");
  beep(M_POWER_OFF);
  while (!mel_done()) mel_play();
  buzzer_off();

  disable_peripherals();
  digitalWrite(PIN_3V3_ACC_EN, LOW);
  digitalWrite(PIN_LED, LOW);

  while (digitalRead(PIN_BUTTON) == HIGH) delay(10);
  delay(100);

  nrf_gpio_cfg_sense_input(g_ADigitalPinMap[PIN_BUTTON],
                           NRF_GPIO_PIN_NOPULL,
                           NRF_GPIO_PIN_SENSE_HIGH);
  sd_power_system_off();
}

// ===================== Pairing mode =====================

static void do_enter_pairing() {
  log("PAIRING MODE — bonds cleared");
  beep(M_PAIRING);

  Bluefruit.Advertising.stop();
  Bluefruit.Periph.clearBonds();

  for (uint16_t conn_hdl = 0; conn_hdl < BLE_MAX_CONNECTION; conn_hdl++) {
    BLEConnection* connection = Bluefruit.Connection(conn_hdl);
    if (connection && connection->connected()) {
      connection->disconnect();
    }
  }

  ble_connected = false;
  pairing_mode = true;
  Bluefruit.Advertising.start(0);
}

// ===================== BLE callbacks =====================

static void connect_callback(uint16_t conn_hdl) {
  (void)conn_hdl;
  ble_connected = true;
  connect_t = millis();
  connect_beeped = false;
  log("BLE connected (confirming...)");
}

static void disconnect_callback(uint16_t conn_hdl, uint8_t reason) {
  (void)conn_hdl;
  (void)reason;
  ble_connected = false;
  connect_beeped = false;
  log("BLE disconnected, reason=0x%02X", reason);
}

// ===================== BLE setup =====================

static void setup_ble() {
  Bluefruit.begin();
  Bluefruit.setTxPower(4);
  Bluefruit.setName("T1000-E Mouse");

  Bluefruit.Periph.setConnectCallback(connect_callback);
  Bluefruit.Periph.setDisconnectCallback(disconnect_callback);

  bledis.setManufacturer("Seeed");
  bledis.setModel("T1000-E BLE Mouse");
  bledis.begin();

  blehid.begin();
  bleuart.begin();

  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addTxPower();
  Bluefruit.Advertising.addAppearance(BLE_APPEARANCE_HID_MOUSE);
  Bluefruit.Advertising.addService(blehid);
  Bluefruit.ScanResponse.addName();
  Bluefruit.ScanResponse.addService(bleuart);

  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.setInterval(32, 244);
  Bluefruit.Advertising.setFastTimeout(30);
  Bluefruit.Advertising.start(0);
}

// ===================== Button FSM =====================

static void update_button() {
  uint32_t now = millis();
  uint8_t raw = digitalRead(PIN_BUTTON);

  bool pressed = false, released = false;
  if (raw != btn_db_state && now - btn_db_lock >= DEBOUNCE_MS) {
    btn_db_state = raw;
    btn_db_lock = now;
    if (raw == HIGH) pressed = true;
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
        mouse_ref_gx = filt_gx;
        mouse_ref_gy = filt_gy;
        mouse_sent_x = 0;
        mouse_sent_y = 0;
        btn_fsm = BTN_GESTURE;
        log("MOUSE enter (gx=%.2f gy=%.2f)", mouse_ref_gx, mouse_ref_gy);
      } else if (btn_clicks >= 1) {
        uint32_t held = now - btn_press_t;
        if (held >= HOLD_SCALE_START && btn_hold_step < HOLD_SCALE_LEN) {
          uint32_t next_at = HOLD_SCALE_START + (uint32_t)btn_hold_step * HOLD_SCALE_STEP;
          if (held >= next_at) {
            play_freq(M_HOLD.notes[btn_hold_step].freq, M_HOLD.notes[btn_hold_step].dur);
            btn_hold_step++;
          }
        }
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
        btn_hold_step = 0;
        btn_fsm = BTN_DOWN;
      } else if (now - btn_rel_t > CLICK_WINDOW) {
        if (btn_clicks == 1) {
          if (ble_connected) {
            log("ACTION left_click");
            hid_mouse_click(MOUSE_BUTTON_LEFT);
            beep(M_CLICK);
          } else {
            beep(M_NOT_CONN);
          }
        } else if (btn_clicks == 2) {
          if (ble_connected) {
            log("ACTION right_click");
            hid_mouse_click(MOUSE_BUTTON_RIGHT);
            beep(M_DBLCLICK);
          } else {
            beep(M_NOT_CONN);
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

// ===================== Mouse gesture (g-delta model) =====================

// Signed smoothstep with linear extension past 1.0.
// [0,1]: 3t²−2t³ (derivative 0 at both ends — compresses center & edge)
// >1.0: gentle linear continuation so there's no wall.
static float smoothstep_signed(float x) {
  float s = (x >= 0) ? 1.0f : -1.0f;
  float t = fabsf(x);
  if (t <= 1.0f) {
    return s * t * t * (3.0f - 2.0f * t);
  }
  return s * (1.0f + (t - 1.0f) * 0.3f);
}

static void update_mouse() {
  if (!mouse_active) return;

  // Gravity deltas on device body axes — offsets cancel in subtraction
  float dgy = filt_gy - mouse_ref_gy;  // left/right tilt → screen X
  float dgx = filt_gx - mouse_ref_gx;  // forward/back tilt → screen Y

  // Smoothstep + scale to virtual screen
  float pos_x =  smoothstep_signed(dgy / TILT_MAX_G_X) * SCREEN_HALF_W;
  float pos_y = -smoothstep_signed(dgx / TILT_MAX_G_Y) * SCREEN_HALF_H;

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
    hid_mouse_move(dx, dy);
    mouse_sent_x += dx;
    mouse_sent_y += dy;
  }
}

// ===================== LED patterns =====================

static void update_led() {
  uint32_t now = millis();

  if (mouse_active) {
    // Double-blink: quick on-off-on-off then gap
    uint32_t phase = (now - led_last);
    if (led_blink_cnt == 0 && phase > 300) {
      digitalWrite(PIN_LED, HIGH); led_on = true;
      led_blink_cnt = 1; led_last = now;
    } else if (led_blink_cnt == 1 && phase > 80) {
      digitalWrite(PIN_LED, LOW); led_on = false;
      led_blink_cnt = 2; led_last = now;
    } else if (led_blink_cnt == 2 && phase > 80) {
      digitalWrite(PIN_LED, HIGH); led_on = true;
      led_blink_cnt = 3; led_last = now;
    } else if (led_blink_cnt == 3 && phase > 80) {
      digitalWrite(PIN_LED, LOW); led_on = false;
      led_blink_cnt = 0; led_last = now;
    }
  } else if (pairing_mode) {
    // Triple-blink every 1.5s
    uint32_t phase = (now - led_last);
    if (led_blink_cnt == 0 && phase > 1500) {
      digitalWrite(PIN_LED, HIGH); led_on = true;
      led_blink_cnt = 1; led_last = now;
    } else if (led_blink_cnt == 1 && phase > 80) {
      digitalWrite(PIN_LED, LOW); led_on = false;
      led_blink_cnt = 2; led_last = now;
    } else if (led_blink_cnt == 2 && phase > 80) {
      digitalWrite(PIN_LED, HIGH); led_on = true;
      led_blink_cnt = 3; led_last = now;
    } else if (led_blink_cnt == 3 && phase > 80) {
      digitalWrite(PIN_LED, LOW); led_on = false;
      led_blink_cnt = 4; led_last = now;
    } else if (led_blink_cnt == 4 && phase > 80) {
      digitalWrite(PIN_LED, HIGH); led_on = true;
      led_blink_cnt = 5; led_last = now;
    } else if (led_blink_cnt == 5 && phase > 80) {
      digitalWrite(PIN_LED, LOW); led_on = false;
      led_blink_cnt = 0; led_last = now;
    }
  } else if (ble_connected) {
    if (!led_on) {
      digitalWrite(PIN_LED, HIGH);
      led_on = true;
    }
  } else {
    // Advertising: slow blink (100ms on, 2s off)
    if (led_on && now - led_last > 100) {
      digitalWrite(PIN_LED, LOW);
      led_on = false;
      led_last = now;
    } else if (!led_on && now - led_last > 2000) {
      digitalWrite(PIN_LED, HIGH);
      led_on = true;
      led_last = now;
    }
  }
}

// ===================== Arduino entry points =====================

void setup() {
  Serial.begin(115200);
  delay(1000);

  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, HIGH);

  disable_peripherals();

  NRF_POWER->DCDCEN = 1;

  pinMode(PIN_BZR, OUTPUT);
  pinMode(PIN_BZR_EN, OUTPUT);
  digitalWrite(PIN_BZR, LOW);
  buzzer_on();

  pinMode(PIN_BUTTON, INPUT);

  pinMode(PIN_3V3_ACC_EN, OUTPUT);
  digitalWrite(PIN_3V3_ACC_EN, HIGH);
  delay(10);
  Wire.begin();
  Wire.setClock(400000);

  bool accel_ok = qma6100p::init();
  log("=== T1000-E BLE Mouse ===");
  log("Accel: %s", accel_ok ? "OK" : "FAIL");

  beep(M_STARTUP);
  while (!mel_done()) mel_play();

  digitalWrite(PIN_LED, LOW);
  led_on = false;

  setup_ble();
  log("Advertising as 'T1000-E Mouse'...");
}

void loop() {
  if (!mel_done()) {
    mel_play();
  }

  // 2-pole low-pass on accelerometer (offset-corrected)
  {
    qma6100p::Accel raw = qma6100p::readXYZ();
    float gx = raw.x / 4096.0f - ACCEL_OFFSET_X;
    float gy = raw.y / 4096.0f - ACCEL_OFFSET_Y;
    float gz = raw.z / 4096.0f - ACCEL_OFFSET_Z;
    if (!filt_init) {
      lp1_gx = gx; lp1_gy = gy; lp1_gz = gz;
      filt_gx = gx; filt_gy = gy; filt_gz = gz;
      filt_init = true;
    } else {
      // Pole 1
      lp1_gx = LP_ALPHA * gx + (1.0f - LP_ALPHA) * lp1_gx;
      lp1_gy = LP_ALPHA * gy + (1.0f - LP_ALPHA) * lp1_gy;
      lp1_gz = LP_ALPHA * gz + (1.0f - LP_ALPHA) * lp1_gz;
      // Pole 2
      filt_gx = LP_ALPHA * lp1_gx + (1.0f - LP_ALPHA) * filt_gx;
      filt_gy = LP_ALPHA * lp1_gy + (1.0f - LP_ALPHA) * filt_gy;
      filt_gz = LP_ALPHA * lp1_gz + (1.0f - LP_ALPHA) * filt_gz;
    }
  }

  update_button();
  update_mouse();

  if (ble_connected && !connect_beeped && millis() - connect_t > 500) {
    connect_beeped = true;
    pairing_mode = false;
    log("BLE connection confirmed");
    beep(M_CONNECT);
  }

  update_led();

  uint32_t now = millis();
  if (now - debug_last >= DEBUG_INTERVAL_MS) {
    debug_last = now;
    log("gX=%.2f gY=%.2f gZ=%.2f", filt_gx, filt_gy, filt_gz);
  }

  delay(5);
}
