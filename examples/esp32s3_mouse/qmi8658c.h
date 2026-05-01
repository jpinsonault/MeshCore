// QMI8658C — Header-only I2C driver for Waveshare ESP32-S3-Touch-LCD-1.28
//
// 6-axis IMU: 3-axis accelerometer + 3-axis gyroscope.
// I2C address 0x6B (SDO/SA0 pulled high on Waveshare board).
// Power must be stable before init.

#pragma once

#include <Wire.h>

namespace qmi8658c {

static constexpr uint8_t ADDR = 0x6B;

// Registers
static constexpr uint8_t REG_WHO_AM_I   = 0x00;
static constexpr uint8_t REG_REVISION   = 0x01;
static constexpr uint8_t REG_CTRL1      = 0x02;  // SPI/sensor enable
static constexpr uint8_t REG_CTRL2      = 0x03;  // accel settings (FSR, ODR)
static constexpr uint8_t REG_CTRL3      = 0x04;  // gyro settings (FSR, ODR)
static constexpr uint8_t REG_CTRL5      = 0x06;  // low-pass filter config
static constexpr uint8_t REG_CTRL7      = 0x08;  // enable accel/gyro
static constexpr uint8_t REG_CTRL9      = 0x0A;  // host command register
static constexpr uint8_t REG_STATUSINT  = 0x2D;
static constexpr uint8_t REG_STATUS0    = 0x2E;
static constexpr uint8_t REG_AX_L       = 0x35;  // accel data: 12 bytes (AX,AY,AZ,GX,GY,GZ)
static constexpr uint8_t REG_RESET      = 0x60;

static constexpr uint8_t WHO_AM_I_VAL   = 0x05;  // QMI8658C chip ID

// CTRL2: accel config — bits [6:4]=FSR, bits [3:0]=ODR
// FSR: 0=±2g, 1=±4g, 2=±8g, 3=±16g
// ODR: 0=8000Hz, 1=4000Hz, 2=2000Hz, 3=1000Hz, 4=500Hz, 5=250Hz, 6=125Hz, 7=62.5Hz
static constexpr uint8_t ACCEL_FS_2G    = (0 << 4);
static constexpr uint8_t ACCEL_FS_4G    = (1 << 4);
static constexpr uint8_t ACCEL_FS_8G    = (2 << 4);
static constexpr uint8_t ACCEL_ODR_250  = 5;
static constexpr uint8_t ACCEL_ODR_500  = 4;

// CTRL3: gyro config — bits [6:4]=FSR, bits [3:0]=ODR
// FSR: 0=±16dps, 1=±32dps, 2=±64dps, 3=±128dps, 4=±256dps, 5=±512dps, 6=±1024dps, 7=±2048dps
// ODR: same as accel
static constexpr uint8_t GYRO_FS_256    = (4 << 4);
static constexpr uint8_t GYRO_FS_512    = (5 << 4);
static constexpr uint8_t GYRO_FS_2048   = (7 << 4);
static constexpr uint8_t GYRO_ODR_250   = 5;
static constexpr uint8_t GYRO_ODR_500   = 4;

// Sensitivity (LSB per unit) at each FSR
static constexpr float ACCEL_SENS_2G    = 16384.0f;  // LSB/g
static constexpr float ACCEL_SENS_4G    = 8192.0f;
static constexpr float GYRO_SENS_256    = 128.0f;    // LSB/dps
static constexpr float GYRO_SENS_512    = 64.0f;
static constexpr float GYRO_SENS_2048   = 16.0f;

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

struct Accel { float x, y, z; };  // in g
struct Gyro  { float x, y, z; };  // in degrees/sec

struct IMUData {
  Accel accel;
  Gyro  gyro;
};

// Current configured sensitivities (set by init)
static float accel_sens = ACCEL_SENS_4G;
static float gyro_sens  = GYRO_SENS_512;

// Soft reset, configure ±4g accel / ±512dps gyro at 250Hz, enable both.
// Returns true if chip ID matches.
static bool init() {
  // Soft reset
  writeReg(REG_RESET, 0xB0);  // reset command
  delay(20);

  uint8_t id = readReg(REG_WHO_AM_I);
  if (id != WHO_AM_I_VAL) return false;

  // CTRL1: address auto-increment enabled
  writeReg(REG_CTRL1, 0x40);

  // CTRL2: accel ±4g, 250Hz ODR
  writeReg(REG_CTRL2, ACCEL_FS_4G | ACCEL_ODR_250);
  accel_sens = ACCEL_SENS_4G;

  // CTRL3: gyro ±512dps, 250Hz ODR
  writeReg(REG_CTRL3, GYRO_FS_512 | GYRO_ODR_250);
  gyro_sens = GYRO_SENS_512;

  // CTRL5: enable low-pass filter on both accel and gyro
  writeReg(REG_CTRL5, 0x11);

  // CTRL7: enable accel + gyro
  writeReg(REG_CTRL7, 0x03);

  delay(30);  // wait for first sample
  return true;
}

// Burst-read accel + gyro (12 bytes starting at REG_AX_L).
// Returns calibrated float values in g and dps.
static IMUData read() {
  Wire.beginTransmission(ADDR);
  Wire.write(REG_AX_L);
  Wire.endTransmission(false);
  Wire.requestFrom(ADDR, (uint8_t)12);

  uint8_t buf[12];
  for (uint8_t i = 0; i < 12; i++) buf[i] = Wire.read();

  IMUData d;
  d.accel.x = (int16_t)((uint16_t)buf[0]  | ((uint16_t)buf[1]  << 8)) / accel_sens;
  d.accel.y = (int16_t)((uint16_t)buf[2]  | ((uint16_t)buf[3]  << 8)) / accel_sens;
  d.accel.z = (int16_t)((uint16_t)buf[4]  | ((uint16_t)buf[5]  << 8)) / accel_sens;
  d.gyro.x  = (int16_t)((uint16_t)buf[6]  | ((uint16_t)buf[7]  << 8)) / gyro_sens;
  d.gyro.y  = (int16_t)((uint16_t)buf[8]  | ((uint16_t)buf[9]  << 8)) / gyro_sens;
  d.gyro.z  = (int16_t)((uint16_t)buf[10] | ((uint16_t)buf[11] << 8)) / gyro_sens;

  return d;
}

}  // namespace qmi8658c
