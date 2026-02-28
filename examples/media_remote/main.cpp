// T1000-E BLE Media Remote
//
// BLE HID media controller for Seeed SenseCAP T1000-E.
// Advertises as a standard Bluetooth media remote — any phone/laptop/tablet
// can pair from normal Bluetooth settings (no app needed).
//
// Controls (Media Mode — default):
//   Single click              → Play/Pause
//   Double click              → Next Track
//   Hold 400ms + tilt L/R     → Volume Down/Up (repeating)
//   Flip face-down            → Mute (toggle)
//   Quad tap                  → Switch to Mouse Mode
//   Double-press + hold 3s    → Power off (deep sleep, wake on button)
//   Triple-press + hold 3s    → Pairing mode (clear bonds, re-advertise)
//
// Controls (Mouse Mode — quad-tap to enter/exit):
//   Single click              → Left Click
//   Double click              → Right Click
//   Hold 400ms + tilt         → Move cursor (tilt-to-move with power curve)
//   Quad tap                  → Switch to Media Mode

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
// Everything derives from BASE_FREQ. Change this one value to shift all sounds.
// Melodies use BASE (click), BASE+5 (mid), BASE+11 (top) for 3-note palette.
// Volume scale: 16 steps, 5Hz apart, starting from BASE.
// Tick duration: guaranteed 12+ cycles at BASE for reliable piezo ring-up.
#define BASE_FREQ    82   // Hz — floor for click, mute, dblclick, vol min
#define MID_FREQ     (BASE_FREQ + 5)   // one semitone-ish up
#define TOP_FREQ     (BASE_FREQ + 11)  // two semitones-ish up
#define NOTE_DUR     123  // ms — standard note (12+ cycles at 82Hz)
#define VOL_TICK_MS  123  // ms — same as note dur so vol sounds match clicks

static const MelNote N_STARTUP[]  = {{BASE_FREQ,NOTE_DUR},{BASE_FREQ,NOTE_DUR},{MID_FREQ,NOTE_DUR}};
static const MelNote N_CONNECT[]  = {{BASE_FREQ,NOTE_DUR},{MID_FREQ,NOTE_DUR}};
static const MelNote N_CLICK[]    = {{BASE_FREQ,NOTE_DUR}};
static const MelNote N_DBLCLICK[] = {{BASE_FREQ,NOTE_DUR},{0,50},{BASE_FREQ,NOTE_DUR}};
static const MelNote N_MUTE[]     = {{TOP_FREQ,NOTE_DUR},{MID_FREQ,NOTE_DUR},{BASE_FREQ,NOTE_DUR}};
static const MelNote N_UNMUTE[]   = {{BASE_FREQ,NOTE_DUR},{TOP_FREQ,NOTE_DUR},{TOP_FREQ,NOTE_DUR}};
static const MelNote N_POWER_OFF[]= {{MID_FREQ,150},{BASE_FREQ,150},{BASE_FREQ,150},{BASE_FREQ,600}};
static const MelNote N_PAIRING[]  = {{MID_FREQ,188},{BASE_FREQ,188},{BASE_FREQ,188},{BASE_FREQ,750}};
static const MelNote N_NOT_CONN[] = {{MID_FREQ,NOTE_DUR},{BASE_FREQ,NOTE_DUR}};
static const MelNote N_HOLD[]     = {{BASE_FREQ,NOTE_DUR},{BASE_FREQ,NOTE_DUR},{MID_FREQ,NOTE_DUR},{MID_FREQ,NOTE_DUR},{MID_FREQ,NOTE_DUR}};
static const MelNote N_MOUSE_ON[] = {{BASE_FREQ,NOTE_DUR},{MID_FREQ,NOTE_DUR},{TOP_FREQ,NOTE_DUR},{MID_FREQ,NOTE_DUR}};
static const MelNote N_MOUSE_OFF[]= {{MID_FREQ,NOTE_DUR},{BASE_FREQ,NOTE_DUR},{MID_FREQ,NOTE_DUR},{TOP_FREQ,NOTE_DUR}};

