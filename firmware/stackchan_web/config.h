// Build-time defaults. Everything here can be overridden at runtime over the USB serial console
// (values are stored in NVS), and you can keep private values in config_local.h (git-ignored).
#pragma once

#define CFG_HOSTNAME     "stackchan"                         // mDNS name -> http://stackchan.local
#define CFG_TTS_URL      "http://192.168.1.10:9001/say"      // server/tts_proxy.py on your Mac
#define CFG_BRAIN_URL    "http://192.168.1.10:9002/visit"    // server/brain.py on your Mac (/learn is derived)
#define CFG_FETCH_TARGET ""                                  // default URL for /api/fetch (optional)
#define CFG_GREETING     "こんにちはー。スタックチャンやで。"        // spoken once after Wi-Fi connects (weather is NOT faked here)
#define CFG_SPEAKER_VOLUME 255                               // 0..255 (max)

// Board capabilities (auto): CoreS3 has the camera + esp-dl face tracking; Core2 on a StackChan base has no camera
#if defined(CONFIG_IDF_TARGET_ESP32S3)
#define HAS_CAMERA 1
#define HAS_MIC 1
#define CFG_SERVO_TX 6
#define CFG_SERVO_RX 7
#else
#define HAS_CAMERA 0
#define HAS_MIC 0            // Core2 on the StackChan base: the PDM mic (GPIO0/34) yields no audio (bus conflict) - talk only
// To listen with a Core2 anyway, plug an M5 PDM Unit (SPM1423) into Port A and set (in config_local.h):
//   #undef HAS_MIC
//   #define HAS_MIC 1
//   #define CFG_EXT_PDM_CLK 33   // Port A SCL
//   #define CFG_EXT_PDM_DATA 32  // Port A SDA
#define CFG_SERVO_TX 27      // found by probing the M-Bus on a Core2 + StackChan base
#define CFG_SERVO_RX 19
#endif

// Face style: 0 = half-moon eyes (CoreS3 default), 1 = sleepy outline eyes: circle outline + half-closed lid line (Core2 default)
#ifndef CFG_FACE_STYLE
#define CFG_FACE_STYLE (HAS_CAMERA ? 0 : 1)
#endif

// Head (Feetech SCS bus servos of the official M5Stack StackChan)
#define CFG_PAN_SIGN   -1     // flip if the head turns away from a face horizontally
#define CFG_TILT_SIGN  -1     // flip if the head turns away from a face vertically
#define CFG_PITCH_CENTER_DEG 45   // pitch angle (official units, 5..85) that counts as "straight ahead"

// Behaviour timing (ms)
#define CFG_SEARCH_START_AFTER 8000     // no face for this long -> start sweeping
#define CFG_SEARCH_DURATION    90000    // give up after this long
#define CFG_SEARCH_SLEEP       600000   // ... and try again after this long
#define CFG_REVISIT_INTERVAL   (15UL * 60 * 1000)  // re-check a person who keeps sitting there

#if __has_include("config_local.h")
#include "config_local.h"   // your private overrides (same #defines) - not committed
#endif
