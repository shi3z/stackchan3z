// Build-time defaults. Everything here can be overridden at runtime over the USB serial console
// (values are stored in NVS), and you can keep private values in config_local.h (git-ignored).
#pragma once

#define CFG_HOSTNAME     "stackchan"                         // mDNS name -> http://stackchan.local
#define CFG_TTS_URL      "http://192.168.1.10:9001/say"      // server/tts_proxy.py on your Mac
#define CFG_BRAIN_URL    "http://192.168.1.10:9002/visit"    // server/brain.py on your Mac (/learn is derived)
#define CFG_FETCH_TARGET ""                                  // default URL for /api/fetch (optional)
#define CFG_GREETING     "こんにちはー。今日もええ天気やなあ。"     // spoken once after Wi-Fi connects
#define CFG_SPEAKER_VOLUME 200                               // 0..255

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