static const Melody M_STARTUP   = {N_STARTUP,   3};
static const Melody M_CONNECT   = {N_CONNECT,   2};
static const Melody M_CLICK     = {N_CLICK,     1};
static const Melody M_DBLCLICK  = {N_DBLCLICK,  3};
static const Melody M_MUTE      = {N_MUTE,      3};
static const Melody M_UNMUTE    = {N_UNMUTE,     3};
static const Melody M_POWER_OFF = {N_POWER_OFF, 4};
static const Melody M_PAIRING   = {N_PAIRING,   4};
static const Melody M_NOT_CONN  = {N_NOT_CONN,  2};
static const Melody M_HOLD      = {N_HOLD,      5};
static const Melody M_MOUSE_ON  = {N_MOUSE_ON,  4};
static const Melody M_MOUSE_OFF = {N_MOUSE_OFF, 4};

// 16 linear steps from BASE_FREQ up, 5Hz per step. Vol min = click pitch.
static const uint16_t VOL_NOTES[] = {
  BASE_FREQ,      BASE_FREQ + 5,  BASE_FREQ + 10, BASE_FREQ + 15,
  BASE_FREQ + 20, BASE_FREQ + 25, BASE_FREQ + 30, BASE_FREQ + 35,
  BASE_FREQ + 40, BASE_FREQ + 45, BASE_FREQ + 50, BASE_FREQ + 55,
  BASE_FREQ + 60, BASE_FREQ + 65, BASE_FREQ + 70, BASE_FREQ + 75
};
#define VOL_NOTE_COUNT 16
static int8_t vol_note_idx = 6;  // 6 steps down to click pitch, 9 up to ceiling

#define HOLD_SCALE_LEN   5
#define HOLD_SCALE_START 500   // ms into hold before first note
#define HOLD_SCALE_STEP  500   // ms between notes

// -- Button debounce (lockout: accept first edge, ignore bounces for N ms) --
static uint8_t  btn_db_state = LOW;       // debounced button state
static uint32_t btn_db_lock  = 0;        // when last edge was accepted

// -- Button FSM (click counter — same pattern as OS input systems) --
enum BtnFSM { BTN_IDLE, BTN_DOWN, BTN_UP, BTN_GESTURE };
static BtnFSM   btn_fsm     = BTN_IDLE;
static uint8_t  btn_clicks   = 0;        // completed press+release cycles
static uint32_t btn_press_t  = 0;        // when current press started
static uint32_t btn_rel_t    = 0;        // when last release happened
static uint8_t  btn_hold_step = 0;       // next ascending scale note to play

#define DEBOUNCE_MS    10
#define CLICK_WINDOW   300   // ms after last release to wait for next click
#define LONGPRESS_MS   400   // first-press hold → gesture mode
#define HOLD_3S_MS     3000  // multi-press hold → power off / pairing

// -- EMA-filtered accelerometer (offset-corrected, updated every loop) --
// Calibrated offsets from accel_calibration.json — subtract to get true g values.
// After correction: Z = face up/down, Y = left/right, X = forward/back.
#define ACCEL_OFFSET_X  (-0.155f)
#define ACCEL_OFFSET_Y  (-0.805f)
#define ACCEL_OFFSET_Z  (1.055f)
#define EMA_ALPHA  0.25f
static float filt_gx = 0, filt_gy = 0, filt_gz = 0;  // offset-corrected & filtered
static bool  filt_init = false;

// -- Gesture mode --
// Tilt is measured as angular change from a reference captured on gesture enter.
// After offset correction: left/right tilt = Y axis, face up/down = Z axis.
static bool     gesture_active  = false;
static float    gesture_ref_gy = 0, gesture_ref_gz = 0;
static bool     gesture_ref_valid = false;
static bool     gesture_tilt_pos = false;  // tilted right (vol up)
static bool     gesture_tilt_neg = false;  // tilted left (vol down)
static uint32_t gesture_last_vol = 0;
#define TILT_ENTER_DEG    15.0f
#define TILT_EXIT_DEG     10.0f
#define TILT_ENTER_RAD    (TILT_ENTER_DEG * 3.14159f / 180.0f)
#define TILT_EXIT_RAD     (TILT_EXIT_DEG  * 3.14159f / 180.0f)
#define GESTURE_REPEAT_MS 250

// -- Remote mode --
enum RemoteMode { MODE_MEDIA, MODE_MOUSE };
static RemoteMode remote_mode = MODE_MEDIA;

