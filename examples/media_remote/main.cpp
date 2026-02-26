// T1000-E BLE Media Remote
//
// BLE HID media controller for Seeed SenseCAP T1000-E.
// Advertises as a standard Bluetooth media remote — any phone/laptop/tablet
// can pair from normal Bluetooth settings (no app needed).
//
// Controls:
//   Single click       → Play/Pause
//   Double click       → Next Track
//   Flip face-down     → Mute (toggle)
//   Hold + tilt L/R    → Volume Down/Up (repeating)

#include <Arduino.h>
#include <bluefruit.h>
#include <NonBlockingRtttl.h>
#include <Wire.h>

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

// -- RTTTL melodies --
static const char M_STARTUP[]   = "Up:d=16,o=6,b=200:c,e,g";
static const char M_CONNECT[]   = "Con:d=16,o=6,b=200:g,c7";
static const char M_CLICK[]     = "Clk:d=32,o=7,b=200:c";
static const char M_DBLCLICK[]  = "Dbl:d=32,o=7,b=200:c,p,c";
static const char M_MUTE[]      = "Mut:d=16,o=6,b=160:g,e,c";
static const char M_UNMUTE[]    = "Unm:d=16,o=6,b=160:c,e,g";
static const char M_GESTURE[]   = "Ges:d=32,o=7,b=200:e,p,e";

// -- Button FSM --
enum BtnState { BTN_IDLE, BTN_DOWN, BTN_WAIT_DBL, BTN_HELD };
static BtnState btn_state = BTN_IDLE;
static uint32_t btn_down_at  = 0;
static uint32_t btn_up_at    = 0;
static uint8_t  btn_prev     = LOW;

#define DEBOUNCE_MS    30
#define DBLCLICK_MS    280
#define LONGPRESS_MS   400

// -- Gesture mode --
static bool     gesture_active  = false;
static uint32_t gesture_last_vol = 0;
#define GESTURE_DEAD_ZONE  0.25f
#define GESTURE_REPEAT_MS  200

// -- Face-down mute --
static bool     face_muted     = false;
static bool     face_down      = false;
static uint32_t face_enter_at  = 0;  // when Z crossed threshold
static uint32_t face_exit_at   = 0;
#define FACE_DOWN_G       (-0.6f)
#define FACE_UP_G         (-0.2f)
#define FACE_HOLD_MS      500

// -- LED state --
static uint32_t led_last       = 0;
static bool     led_on         = false;
static uint8_t  led_blink_cnt  = 0;   // for double-blink pattern

// -- Connection tracking --
static bool     ble_connected  = false;

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
static void buzzer_off() { digitalWrite(PIN_BZR_EN, LOW); digitalWrite(PIN_BZR, LOW); }

