// Cartoon face: two half-lidded white eyes on black, both pupils looking the same way (no mouth).
#pragma once
#include <M5Unified.h>

class Face {
 public:
  void begin() {
    W = M5.Display.width(); H = M5.Display.height();
    canvas.setPsram(true);
    canvas.setColorDepth(16);
    canvas.createSprite(W, H);
    nextBlink = millis() + 2500;
    nextGaze  = millis() + 1500;
    dirty = true;
  }
  // 0.0 = closed, 1.0 = wide open (idle default is wide open like the reference picture)
  void setMouth(float open01) { mouthBase = constrain(open01, 0.f, 1.f); }
  void talk(uint32_t ms) { talkUntil = millis() + ms; }
  // text overlay (Japanese OK). size 1..3
  // External gaze target (e.g. a detected face): nx, ny in -1..1 (screen left/top = -1). valid=false -> idle wander.
  void setLook(float nx, float ny, bool valid) { lookValid = valid; if (valid) { lookNx = constrain(nx, -1.f, 1.f); lookNy = constrain(ny, -1.f, 1.f); lastLook = millis(); } }
  bool isTracking() const { return lookValid; }
  // show a JPEG (QVGA fits the screen) instead of the eyes for ms; takes ownership of the buffer
  void showImage(uint8_t* jpg, size_t len, uint32_t ms) { clearImage(); imgBuf = jpg; imgLen = len; imgUntil = millis() + ms; }
  void clearImage() { if (imgBuf) { free(imgBuf); imgBuf = nullptr; imgLen = 0; } imgUntil = 0; }
  void overlay(const String& text, uint32_t ms, int size = 1) { ovText = text; ovSize = constrain(size, 1, 3); ovUntil = millis() + ms; dirty = true; }

  void update() {
    uint32_t now = millis();
    if (now - lastFrame < 40) return;
    lastFrame = now;

    // --- blink ---
    if (!blinking && now >= nextBlink) { blinking = true; blinkStart = now; }
    if (blinking) {   // sleepy blink: lid drifts down (260 ms), rests closed (160 ms), drifts back up (380 ms)
      uint32_t e = now - blinkStart;
      if (e < 260)      lid = e / 260.f;
      else if (e < 420) lid = 1.f;
      else if (e < 800) lid = 1.f - (e - 420) / 380.f;
      else { blinking = false; lid = 0.f; nextBlink = now + 2500 + random(4000); }
    }

    // --- gaze: follow a tracked face, otherwise a slow, small idle wander ---
    if (lookValid) {
      gazeTx = lookNx * 24.f; gazeTy = lookNy * 5.f;
      gazeX += (gazeTx - gazeX) * 0.3f; gazeY += (gazeTy - gazeY) * 0.3f;
    } else {
      if (now >= nextGaze) { gazeTx = 10 + random(-6, 7); gazeTy = random(-2, 3); nextGaze = now + 3000 + random(4000); }
      gazeX += (gazeTx - gazeX) * 0.05f; gazeY += (gazeTy - gazeY) * 0.05f;
    }

    // --- mouth ---
    float target = mouthBase;
    if (now < talkUntil) {
      if (now >= nextTalkStep) { talkOpen = 0.15f + 0.85f * (random(100) / 100.f); nextTalkStep = now + 80 + random(120); }
      target = talkOpen;
    }
    mouthOpen += (target - mouthOpen) * 0.45f;
    talking = now < talkUntil;

    if (ovUntil && now > ovUntil) { ovUntil = 0; ovText = ""; }
    if (imgBuf && now > imgUntil) clearImage();
    draw();
  }

 private:
  M5Canvas canvas{&M5.Display};
  int W = 320, H = 240;
  uint32_t lastFrame = 0, nextBlink = 0, blinkStart = 0, nextGaze = 0, talkUntil = 0, nextTalkStep = 0, ovUntil = 0;
  bool blinking = false, dirty = false, talking = false;
  float gazeX = 10, gazeY = 0, gazeTx = 10, gazeTy = 0, lid = 0;   // lid: 0 open .. 1 closed
  bool lookValid = false; float lookNx = 0, lookNy = 0; uint32_t lastLook = 0;
  float mouthBase = 0.9f, mouthOpen = 0.9f, talkOpen = 0.5f;
  String ovText; int ovSize = 1;
  uint8_t* imgBuf = nullptr; size_t imgLen = 0; uint32_t imgUntil = 0;