// -- Mouse mode --
#define MOUSE_DEADZONE_DEG   2.0f
#define MOUSE_MAX_ANGLE_DEG  30.0f    // reference angle where velocity = MAX_VELOCITY (no cap beyond)
#define MOUSE_MAX_VELOCITY   15.0f
#define MOUSE_EXPONENT       2.0f
#define MOUSE_SMOOTH_ALPHA   0.35f

static bool  mouse_gesture_active = false;
static float mouse_ref_pitch = 0, mouse_ref_roll = 0;
static float mouse_smooth_pitch = 0, mouse_smooth_roll = 0;
static float mouse_accum_x = 0, mouse_accum_y = 0;

// -- Face-down mute --
// After offset correction: Z ~ +1.0 face-up, Z ~ -1.0 face-down
static bool     face_muted     = false;
static bool     face_down      = false;
static uint32_t face_enter_at  = 0;
static uint32_t face_exit_at   = 0;
#define FACE_DOWN_GZ     (-0.6f)   // corrected Z < this = face-down
#define FACE_UP_GZ       (-0.2f)   // corrected Z > this = face-up (hysteresis)
#define FACE_HOLD_MS      500

// -- LED state --
static uint32_t led_last       = 0;
static bool     led_on         = false;
static uint8_t  led_blink_cnt  = 0;   // for double-blink pattern

// -- Connection tracking --
static bool     ble_connected  = false;
static bool     pairing_mode   = false;
static uint32_t connect_t      = 0;     // when last BLE connect happened
static bool     connect_beeped = false; // true once we've confirmed + beeped

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

  // Sensor rails off (but keep accelerometer power ON)
  pinMode(PIN_3V3_EN, OUTPUT);
  pinMode(SENSOR_EN, OUTPUT);
  digitalWrite(PIN_3V3_EN, LOW);
  digitalWrite(SENSOR_EN, LOW);

  // LR1110 radio: deselect and hold in reset
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

// Play a raw frequency for a short duration (used for volume feedback)
static uint32_t tone_end_ms = 0;
static void play_freq(uint16_t freq_hz, uint16_t dur_ms) {
  buzzer_on();
  tone(PIN_BZR, freq_hz, dur_ms);
  tone_end_ms = millis() + dur_ms;
}

// ===================== HID send helper =====================

// Send a consumer key press+release to all connected & secured peers.
static void hid_consumer_tap(uint16_t usage_code) {
  for (uint16_t conn_hdl = 0; conn_hdl < BLE_MAX_CONNECTION; conn_hdl++) {
    BLEConnection* connection = Bluefruit.Connection(conn_hdl);
    if (connection && connection->connected() && connection->secured()) {
      blehid.consumerKeyPress(conn_hdl, usage_code);
      delay(10);
      blehid.consumerKeyRelease(conn_hdl);
    }
  }
}

// Release consumer key on all connections.
static void hid_consumer_release() {
  for (uint16_t conn_hdl = 0; conn_hdl < BLE_MAX_CONNECTION; conn_hdl++) {
    BLEConnection* connection = Bluefruit.Connection(conn_hdl);
    if (connection && connection->connected() && connection->secured()) {
      blehid.consumerKeyRelease(conn_hdl);
    }
  }
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
  while (!mel_done()) mel_play();  // play melody to completion
  buzzer_off();

  disable_peripherals();
  digitalWrite(PIN_3V3_ACC_EN, LOW);  // kill accelerometer rail
  digitalWrite(PIN_LED, LOW);

  // Wait for button release so we don't wake immediately
  while (digitalRead(PIN_BUTTON) == HIGH) delay(10);
  delay(100);

  // Configure button as wake source, then enter system-off
  nrf_gpio_cfg_sense_input(g_ADigitalPinMap[PIN_BUTTON],
                           NRF_GPIO_PIN_NOPULL,
                           NRF_GPIO_PIN_SENSE_HIGH);
  sd_power_system_off();
  // Never returns — full reset on wake
}

// ===================== Pairing mode =====================