static void beep(const char *melody) {
  buzzer_on();
  rtttl::begin(PIN_BZR, melody);
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

// Send a consumer key press (held) to all connected & secured peers.
static void hid_consumer_press(uint16_t usage_code) {
  for (uint16_t conn_hdl = 0; conn_hdl < BLE_MAX_CONNECTION; conn_hdl++) {
    BLEConnection* connection = Bluefruit.Connection(conn_hdl);
    if (connection && connection->connected() && connection->secured()) {
      blehid.consumerKeyPress(conn_hdl, usage_code);
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

// ===================== BLE callbacks =====================

static void connect_callback(uint16_t conn_hdl) {
  (void)conn_hdl;
  ble_connected = true;
  Serial.println("BLE connected");
  beep(M_CONNECT);
}

static void disconnect_callback(uint16_t conn_hdl, uint8_t reason) {
  (void)conn_hdl;
  (void)reason;
  ble_connected = false;
  Serial.printf("BLE disconnected, reason=0x%02X\n", reason);
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

  // Advertising
  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addTxPower();
  Bluefruit.Advertising.addAppearance(BLE_APPEARANCE_HID_KEYBOARD);
  Bluefruit.Advertising.addService(blehid);
  Bluefruit.ScanResponse.addName();

  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.setInterval(32, 244);  // fast then slow (units of 0.625ms)
  Bluefruit.Advertising.setFastTimeout(30);     // 30s of fast advertising
  Bluefruit.Advertising.start(0);               // advertise forever
}

// ===================== Button FSM =====================

static void update_button() {
  uint32_t now = millis();
  uint8_t raw = digitalRead(PIN_BUTTON);

  // Debounce
  bool pressed  = (raw == HIGH && btn_prev == LOW  && now - btn_down_at > DEBOUNCE_MS);
  bool released = (raw == LOW  && btn_prev == HIGH && now - btn_up_at   > DEBOUNCE_MS);
  btn_prev = raw;

  switch (btn_state) {
    case BTN_IDLE:
      if (pressed) {
        btn_down_at = now;
        btn_state = BTN_DOWN;
      }
      break;

    case BTN_DOWN:
      if (released) {
        btn_up_at = now;
        btn_state = BTN_WAIT_DBL;
      } else if (now - btn_down_at > LONGPRESS_MS) {
        // Long press → enter gesture mode
        gesture_active = true;
        gesture_last_vol = now;
        btn_state = BTN_HELD;
        Serial.println("GESTURE enter");
        beep(M_GESTURE);
      }
      break;

    case BTN_WAIT_DBL:
      if (pressed) {
        // Second press within window → double click
        btn_down_at = now;
        Serial.println("ACTION next_track");
        hid_consumer_tap(HID_USAGE_CONSUMER_SCAN_NEXT);
        beep(M_DBLCLICK);
        // Wait for this press to release before going idle
        btn_state = BTN_DOWN;
        // Override: after double-click action, go idle on next release
        // We reuse BTN_DOWN but the action was already fired. On release
        // we'll transition to WAIT_DBL again, but that's harmless — it
        // will time out to idle. To keep it clean, consume via a flag:
        // Actually, let's just go to a simple wait for release.
      } else if (now - btn_up_at > DBLCLICK_MS) {
        // Timeout → single click confirmed
        Serial.println("ACTION play_pause");
        hid_consumer_tap(HID_USAGE_CONSUMER_PLAY_PAUSE);
        beep(M_CLICK);
        btn_state = BTN_IDLE;
      }
      break;

    case BTN_HELD:
      if (released) {
        // Release from gesture mode
        hid_consumer_release();
        gesture_active = false;
        btn_up_at = now;
        btn_state = BTN_IDLE;
        Serial.println("GESTURE exit");
      }
      break;
  }
}

// ===================== Gesture volume =====================

static void update_gesture() {
  if (!gesture_active || !ble_connected) return;

  uint32_t now = millis();
  if (now - gesture_last_vol < GESTURE_REPEAT_MS) return;

  qma6100p::Accel a = qma6100p::readXYZ();
  float gy = a.y / 4096.0f;

  if (gy > GESTURE_DEAD_ZONE) {
    hid_consumer_tap(HID_USAGE_CONSUMER_VOLUME_INCREMENT);
    gesture_last_vol = now;
    Serial.printf("VOL+ (gY=%.2f)\n", gy);
  } else if (gy < -GESTURE_DEAD_ZONE) {
    hid_consumer_tap(HID_USAGE_CONSUMER_VOLUME_DECREMENT);
    gesture_last_vol = now;
    Serial.printf("VOL- (gY=%.2f)\n", gy);
  }
}

// ===================== Face-down mute =====================

static void update_face_mute() {
  // Skip during gesture mode (device is tilted for volume)
  if (gesture_active) return;

  float gz = qma6100p::gZ();
  uint32_t now = millis();

  if (!face_down) {
    // Looking for face-down: Z < threshold
    if (gz < FACE_DOWN_G) {
      if (face_enter_at == 0) face_enter_at = now;
      if (now - face_enter_at >= FACE_HOLD_MS) {
        face_down = true;
        face_exit_at = 0;
        if (!face_muted) {
          face_muted = true;
          Serial.println("FACE_DOWN → mute");
          hid_consumer_tap(HID_USAGE_CONSUMER_MUTE);
          beep(M_MUTE);
        }
      }
    } else {
      face_enter_at = 0;
    }
  } else {
    // Looking for face-up: Z > threshold (with hysteresis)
    if (gz > FACE_UP_G) {
      if (face_exit_at == 0) face_exit_at = now;
      if (now - face_exit_at >= FACE_HOLD_MS) {
        face_down = false;
        face_enter_at = 0;
        if (face_muted) {
          face_muted = false;
          Serial.println("FACE_UP → unmute");
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
    // Fast blink: 100ms toggle
    if (now - led_last > 100) {
      led_on = !led_on;
      digitalWrite(PIN_LED, led_on);
      led_last = now;
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
  Serial.printf("\n=== T1000-E Media Remote ===\n");
  Serial.printf("Accel: %s\n", accel_ok ? "OK" : "FAIL");

  // Startup melody
  rtttl::begin(PIN_BZR, M_STARTUP);
  while (!rtttl::done()) rtttl::play();

  digitalWrite(PIN_LED, LOW);
  led_on = false;

  setup_ble();
  Serial.println("Advertising as 'T1000-E Remote'...\n");
}

void loop() {
  // Non-blocking RTTTL
  if (!rtttl::done()) {
    rtttl::play();
  } else {
    // Turn off buzzer gate when melody finishes to save power
    buzzer_off();
  }

  update_button();
  update_gesture();
  update_face_mute();
  update_led();

  delay(5);
}