  static constexpr uint16_t BG     = 0x0000;   // black background
  static constexpr uint16_t BLACK  = 0x0000;
  static constexpr uint16_t WHITE  = 0xFFFF;
  static constexpr uint16_t MOUTH_OUTLINE = 0xFFFF;   // white lip line
  static constexpr uint16_t MOUTH_INSIDE  = 0x0000;   // black inside
  static constexpr uint16_t THROAT        = 0x4208;   // dark gray shading

  // Eye = upper half of an ellipse on a flat baseline. Only the top outline is a curve.
  // Openness is expressed solely by the half-ellipse's height (aspect ratio):
  // lid 0 -> full height, lid 1 -> squashed onto the baseline. The pupil squashes with it.
  void drawEye(int cx, int cy, int px, int py, int /*inner*/) {
    const int RX = 40, RY = 33;
    const int base = cy + 12;                    // flat bottom edge
    float k = 1.f - 0.94f * lid;                 // vertical scale factor
    int ry = max(2, (int)(RY * k));
    canvas.fillEllipse(cx, base, RX, ry, WHITE);
    canvas.fillRect(cx - RX - 2, base + 1, 2 * RX + 4, ry + 2, BG);   // keep only the upper half
    int pr = 9, pry = max(1, (int)(pr * k));
    int pcy = base - (int)((14 - py) * k);       // pupil sits a little above the baseline
    canvas.fillEllipse(cx + px, pcy, pr, pry, BLACK);
  }

  void drawOverlay() {
    if (ovText.length()) {
      const lgfx::IFont* f = ovSize == 1 ? (const lgfx::IFont*)&fonts::lgfxJapanGothic_16
                           : ovSize == 2 ? (const lgfx::IFont*)&fonts::lgfxJapanGothic_24
                                         : (const lgfx::IFont*)&fonts::lgfxJapanGothic_36;
      int lh = ovSize == 1 ? 18 : ovSize == 2 ? 27 : 40;
      // split lines on '\n'
      int nLines = 1; for (char c : ovText) if (c == '\n') nLines++;
      int boxH = nLines * lh + 8;
      int y0 = (ovSize == 1) ? H - boxH : (H - boxH) / 2;   // small: bottom bar, large: centered
      canvas.fillRect(0, y0, W, boxH, WHITE);
      canvas.setTextColor(BLACK, WHITE);
      canvas.setFont(f);
      canvas.setTextSize(1);
      canvas.setTextDatum(top_center);
      int y = y0 + 4, from = 0;
      while (from <= (int)ovText.length()) {
        int nl = ovText.indexOf('\n', from); if (nl < 0) nl = ovText.length();
        canvas.drawString(ovText.substring(from, nl), W / 2, y);
        y += lh; from = nl + 1;
      }
      canvas.setTextDatum(top_left);
      canvas.setFont(&fonts::Font0);
    }
  }

  void draw() {
    canvas.fillScreen(BG);
    if (imgBuf) {   // photo mode: draw the JPEG, keep the text overlay
      canvas.drawJpg(imgBuf, imgLen, 0, 0, W, H, 0, 0, 1.0f, 1.0f, datum_t::middle_center);
      drawOverlay(); canvas.pushSprite(0, 0); return;
    }

    // eyes only, centered on the screen; both pupils point the same way
    int gx = (int)lroundf(gazeX), gy = (int)lroundf(gazeY);   // no jitter: pupils move smoothly only
    const int ey = H / 2;
    drawEye(W / 2 - 60, ey, gx, gy, +1);
    drawEye(W / 2 + 60, ey, gx, gy, -1);

    drawOverlay();
    canvas.pushSprite(0, 0);
  }
};