static void do_enter_pairing() {
  log("PAIRING MODE — bonds cleared");
  beep(M_PAIRING);

  Bluefruit.Advertising.stop();
  Bluefruit.Periph.clearBonds();

  // Disconnect any current connection
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
  Bluefruit.setName("T1000-E Remote");

  Bluefruit.Periph.setConnectCallback(connect_callback);
  Bluefruit.Periph.setDisconnectCallback(disconnect_callback);

  // Device Information Service
  bledis.setManufacturer("Seeed");
  bledis.setModel("T1000-E Media Remote");
  bledis.begin();

  // HID service
  blehid.begin();

  // BLE UART (debug console — connect with nRF Connect / LightBlue)
  bleuart.begin();

  // Advertising: HID appearance in main packet, NUS in scan response
  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addTxPower();
  Bluefruit.Advertising.addAppearance(BLE_APPEARANCE_HID_KEYBOARD);
  Bluefruit.Advertising.addService(blehid);
  Bluefruit.ScanResponse.addName();
  Bluefruit.ScanResponse.addService(bleuart);

  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.setInterval(32, 244);  // fast then slow (units of 0.625ms)
  Bluefruit.Advertising.setFastTimeout(30);     // 30s of fast advertising
  Bluefruit.Advertising.start(0);               // advertise forever
}

// ===================== Button FSM =====================

static void update_button() {
  uint32_t now = millis();
  uint8_t raw = digitalRead(PIN_BUTTON);

  // Lockout debounce: accept first edge instantly, ignore for DEBOUNCE_MS after
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
        // First press held → gesture mode (volume or mouse depending on mode)
        if (remote_mode == MODE_MEDIA) {
          gesture_active = true;
          float yz_mag = sqrtf(filt_gy * filt_gy + filt_gz * filt_gz);
          gesture_ref_valid = (yz_mag >= 0.3f);
          gesture_ref_gy = filt_gy;
          gesture_ref_gz = filt_gz;
          gesture_tilt_pos = false;
          gesture_tilt_neg = false;
          gesture_last_vol = now;
          btn_fsm = BTN_GESTURE;
          log("GESTURE enter (Y=%.2f Z=%.2f mag=%.2f%s)",
              filt_gy, filt_gz, yz_mag, gesture_ref_valid ? "" : " DEFERRED");
        } else {
          mouse_gesture_active = true;
          mouse_ref_pitch = atan2f(filt_gx, sqrtf(filt_gy*filt_gy + filt_gz*filt_gz));
          mouse_ref_roll  = atan2f(filt_gy, sqrtf(filt_gx*filt_gx + filt_gz*filt_gz));
          mouse_smooth_pitch = 0;
          mouse_smooth_roll = 0;
          mouse_accum_x = 0;
          mouse_accum_y = 0;
          btn_fsm = BTN_GESTURE;
          log("MOUSE GESTURE enter (pitch=%.2f roll=%.2f)",
              mouse_ref_pitch, mouse_ref_roll);
        }
      } else if (btn_clicks >= 1) {
        // Multi-press hold: ascending scale counting up to action
        uint32_t held = now - btn_press_t;
        if (held >= HOLD_SCALE_START && btn_hold_step < HOLD_SCALE_LEN) {
          uint32_t next_at = HOLD_SCALE_START + (uint32_t)btn_hold_step * HOLD_SCALE_STEP;
          if (held >= next_at) {
            play_freq(M_HOLD.notes[btn_hold_step].freq, M_HOLD.notes[btn_hold_step].dur);
            btn_hold_step++;
          }
        }
        if (btn_clicks == 1 && now - btn_press_t > HOLD_3S_MS) {
          do_power_off();  // never returns
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
        // Window expired — fire action based on click count
        if (btn_clicks == 4) {
          // Quad-tap: toggle mode
          if (remote_mode == MODE_MEDIA) {
            remote_mode = MODE_MOUSE;
            log("MODE -> MOUSE");
            beep(M_MOUSE_ON);
          } else {
            remote_mode = MODE_MEDIA;
            log("MODE -> MEDIA");
            beep(M_MOUSE_OFF);
          }
        } else if (btn_clicks == 1) {
          if (ble_connected) {
            if (remote_mode == MODE_MEDIA) {
              log("ACTION play_pause");
              hid_consumer_tap(HID_USAGE_CONSUMER_PLAY_PAUSE);
              beep(M_CLICK);
            } else {
              log("ACTION left_click");
              hid_mouse_click(MOUSE_BUTTON_LEFT);
              beep(M_CLICK);
            }
          } else {
            beep(M_NOT_CONN);
          }
        } else if (btn_clicks == 2) {
          if (ble_connected) {
            if (remote_mode == MODE_MEDIA) {
              log("ACTION next_track");
              hid_consumer_tap(HID_USAGE_CONSUMER_SCAN_NEXT);
              beep(M_DBLCLICK);
            } else {
              log("ACTION right_click");
              hid_mouse_click(MOUSE_BUTTON_RIGHT);
              beep(M_DBLCLICK);
            }
          } else {
            beep(M_NOT_CONN);
          }
        }
        btn_fsm = BTN_IDLE;
      }
      break;

    case BTN_GESTURE:
      if (released) {
        if (mouse_gesture_active) {
          mouse_gesture_active = false;
          log("MOUSE GESTURE exit");
        } else {
          hid_consumer_release();
          gesture_active = false;
          log("GESTURE exit");
        }
        btn_fsm = BTN_IDLE;
      }
      break;
  }
}

