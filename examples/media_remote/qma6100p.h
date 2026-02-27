// QMA6100P — Header-only I2C accelerometer driver for T1000-E
//
// Seeed SenseCAP T1000-E has a QMA6100P at I2C address 0x12.
// Power rail must be enabled (PIN_3V3_ACC_EN HIGH) before init.

#pragma once

#include <Wire.h>

namespace qma6100p {

static constexpr uint8_t ADDR        = 0x12;

// Registers
static constexpr uint8_t REG_CHIP_ID = 0x00;
static constexpr uint8_t REG_DATA    = 0x01;  // 6 bytes: XL XH YL YH ZL ZH
static constexpr uint8_t REG_BW      = 0x10;  // bandwidth / ODR
static constexpr uint8_t REG_PM      = 0x11;  // power mode
static constexpr uint8_t REG_FSR     = 0x0E;  // full-scale range
static constexpr uint8_t REG_NVM     = 0x33;
static constexpr uint8_t REG_RESET   = 0x36;

static constexpr uint8_t CHIP_ID_VAL = 0x90;

static void writeReg(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(ADDR);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

static uint8_t readReg(uint8_t reg) {
  Wire.beginTransmission(ADDR);
  Wire.write(reg);
  Wire.endTransmission(false);
  Wire.requestFrom(ADDR, (uint8_t)1);
  return Wire.read();
}

struct Accel {
  int16_t x, y, z;
};

// Soft-reset, configure +-2g range, 250Hz BW, active mode.
// Returns true if chip ID matches.
//
// Init order matters: NVM load resets PM to standby, so active mode
// must be set AFTER NVM load. FSR/BW writes require active mode.
static bool init() {
  // Trigger soft reset
  writeReg(REG_RESET, 0xB6);
  delay(50);

  // Clear reset register to complete reset
  writeReg(REG_RESET, 0x00);
  delay(20);

  uint8_t id = readReg(REG_CHIP_ID);
  if (id != CHIP_ID_VAL) return false;

  // NVM load first (reloads factory calibration, but resets PM to standby)
  writeReg(REG_PM, 0x80);     // need active mode for NVM load to work
  delay(10);
  uint8_t nvm = readReg(REG_NVM);
  writeReg(REG_NVM, nvm | 0x08);
  delay(50);

  // Active mode AGAIN (NVM load reset PM to standby)
  writeReg(REG_PM, 0x80);
  delay(10);

  // Configure (must be in active mode to stick)
  writeReg(REG_FSR, 0x01);    // +/-2g
  writeReg(REG_BW, 0x05);     // 250 Hz bandwidth
  delay(20);
  return true;
}

// Burst-read raw 14-bit accelerometer data.
static Accel readXYZ() {
  Wire.beginTransmission(ADDR);
  Wire.write(REG_DATA);
  Wire.endTransmission(false);
  Wire.requestFrom(ADDR, (uint8_t)6);

  uint8_t buf[6];
  for (uint8_t i = 0; i < 6; i++) buf[i] = Wire.read();

  Accel a;
  a.x = (int16_t)((uint16_t)buf[0] | ((uint16_t)buf[1] << 8)) >> 2;
  a.y = (int16_t)((uint16_t)buf[2] | ((uint16_t)buf[3] << 8)) >> 2;
  a.z = (int16_t)((uint16_t)buf[4] | ((uint16_t)buf[5] << 8)) >> 2;
  return a;
}

}  // namespace qma6100p
