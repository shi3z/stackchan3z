// Pan/tilt head for the M5Stack StackChan: two Feetech SCS serial bus servos on UART1 (1 Mbps, TX=G6, RX=G7).
// ID 1 = yaw (pan), ID 2 = pitch (tilt). Raw units: 3.2 steps per degree; zero positions from the official firmware.
// Public API is in degrees with 90/90 = straight ahead, same as the old PWM version.
#pragma once
#include <Arduino.h>
#include "SCSCL.h"
#include "config.h"

class Head {
 public:
  static constexpr float PAN_MIN = 10, PAN_MAX = 170, TILT_MIN = 62, TILT_MAX = 118;   // tilt maps to pitch 5..85 deg (manual limit)
  static constexpr int YAW_ID = 1, PITCH_ID = 2;
  static constexpr int YAW_ZERO = 460, PITCH_ZERO = 620;     // raw position at angle 0 (official defaults)
  static constexpr float STEPS_PER_DEG = 3.2f;               // 0.3125 deg per raw step
  int pitchCenterDeg = 45;                                   // pitch angle (official units) that we call tilt=90

  // pins are ignored (kept for API compatibility); the bus is fixed to UART1 G6/G7
  void begin(int tx = CFG_SERVO_TX, int rx = CFG_SERVO_RX) {
    if (!busOk) busOk = bus.begin(UART_NUM_1, 1000000, tx, rx);
    if (!busOk) { Serial.println("[head] uart init failed"); return; }
    pingYaw = bus.Ping(YAW_ID) != -1; pingPitch = bus.Ping(PITCH_ID) != -1;
    Serial.printf("[head] servo ping: yaw(id1)=%s pitch(id2)=%s\n", pingYaw ? "ok" : "NO", pingPitch ? "ok" : "NO");
    bus.EnableTorque(YAW_ID, 1); bus.EnableTorque(PITCH_ID, 1);
    attached = true; lastRawPan = lastRawTilt = -1;
    write(curPan, curTilt, true);
  }
  void end() { if (!attached) return; bus.EnableTorque(YAW_ID, 0); bus.EnableTorque(PITCH_ID, 0); attached = false; }
  bool isAttached() const { return attached; }
  bool isGesturing() const { return gesture != NONE; }
  bool atTarget() const { return fabsf(curPan - tgtPan) < 1.5f && fabsf(curTilt - tgtTilt) < 1.5f; }
  bool servosFound() const { return pingYaw && pingPitch; }
  int pinPan() const { return CFG_SERVO_TX; }
  int pinTilt() const { return CFG_SERVO_RX; }
  float pan() const { return curPan; }
  float tilt() const { return curTilt; }
  int readRawPan()  { return bus.ReadPos(YAW_ID); }
  int readRawTilt() { return bus.ReadPos(PITCH_ID); }

  void moveTo(float pan, float tilt, float speed = 3.0f) {
    tgtPan = constrain(pan, PAN_MIN, PAN_MAX); tgtTilt = constrain(tilt, TILT_MIN, TILT_MAX);
    stepDeg = max(0.5f, speed); gesture = NONE;
  }
  void nod(int times = 2)   { gesture = NOD;   gCount = times * 2; gPhase = 0; gBasePan = tgtPan; gBaseTilt = tgtTilt; }
  void shake(int times = 2) { gesture = SHAKE; gCount = times * 2; gPhase = 0; gBasePan = tgtPan; gBaseTilt = tgtTilt; }
  void center() { moveTo(90, 90); }

  void update() {
    uint32_t now = millis();
    if (now - last < 20) return;
    last = now;
    if (gesture != NONE && fabsf(curPan - gTgtPan()) < 1 && fabsf(curTilt - gTgtTilt()) < 1) {
      if (gCount-- <= 0) { gesture = NONE; tgtPan = gBasePan; tgtTilt = gBaseTilt; }
      else gPhase++;
    }
    float tp = gesture != NONE ? gTgtPan() : tgtPan, tt = gesture != NONE ? gTgtTilt() : tgtTilt;
    float sp = gesture != NONE ? 5.0f : stepDeg;
    curPan  += constrain(tp - curPan, -sp, sp);
    curTilt += constrain(tt - curTilt, -sp, sp);
    if (attached) write(curPan, curTilt, false);
  }

 private:
  enum { NONE, NOD, SHAKE } gesture = NONE;
  SCSCL bus;
  bool busOk = false, attached = false, pingYaw = false, pingPitch = false;
  float curPan = 90, curTilt = 90, tgtPan = 90, tgtTilt = 90, stepDeg = 3;
  float gBasePan = 90, gBaseTilt = 90; int gCount = 0, gPhase = 0;
  uint32_t last = 0; int lastRawPan = -1, lastRawTilt = -1;

  float gTgtPan()  const { return gesture == SHAKE ? gBasePan  + ((gPhase & 1) ? 25 : -25) : gBasePan; }
  float gTgtTilt() const { return gesture == NOD   ? gBaseTilt + ((gPhase & 1) ? 15 : -8)  : gBaseTilt; }
  int rawPan(float pan)  const { return constrain((int)lroundf(YAW_ZERO + (pan - 90) * STEPS_PER_DEG), 0, 1000); }
  int rawTilt(float tilt) const {
    float pitchDeg = pitchCenterDeg + (tilt - 90) * 1.4f;               // tilt 62..118 -> pitch ~5..85
    pitchDeg = constrain(pitchDeg, 5.f, 85.f);                           // manual: keep the Y servo within 5-85 deg
    return constrain((int)lroundf(PITCH_ZERO + pitchDeg * STEPS_PER_DEG), 0, 1000);
  }
  void write(float pan, float tilt, bool force) {
    int rp = rawPan(pan), rt = rawTilt(tilt);
    if (force || rp != lastRawPan)  { bus.WritePos(YAW_ID, rp, 20, 0);   lastRawPan = rp; }
    if (force || rt != lastRawTilt) { bus.WritePos(PITCH_ID, rt, 20, 0); lastRawTilt = rt; }
  }
};