// ===================== Gesture volume (relative tilt with atan2) =====================

static void update_gesture() {
  if (!gesture_active) return;

  // Guard: need enough Y-Z gravity component for reliable roll detection.
  // Below 0.3g (~72° from horizontal) noise dominates and tilt sign reverses.
  float yz_mag = sqrtf(filt_gy * filt_gy + filt_gz * filt_gz);
  if (yz_mag < 0.3f) {
    gesture_tilt_pos = false;
    gesture_tilt_neg = false;
    return;
  }

  // Lazy reference capture: if entry was at a bad angle, grab it now
  if (!gesture_ref_valid) {
    gesture_ref_gy = filt_gy;
    gesture_ref_gz = filt_gz;
    gesture_ref_valid = true;
    log("GESTURE ref captured (Y=%.2f Z=%.2f)", filt_gy, filt_gz);
    return;
  }

  // Wrap-safe angle delta via 2D cross/dot product in the Y-Z plane.
  // cross = |ref||cur|sin(delta), dot = |ref||cur|cos(delta)
  // atan2 gives correct signed angle without ±pi discontinuity.
  float cross = gesture_ref_gz * filt_gy - gesture_ref_gy * filt_gz;
  float dot   = gesture_ref_gy * filt_gy + gesture_ref_gz * filt_gz;
  float delta = atan2f(cross, dot);
  float delta_deg = delta * 180.0f / 3.14159f;
  uint32_t now = millis();

  // Hysteresis state machine for positive tilt direction
  if (!gesture_tilt_pos && delta > TILT_ENTER_RAD) {
    gesture_tilt_pos = true;
    gesture_tilt_neg = false;
    log("TILT -> positive (%.1f deg)", delta_deg);
  } else if (gesture_tilt_pos && delta < TILT_EXIT_RAD) {
    gesture_tilt_pos = false;
    log("TILT -> center (%.1f deg)", delta_deg);
  }

  // Hysteresis state machine for negative tilt direction
  if (!gesture_tilt_neg && delta < -TILT_ENTER_RAD) {
    gesture_tilt_neg = true;
    gesture_tilt_pos = false;
    log("TILT -> negative (%.1f deg)", delta_deg);
  } else if (gesture_tilt_neg && delta > -TILT_EXIT_RAD) {
    gesture_tilt_neg = false;
    log("TILT -> center (%.1f deg)", delta_deg);
  }

  // Repeat volume changes at interval
  if (now - gesture_last_vol >= GESTURE_REPEAT_MS) {
    if (gesture_tilt_pos) {
      if (ble_connected) hid_consumer_tap(HID_USAGE_CONSUMER_VOLUME_INCREMENT);
      if (vol_note_idx < VOL_NOTE_COUNT - 1) vol_note_idx++;
      play_freq(VOL_NOTES[vol_note_idx], VOL_TICK_MS);
      gesture_last_vol = now;
    } else if (gesture_tilt_neg) {
      if (ble_connected) hid_consumer_tap(HID_USAGE_CONSUMER_VOLUME_DECREMENT);
      if (vol_note_idx > 0) vol_note_idx--;
      play_freq(VOL_NOTES[vol_note_idx], VOL_TICK_MS);
      gesture_last_vol = now;
    }
  }
}

// ===================== Mouse gesture (tilt-to-cursor) =====================

