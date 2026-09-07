// Minimal driver for the PY32 IO expander on the M5Stack StackChan base (internal I2C, addr 0x6F).
// Pin 0 = VM_EN: servo power enable. Register map from m5stack/StackChan PY32IOExpander_Class.
#pragma once
#include <M5Unified.h>

class BaseIOExpander {
 public:
  static constexpr uint8_t ADDR = 0x6F;
  bool begin() {
    for (int i = 0; i < 6; i++) {            // the PY32 boots slowly; retry for ~1.2 s
      uint8_t v = M5.In_I2C.readRegister8(ADDR, 0x02, 100000);   // REG_VERSION
      if (v != 0 && v != 0xFF) { ok = true; version = v; return true; }
      delay(200);
    }
    return false;
  }
  bool isOk() const { return ok; }
  uint8_t ver() const { return version; }
  void setBit(uint8_t reg, uint8_t bit, bool on) {
    uint8_t v = M5.In_I2C.readRegister8(ADDR, reg, 100000);
    v = on ? (v | (1 << bit)) : (v & ~(1 << bit));
    M5.In_I2C.writeRegister8(ADDR, reg, v, 100000);
  }
  // VM_EN = pin 0: output, pull-up, level high = servo power on
  void setServoPower(bool on) {
    if (!ok) return;
    setBit(0x03, 0, true);    // GPIO mode: output
    setBit(0x0B, 0, false);   // pull-down off
    setBit(0x09, 0, true);    // pull-up on
    setBit(0x05, 0, on);      // output level
  }
  bool servoPower() { return ok && (M5.In_I2C.readRegister8(ADDR, 0x05, 100000) & 1); }
 private:
  bool ok = false; uint8_t version = 0;
};