static float mouse_apply_curve(float angle_deg) {
  float sign = (angle_deg >= 0) ? 1.0f : -1.0f;
  float mag = fabsf(angle_deg);
  if (mag < MOUSE_DEADZONE_DEG) return 0.0f;
  float normalized = (mag - MOUSE_DEADZONE_DEG) / (MOUSE_MAX_ANGLE_DEG - MOUSE_DEADZONE_DEG);
  float curved = powf(normalized, MOUSE_EXPONENT);
  return sign * curved * MOUSE_MAX_VELOCITY;
}

static void update_mouse_gesture() {
  if (!mouse_gesture_active) return;

  // Compute current pitch and roll from filtered accel
  float cur_pitch = atan2f(filt_gx, sqrtf(filt_gy*filt_gy + filt_gz*filt_gz));
  float cur_roll  = atan2f(filt_gy, sqrtf(filt_gx*filt_gx + filt_gz*filt_gz));

  // Delta from reference (convert to degrees)
  float d_pitch_deg = (cur_pitch - mouse_ref_pitch) * 180.0f / 3.14159f;
  float d_roll_deg  = (cur_roll  - mouse_ref_roll)  * 180.0f / 3.14159f;

  // Smooth the deltas with separate EMA
  mouse_smooth_pitch = MOUSE_SMOOTH_ALPHA * d_pitch_deg + (1.0f - MOUSE_SMOOTH_ALPHA) * mouse_smooth_pitch;
  mouse_smooth_roll  = MOUSE_SMOOTH_ALPHA * d_roll_deg  + (1.0f - MOUSE_SMOOTH_ALPHA) * mouse_smooth_roll;

  // Apply response curve: Roll → X (left/right), Pitch → Y (up/down)
  // Pitch gets 15% boost — harder to tilt forward/back when holding the device
  float vx = mouse_apply_curve(mouse_smooth_roll);
  float vy = mouse_apply_curve(mouse_smooth_pitch * 1.15f);

  // Sub-pixel accumulation — extract integer part, keep fractional remainder
  mouse_accum_x += vx;
  mouse_accum_y += vy;

  int raw_dx = (int)mouse_accum_x;
  int raw_dy = (int)mouse_accum_y;
  if (raw_dx > 127) raw_dx = 127;
  if (raw_dx < -127) raw_dx = -127;
  if (raw_dy > 127) raw_dy = 127;
  if (raw_dy < -127) raw_dy = -127;

  int8_t dx = (int8_t)raw_dx;
  int8_t dy = (int8_t)raw_dy;
  mouse_accum_x -= dx;
  mouse_accum_y -= dy;

  if (dx != 0 || dy != 0) {
    hid_mouse_move(dx, dy);
  }
}

// ===================== Face-down mute (uses corrected Z axis) =====================

static void update_face_mute() {
  if (gesture_active) return;

  // After offset correction: Z ~ +1.0 face-up, Z ~ -1.0 face-down
  float gz = filt_gz;
  uint32_t now = millis();

  if (!face_down) {
    if (gz < FACE_DOWN_GZ) {
      if (face_enter_at == 0) face_enter_at = now;
      if (now - face_enter_at >= FACE_HOLD_MS) {
        face_down = true;
        face_exit_at = 0;
        if (!face_muted) {
          face_muted = true;
          log("FACE_DOWN -> mute (Z=%.2f)", gz);
          hid_consumer_tap(HID_USAGE_CONSUMER_MUTE);
          beep(M_MUTE);
        }
      }
    } else {
      face_enter_at = 0;
    }
  } else {
    if (gz > FACE_UP_GZ) {
      if (face_exit_at == 0) face_exit_at = now;
      if (now - face_exit_at >= FACE_HOLD_MS) {
        face_down = false;
        face_enter_at = 0;
        if (face_muted) {
          face_muted = false;
          log("FACE_UP -> unmute (Z=%.2f)", gz);
          hid_consumer_tap(HID_USAGE_CONSUMER_MUTE);
          beep(M_UNMUTE);
        }
      }
    } else {
      face_exit_at = 0;
    }
  }
}

// ===================== LED patterns =====================

static void update_led() {
  uint32_t now = millis();

  if (gesture_active) {
    // Fast blink: 100ms toggle (media volume gesture)
    if (now - led_last > 100) {
      led_on = !led_on;
      digitalWrite(PIN_LED, led_on);
      led_last = now;
    }
  } else if (mouse_gesture_active) {
    // Double-blink: quick on-off-on-off then gap (mouse gesture)
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
    // Triple-blink every 1.5s: on-off-on-off-on-off-wait
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
  } else if (face_muted) {
    // Double-blink every 2s: on 100ms, off 100ms, on 100ms, off 1700ms
    uint32_t phase = (now - led_last);
    if (led_blink_cnt == 0 && phase > 2000) {
      digitalWrite(PIN_LED, HIGH);
      led_on = true;
      led_blink_cnt = 1;
      led_last = now;
    } else if (led_blink_cnt == 1 && phase > 100) {
      digitalWrite(PIN_LED, LOW);
      led_on = false;
      led_blink_cnt = 2;
      led_last = now;
    } else if (led_blink_cnt == 2 && phase > 100) {
      digitalWrite(PIN_LED, HIGH);
      led_on = true;
      led_blink_cnt = 3;
      led_last = now;
    } else if (led_blink_cnt == 3 && phase > 100) {
      digitalWrite(PIN_LED, LOW);
      led_on = false;
      led_blink_cnt = 0;
      led_last = now;
    }
  } else if (ble_connected) {
    // Solid on
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

  // LED on immediately to show boot
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, HIGH);

  disable_peripherals();

  // Enable DC/DC converter for efficiency
  NRF_POWER->DCDCEN = 1;

  // Buzzer
  pinMode(PIN_BZR, OUTPUT);
  pinMode(PIN_BZR_EN, OUTPUT);
  digitalWrite(PIN_BZR, LOW);
  buzzer_on();

  // Button
  pinMode(PIN_BUTTON, INPUT);

  // Accelerometer power on and I2C init
  pinMode(PIN_3V3_ACC_EN, OUTPUT);
  digitalWrite(PIN_3V3_ACC_EN, HIGH);
  delay(10);
  Wire.begin();
  Wire.setClock(400000);

  bool accel_ok = qma6100p::init();
  log("=== T1000-E Media Remote ===");
  log("Accel: %s", accel_ok ? "OK" : "FAIL");

  // Startup melody (blocking)
  beep(M_STARTUP);
  while (!mel_done()) mel_play();

  digitalWrite(PIN_LED, LOW);
  led_on = false;

  setup_ble();
  log("Advertising as 'T1000-E Remote'...");
}

void loop() {
  // Non-blocking melody + tone() management
  if (!mel_done()) {
    mel_play();
  } else if (tone_end_ms > 0 && millis() >= tone_end_ms) {
    buzzer_off();
    tone_end_ms = 0;
  }

  // Update EMA-filtered accelerometer with offset correction
  {
    qma6100p::Accel raw = qma6100p::readXYZ();
    float gx = raw.x / 4096.0f - ACCEL_OFFSET_X;
    float gy = raw.y / 4096.0f - ACCEL_OFFSET_Y;
    float gz = raw.z / 4096.0f - ACCEL_OFFSET_Z;
    if (!filt_init) {
      filt_gx = gx; filt_gy = gy; filt_gz = gz;
      filt_init = true;
    } else {
      filt_gx = EMA_ALPHA * gx + (1.0f - EMA_ALPHA) * filt_gx;
      filt_gy = EMA_ALPHA * gy + (1.0f - EMA_ALPHA) * filt_gy;
      filt_gz = EMA_ALPHA * gz + (1.0f - EMA_ALPHA) * filt_gz;
    }
  }

  update_button();
  update_gesture();
  update_mouse_gesture();
  update_face_mute();

  // Deferred connection confirmation: failed bond attempts disconnect in <100ms.
  // Only beep + exit pairing mode once the connection survives 500ms.
  if (ble_connected && !connect_beeped && millis() - connect_t > 500) {
    connect_beeped = true;
    pairing_mode = false;
    log("BLE connection confirmed");
    beep(M_CONNECT);
  }

  update_led();

  // Periodic debug output (filtered values only)
  uint32_t now = millis();
  if (now - debug_last >= DEBUG_INTERVAL_MS) {
    debug_last = now;
    log("gX=%.2f gY=%.2f gZ=%.2f", filt_gx, filt_gy, filt_gz);
  }

  delay(5);
}
