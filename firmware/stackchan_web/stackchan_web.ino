// Stack-chan Web Server
// - Wi-Fi credentials are stored in NVS and can be set over USB serial:
//     wifi <ssid> <password>
//     target <url>          (tailnet server URL used by /api/fetch and "fetch")
//     fetch                 (GET target URL and print the response)
//     status | reboot | clear
// - HTTP server on port 80: /  /api/status  /api/fetch?url=...  /api/display?text=...
// - mDNS: http://stackchan.local

#include <M5Unified.h>
#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <Wire.h>
#include <ArduinoJson.h>
#include "camera.h"
#include "human_face_detect_msr01.hpp"
#include "human_face_detect_mnp01.hpp"
#include "config.h"
#include "face.h"
#include "head.h"
#include "ioexpander.h"

Face face;
Head head;
BaseIOExpander baseIO;
CoreS3Camera cam;
bool g_camOk = false;
SemaphoreHandle_t g_camMutex;
// ---- face tracking / auto comment state ----
volatile bool  g_track = true;             // run the on-device face detector
volatile float g_faceNx = 0, g_faceNy = 0; // last face center, -1..1
volatile uint32_t g_faceSeen = 0;          // millis() of last detection
volatile int g_faceW = 0;
volatile uint32_t g_detMs = 0, g_detCount = 0, g_detHits = 0;   // stats: last inference time, frames processed, faces found
int  g_mirror = 0;                          // 1 = flip horizontally if the eyes look the wrong way
bool g_autoComment = true;                  // comment on clothing when a face appears
volatile bool g_commentBusy = false;
uint32_t g_lastComment = 0, g_faceSince = 0, g_faceGone = 0, g_wavStart = 0;
bool g_facePresent = false, g_commentedThisVisit = false;
// ---- head follows the face ----
bool g_headFollow = true; int g_panSign = CFG_PAN_SIGN, g_tiltSign = CFG_TILT_SIGN;
uint32_t g_lastHeadStep = 0; bool g_headCentered = true;
uint32_t g_holdUntil = 0;   // head frozen until this time (photo moment)
float g_prevNx = 0, g_prevNy = 0; int g_panWorse = 0, g_tiltWorse = 0; bool g_stepPending = false;
// ---- search for a face when none is visible ----
enum SearchState { SEARCH_NONE, SEARCH_ACTIVE, SEARCH_SLEEP };
SearchState g_search = SEARCH_NONE;
uint32_t g_searchStart = 0, g_searchWakeAt = 0; int g_searchLeg = 0;
const uint32_t SEARCH_START_AFTER_MS = CFG_SEARCH_START_AFTER, SEARCH_DURATION_MS = CFG_SEARCH_DURATION, SEARCH_SLEEP_MS = CFG_SEARCH_SLEEP;
void startSearch() {
  g_search = SEARCH_ACTIVE; g_searchStart = millis(); g_searchLeg = 0;
  head.moveTo(30, 90, 2.0f);
  Serial.println("[search] looking for a face...");
}
String g_commentUrl = CFG_BRAIN_URL;   // brain: face recognition + greeting; /learn on the same host
String g_lastCommentText, g_lastName;

static const char* HOSTNAME = CFG_HOSTNAME;

Preferences prefs;
WebServer server(80);

String g_ssid, g_pass, g_target, g_gw, g_tts, g_greet;
uint8_t* g_wav = nullptr;   // WAV currently playing (PSRAM); freed when playback ends
String g_lastFetch = "";
unsigned long g_lastReconnect = 0;
int g_reqCount = 0;


const char* boardName() {
  switch (M5.getBoard()) {
    case m5::board_t::board_M5StackCoreS3:   return "M5StackCoreS3";
    case m5::board_t::board_M5StackCoreS3SE: return "M5StackCoreS3SE";
    case m5::board_t::board_M5StackCore2:    return "M5StackCore2";
    case m5::board_t::board_M5Stack:         return "M5Stack";
    case m5::board_t::board_M5AtomS3:        return "M5AtomS3";
    case m5::board_t::board_M5StampS3:       return "M5StampS3";
    case m5::board_t::board_M5Cardputer:     return "M5Cardputer";
    default: { static char b[24]; snprintf(b, sizeof b, "board_%d", (int)M5.getBoard()); return b; }
  }
}

// ---------- display ----------
void drawStatus(const String& line1, const String& line2 = "", const String& line3 = "") {
  String t = line1; if (line2.length()) t += "  " + line2; if (line3.length()) t += "  " + line3;
  face.overlay(t, 6000);
}
void showConnected() {
  face.overlay("IP " + WiFi.localIP().toString() + "  " + String(HOSTNAME) + ".local", 6000);
}

// ---------- outbound fetch ----------
String doFetch(const String& url, int* codeOut) {
  HTTPClient http;
  http.setTimeout(8000);
  if (!http.begin(url)) { if (codeOut) *codeOut = -1; return "begin failed"; }
  int code = http.GET();
  String body = (code > 0) ? http.getString() : http.errorToString(code);
  http.end();
  if (codeOut) *codeOut = code;
  return body;
}

// ---------- speech ----------
String urlEncode(const String& in) {
  String out; out.reserve(in.length() * 3);
  for (size_t i = 0; i < in.length(); i++) {
    unsigned char c = in[i];
    if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') out += (char)c;
    else { char b[4]; snprintf(b, sizeof b, "%%%02X", c); out += b; }
  }
  return out;
}

String urlDecode(const String& in) {
  String out; out.reserve(in.length());
  for (size_t i = 0; i < in.length(); i++) {
    char c = in[i];
    if (c == '%' && i + 2 < in.length()) { out += (char)strtol(in.substring(i + 1, i + 3).c_str(), nullptr, 16); i += 2; }
    else if (c == '+') out += ' '; else out += c;
  }
  return out;
}

// read exactly len bytes of the response body into PSRAM (nullptr on failure)
uint8_t* readBody(HTTPClient& http, int len) {
  uint8_t* buf = (uint8_t*)ps_malloc(len);
  if (!buf) buf = (uint8_t*)malloc(len);
  if (!buf) return nullptr;
  WiFiClient* st = http.getStreamPtr();
  int got = 0; uint32_t t0 = millis();
  while (got < len && millis() - t0 < 30000) {
    int n = st->read(buf + got, len - got);
    if (n > 0) { got += n; t0 = millis(); } else delay(1);
  }
  if (got < len) { free(buf); return nullptr; }
  return buf;
}

void playBuf(uint8_t* buf, int len) {
  M5.Speaker.stop();
  if (g_wav) { free(g_wav); g_wav = nullptr; }
  g_wav = buf; g_wavStart = millis();
  M5.Speaker.playWav(g_wav, len);
  face.talk(len / 32);   // 16 kHz * 2 bytes = 32 bytes per ms
}

// Download a WAV from the TTS proxy and play it on the speaker (non-blocking; buffer freed in loop()).
bool say(const String& text, const String& emotion = "happy") {
  if (!g_tts.length() || WiFi.status() != WL_CONNECTED) { Serial.println("[say] no tts url / no wifi"); return false; }
  String url = g_tts + "?text=" + urlEncode(text) + "&emotion=" + urlEncode(emotion);
  HTTPClient http; http.setTimeout(30000);
  if (!http.begin(url)) return false;
  int code = http.GET(); int len = http.getSize();
  if (code != 200 || len <= 44) { Serial.printf("[say] http %d len %d\n", code, len); http.end(); return false; }
  uint8_t* buf = readBody(http, len); http.end();
  if (!buf) { Serial.println("[say] download failed"); return false; }
  playBuf(buf, len);
  Serial.printf("[say] playing %d bytes: %s\n", len, text.c_str());
  return true;
}

// ---------- camera capture (JPEG) ----------
// returns malloc'd JPEG (caller frees) or nullptr. Takes the camera mutex.
uint8_t* captureJpeg(size_t* outLen, int q = 80) {
  if (!g_camOk) return nullptr;
  uint8_t* jpg = nullptr; size_t jlen = 0;
  if (xSemaphoreTake(g_camMutex, pdMS_TO_TICKS(2000)) != pdTRUE) return nullptr;
  if (cam.get()) cam.free();                       // drop a possibly stale frame
  if (cam.get()) { if (!frame2jpg(cam.fb, q, &jpg, &jlen)) jpg = nullptr; cam.free(); }
  xSemaphoreGive(g_camMutex);
  *outLen = jlen; return jpg;
}

// ---------- face detection task (core 0) ----------
void faceTask(void*) {
  HumanFaceDetectMSR01 s1(0.1F, 0.5F, 10, 0.2F);
  HumanFaceDetectMNP01 s2(0.5F, 0.3F, 5);
  for (;;) {
    if (!g_track || !g_camOk) { vTaskDelay(pdMS_TO_TICKS(200)); continue; }
    // copy the frame to our own PSRAM buffer and return it immediately so the driver never starves
    static uint16_t* frame = nullptr; int w = 0, h = 0; bool have = false;
    if (xSemaphoreTake(g_camMutex, pdMS_TO_TICKS(100)) == pdTRUE) {
      if (cam.get()) {
        w = cam.fb->width; h = cam.fb->height;
        if (!frame) frame = (uint16_t*)ps_malloc(w * h * 2);
        if (frame) { memcpy(frame, cam.fb->buf, w * h * 2); have = true; }
        cam.free();
      }
      xSemaphoreGive(g_camMutex);
    }
    if (have) {
      {
        uint32_t t0 = millis();
        std::list<dl::detect::result_t>& cand = s1.infer(frame, {h, w, 3});
        std::list<dl::detect::result_t>& res  = s2.infer(frame, {h, w, 3}, cand);
        g_detMs = millis() - t0; g_detCount++;
        if (!res.empty()) {
          g_detHits++;
          const dl::detect::result_t* best = nullptr; int bestArea = 0;   // largest face wins
          for (auto& r : res) { int a = (r.box[2] - r.box[0]) * (r.box[3] - r.box[1]); if (a > bestArea) { bestArea = a; best = &r; } }
          float cx = (best->box[0] + best->box[2]) * 0.5f, cy = (best->box[1] + best->box[3]) * 0.5f;
          float nx = cx / w * 2.f - 1.f, ny = cy / h * 2.f - 1.f;
          if (g_mirror) nx = -nx;
          g_faceNx = nx; g_faceNy = ny; g_faceW = best->box[2] - best->box[0]; g_faceSeen = millis();
        }
      }
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

// ---------- mic recording (16 kHz mono 16-bit WAV in PSRAM) ----------
uint8_t* recordWav(uint32_t seconds, size_t* outLen) {
  const uint32_t rate = 16000; const size_t samples = rate * seconds;
  uint8_t* wav = (uint8_t*)ps_malloc(44 + samples * 2);
  if (!wav) return nullptr;
  int16_t* pcm = (int16_t*)(wav + 44);
  M5.Speaker.end(); M5.Mic.begin();
  size_t off = 0; const size_t chunk = 512;
  while (off < samples) {
    size_t n = min(chunk, samples - off);
    if (M5.Mic.record(pcm + off, n, rate, false)) { off += n; while (M5.Mic.isRecording()) delay(1); }
    else delay(1);
  }
  M5.Mic.end(); M5.Speaker.begin();
  uint32_t dataLen = samples * 2, byteRate = rate * 2;
  memcpy(wav, "RIFF", 4); uint32_t v = 36 + dataLen; memcpy(wav + 4, &v, 4); memcpy(wav + 8, "WAVEfmt ", 8);
  v = 16; memcpy(wav + 16, &v, 4); uint16_t h = 1; memcpy(wav + 20, &h, 2); memcpy(wav + 22, &h, 2);
  memcpy(wav + 24, &rate, 4); memcpy(wav + 28, &byteRate, 4); h = 2; memcpy(wav + 32, &h, 2); h = 16; memcpy(wav + 34, &h, 2);
  memcpy(wav + 36, "data", 4); memcpy(wav + 40, &dataLen, 4);
  *outLen = 44 + dataLen; return wav;
}

void waitSpeechDone(uint32_t maxMs = 15000) {
  uint32_t t0 = millis();
  while (millis() - t0 < maxMs && (M5.Speaker.isPlaying() || (g_wav && millis() - g_wavStart < 300))) delay(20);
}

// POST binary to url, parse the JSON reply. Returns HTTP code.
int postJson(const String& url, const char* contentType, const uint8_t* body, size_t len, JsonDocument& doc, uint32_t timeoutMs) {
  HTTPClient http; http.setTimeout(timeoutMs);
  if (!http.begin(url)) return -1;
  http.addHeader("Content-Type", contentType);
  int code = http.POST((uint8_t*)body, len);
  String resp = (code > 0) ? http.getString() : "";
  http.end();
  if (code > 0 && deserializeJson(doc, resp)) return -2;
  return code;
}

// download a JPEG (absolute URL, or a path relative to the brain host) and show it on the screen for ms
bool showImageFromUrl(String url, uint32_t ms) {
  if (url.startsWith("/")) { int i = g_commentUrl.indexOf('/', 8); url = (i > 0 ? g_commentUrl.substring(0, i) : g_commentUrl) + url; }
  HTTPClient http; http.setTimeout(15000);
  if (!http.begin(url)) return false;
  int code = http.GET(), len = http.getSize();
  if (code != 200 || len <= 0) { http.end(); Serial.printf("[image] http %d\n", code); return false; }
  uint8_t* buf = readBody(http, len); http.end();
  if (!buf) return false;
  face.showImage(buf, len, ms);
  return true;
}

// Tilt the head down, photograph the clothes, ask the brain for a comment, come back up, say it.
void clothesShot(const String& name) {
  waitSpeechDone();
  float pan0 = head.pan(), tilt0 = head.tilt();
  g_holdUntil = millis() + 8000;                               // no face-follow while we look down
  head.moveTo(pan0, tilt0 + g_tiltSign * 30.f, 6.f);           // "down" is the direction that follows a face lower in the image
  delay(1500);                                                 // settle
  size_t jlen = 0; uint8_t* jpg = captureJpeg(&jlen, 85);
  head.moveTo(pan0, tilt0, 6.f);                               // back to the face
  g_holdUntil = millis() + 1200;
  if (!jpg) { Serial.println("[clothes] capture failed"); return; }
  String url = g_commentUrl; url.replace("/visit", "/clothes");
  JsonDocument d;
  int code = postJson(url + "?name=" + urlEncode(name), "image/jpeg", jpg, jlen, d, 120000);
  free(jpg);
  String say_ = d["say"] | "";
  Serial.printf("[clothes] http %d say=%s\n", code, say_.c_str());
  if (say_.length()) { face.overlay(say_, 6000, 1); say(say_, "happy"); }
}

// ---------- visit task (core 1, one-shot): photo -> brain -> greet / ask the name -> listen -> remember ----------
void visitTask(void*) {
  size_t jlen = 0; uint8_t* jpg = captureJpeg(&jlen, 85);
  if (!jpg) { Serial.println("[visit] capture failed"); g_commentBusy = false; vTaskDelete(nullptr); return; }
  JsonDocument doc;
  int code = postJson(g_commentUrl, "image/jpeg", jpg, jlen, doc, 120000);
  free(jpg);
  if (code != 200) { Serial.printf("[visit] http %d\n", code); g_commentBusy = false; vTaskDelete(nullptr); return; }
  bool known = doc["known"] | false;
  String say_ = doc["say"] | "", name = doc["name"] | "", show = doc["show"] | "";
  bool clothes = doc["clothes"] | false;
  Serial.printf("[visit] person=%d known=%d name=%s say=%s\n", (int)(doc["person"] | false), known, name.c_str(), say_.c_str());
  if (known && !say_.length()) face.overlay(name + "さん", 2500, 1);
  if (!say_.length() && !clothes && !doc["listen"].is<JsonObject>() && !doc["display"].is<JsonObject>() && !doc["recheck"].is<int>()) { g_commentBusy = false; vTaskDelete(nullptr); return; }
  g_lastCommentText = say_; if (known) g_lastName = name;
  if (say_.length()) {
    face.overlay(known ? (name + "さん  " + say_) : say_, 6000, 1);
    say(say_, known ? "happy" : "neutral");
    if (show.length()) { showImageFromUrl(show, 8000); }   // e.g. the photo of a visitor being reported to the owner
  }
  if (clothes && head.isAttached()) clothesShot(name);
  // generic "listen" step: the brain wants an answer (name of a stranger, a profile question, ...)
  if (doc["listen"].is<JsonObject>()) {
    String lurl = doc["listen"]["url"] | ""; int secs = doc["listen"]["seconds"] | 4;
    if (lurl.startsWith("/")) { int i = g_commentUrl.indexOf('/', 8); lurl = (i > 0 ? g_commentUrl.substring(0, i) : g_commentUrl) + lurl; }
    waitSpeechDone();
    face.overlay("きいてるで... (" + String(secs) + "秒)", secs * 1000 + 500, 2);
    size_t wlen = 0; uint8_t* wav = recordWav(secs, &wlen);
    if (wav) {
      face.overlay("かんがえ中...", 30000, 2);
      JsonDocument doc2;
      int c2 = postJson(lurl, "audio/wav", wav, wlen, doc2, 300000);
      free(wav);
      String reply = doc2["say"] | "", heard = doc2["heard"] | "", nm = doc2["name"] | "";
      Serial.printf("[listen] http %d heard=%s reply=%s\n", c2, heard.c_str(), reply.c_str());
      if (nm.length()) g_lastName = nm;
      if (reply.length()) { face.overlay(reply, 5000, 1); say(reply, "happy"); }
      else face.overlay("", 1, 1);
    }
  }
  // the brain wants another look soon (e.g. to confirm the owner is really dozing)
  if (doc["recheck"].is<int>()) {
    uint32_t sec = doc["recheck"] | 0;
    if (sec > 0 && sec * 1000 < CFG_REVISIT_INTERVAL) g_lastComment = millis() - (CFG_REVISIT_INTERVAL - sec * 1000);
  }
  // a topic to post on the screen (news picked for the owner)
  if (doc["display"].is<JsonObject>()) {
    String t = doc["display"]["text"] | ""; int sz = doc["display"]["size"] | 1; uint32_t ms = doc["display"]["ms"] | 20000;
    if (t.length()) { waitSpeechDone(); face.overlay(t, ms, sz); }
  }
  g_commentBusy = false;
  vTaskDelete(nullptr);
}

bool startComment() {
  if (g_commentBusy || WiFi.status() != WL_CONNECTED || !g_camOk) return false;
  g_commentBusy = true; g_lastComment = millis();
  if (xTaskCreatePinnedToCore(visitTask, "visit", 16384, nullptr, 1, nullptr, 1) != pdPASS) { g_commentBusy = false; return false; }
  return true;
}

// ---------- HTTP handlers ----------
String jsonEscape(const String& s) {
  String o; o.reserve(s.length() + 8);
  for (char c : s) {
    if (c == '"') o += "\\\""; else if (c == '\\') o += "\\\\";
    else if (c == '\n') o += "\\n"; else if (c == '\r') {} else o += c;
  }
  return o;
}

void handleRoot() {
  g_reqCount++;
  String html = "<!doctype html><meta charset=utf-8><title>Stack-chan</title>"
    "<style>body{font-family:sans-serif;margin:2em}code{background:#eee;padding:2px 4px}</style>"
    "<h1>Stack-chan Web Server</h1>"
    "<p>Board: <b>" + String(boardName()) + "</b></p>"
    "<p>IP: <b>" + WiFi.localIP().toString() + "</b> / RSSI " + String(WiFi.RSSI()) + " dBm</p>"
    "<p>Uptime: " + String(millis() / 1000) + " s / Requests: " + String(g_reqCount) + "</p>"
    "<p>Target: <code>" + (g_target.length() ? g_target : String("(none)")) + "</code></p>"
    "<ul><li><a href=/api/status>/api/status</a></li>"
    "<li><a href=/api/fetch>/api/fetch</a> (GET target; or ?url=...)</li>"
    "<li><a href='/api/display?text=Hello&size=2&ms=3000'>/api/display?text=...&size=1..3&ms=</a> (Japanese OK, \\n = newline; &image=URL shows a JPEG)</li>"
    "<li><a href='/api/head?gesture=nod'>/api/head?gesture=nod|shake|center&n=2</a> / <a href='/api/head?pan=60&tilt=90'>/api/head?pan=&tilt=&speed=</a></li>"
    "<li><a href='/api/camera.jpg'>/api/camera.jpg?q=80</a></li>"
    "<li><a href='/api/track'>/api/track?on=1&auto=1&mirror=0&head=1&pansign=1&tiltsign=1</a> (face tracking, head follow, auto comment)</li>"
    "<li><a href='/api/comment'>/api/comment</a> (photo -> VLM -> speak now)</li>"
    "<li><a href='/api/talk?ms=3000'>/api/talk?ms=3000</a> (mouth flap)</li>"
    "<li><a href='/api/face?mouth=0.2'>/api/face?mouth=0.2</a> (resting mouth 0..1)</li>"
    "<li><a href='/api/say?text=%E3%81%93%E3%82%93%E3%81%AB%E3%81%A1%E3%81%AF%E3%83%BC'>/api/say?text=...</a> (speak via TTS proxy)</li></ul>";
  server.send(200, "text/html", html);
}

void handleStatus() {
  g_reqCount++;
  String j = "{";
  j += "\"board\":\"" + String(boardName()) + "\",";
  j += "\"ip\":\"" + WiFi.localIP().toString() + "\",";
  j += "\"mac\":\"" + WiFi.macAddress() + "\",";
  j += "\"rssi\":" + String(WiFi.RSSI()) + ",";
  j += "\"uptime_s\":" + String(millis() / 1000) + ",";
  j += "\"free_heap\":" + String(ESP.getFreeHeap()) + ",";
  j += "\"target\":\"" + jsonEscape(g_target) + "\",";
  j += "\"camera\":" + String(g_camOk ? "true" : "false") + ",";
  j += "\"face\":" + String(millis() - g_faceSeen < 700 ? "true" : "false") + ",\"track\":" + String(g_track ? "true" : "false") + ",";
  j += "\"head\":{\"attached\":" + String(head.isAttached() ? "true" : "false") + ",\"servos\":" + String(head.servosFound() ? "true" : "false") + ",\"pan\":" + String(head.pan(), 1) +
       ",\"tilt\":" + String(head.tilt(), 1) + "},";
  j += "\"requests\":" + String(g_reqCount);
  j += "}";
  server.send(200, "application/json", j);
}

void handleFetch() {
  g_reqCount++;
  String url = server.hasArg("url") ? server.arg("url") : g_target;
  if (!url.length()) { server.send(400, "application/json", "{\"error\":\"no url; set target or pass ?url=\"}"); return; }
  int code = 0;
  String body = doFetch(url, &code);
  g_lastFetch = body;
  String j = "{\"url\":\"" + jsonEscape(url) + "\",\"code\":" + String(code) +
             ",\"body\":\"" + jsonEscape(body.substring(0, 2000)) + "\"}";
  server.send(200, "application/json", j);
}

void handleDisplay() {
  g_reqCount++;
  face.overlay(server.arg("text"), 4000);
  server.send(200, "application/json", "{\"ok\":true}");
}

// /api/talk?ms=2000  : mouth flapping for ms milliseconds
void handleTalk() {
  g_reqCount++;
  uint32_t ms = server.hasArg("ms") ? server.arg("ms").toInt() : 2000;
  face.talk(ms);
  server.send(200, "application/json", "{\"ok\":true,\"ms\":" + String(ms) + "}");
}

// /api/say?text=...&emotion=happy
void handleSay() {
  g_reqCount++;
  if (!server.hasArg("text")) { server.send(400, "application/json", "{\"error\":\"text required\"}"); return; }
  bool ok = say(server.arg("text"), server.hasArg("emotion") ? server.arg("emotion") : "happy");
  server.send(ok ? 200 : 502, "application/json", ok ? "{\"ok\":true}" : "{\"ok\":false}");
}

// /api/display?text=...&ms=4000&size=1..3   (Japanese OK, "\n" for new lines)
void handleDisplay2() {
  g_reqCount++;
  String t = server.arg("text"); t.replace("\\n", "\n");
  uint32_t ms = server.hasArg("ms") ? server.arg("ms").toInt() : 4000;
  if (server.hasArg("image")) {
    bool ok = showImageFromUrl(server.arg("image"), ms);
    if (t.length()) face.overlay(t, ms, server.hasArg("size") ? server.arg("size").toInt() : 1);
    server.send(ok ? 200 : 502, "application/json", ok ? "{\"ok\":true}" : "{\"ok\":false,\"error\":\"image download failed\"}");
    return;
  }
  int size = server.hasArg("size") ? server.arg("size").toInt() : 1;
  face.overlay(t, ms, size);
  server.send(200, "application/json", "{\"ok\":true}");
}

// /api/head?pan=90&tilt=90&speed=3   |  /api/head?gesture=nod|shake|center&n=2  |  /api/head?off=1
void handleHead() {
  g_reqCount++;
  if (server.hasArg("off")) { head.end(); server.send(200, "application/json", "{\"ok\":true,\"attached\":false}"); return; }
  if (!head.isAttached()) head.begin();
  if (server.hasArg("gesture")) {
    String g = server.arg("gesture"); int n = server.hasArg("n") ? server.arg("n").toInt() : 2;
    if (g == "nod") head.nod(n); else if (g == "shake") head.shake(n); else if (g == "center") head.center();
    else { server.send(400, "application/json", "{\"error\":\"gesture must be nod|shake|center\"}"); return; }
  } else if (server.hasArg("pan") || server.hasArg("tilt")) {
    float pan = server.hasArg("pan") ? server.arg("pan").toFloat() : head.pan();
    float tilt = server.hasArg("tilt") ? server.arg("tilt").toFloat() : head.tilt();
    float sp = server.hasArg("speed") ? server.arg("speed").toFloat() : 3.0f;
    head.moveTo(pan, tilt, sp);
  }
  String j = "{\"ok\":true,\"pan\":" + String(head.pan(), 1) + ",\"tilt\":" + String(head.tilt(), 1) +
             "}";
  server.send(200, "application/json", j);
}

// /api/camera.jpg?q=80   (QVGA 320x240)
void handleCamera() {
  g_reqCount++;
  if (!g_camOk) { server.send(503, "application/json", "{\"error\":\"camera not initialized\"}"); return; }
  int q = server.hasArg("q") ? constrain(server.arg("q").toInt(), 10, 100) : 80;
  size_t jlen = 0; uint8_t* jpg = captureJpeg(&jlen, q);
  if (!jpg) { server.send(500, "application/json", "{\"error\":\"capture failed\"}"); return; }
  server.setContentLength(jlen);
  server.send(200, "image/jpeg", "");
  // write in chunks and keep going while the socket buffer drains (slow clients via tailscale)
  WiFiClient c = server.client();
  size_t sent = 0; int idle = 0;
  while (sent < jlen && c.connected() && idle < 3000) {
    size_t n = c.write(jpg + sent, min((size_t)1024, jlen - sent));
    if (n > 0) { sent += n; idle = 0; } else { delay(1); idle++; }
  }
  c.flush();
  if (sent < jlen) Serial.printf("[camera] short send %u/%u\n", (unsigned)sent, (unsigned)jlen);
  free(jpg);
}

// /api/track?on=1|0&mirror=0|1&auto=1|0   /api/track (status)
void handleTrack() {
  g_reqCount++;
  if (server.hasArg("on")) g_track = server.arg("on").toInt() != 0;
  if (server.hasArg("auto")) g_autoComment = server.arg("auto").toInt() != 0;
  if (server.hasArg("mirror")) { g_mirror = server.arg("mirror").toInt(); prefs.begin("app", false); prefs.putInt("mirror", g_mirror); prefs.end(); }
  if (server.hasArg("head")) g_headFollow = server.arg("head").toInt() != 0;
  if (server.hasArg("search")) { if (server.arg("search").toInt()) startSearch(); else { g_search = SEARCH_NONE; head.center(); g_headCentered = true; } }
  if (server.hasArg("pansign") || server.hasArg("tiltsign")) {
    if (server.hasArg("pansign")) g_panSign = server.arg("pansign").toInt() < 0 ? -1 : 1;
    if (server.hasArg("tiltsign")) g_tiltSign = server.arg("tiltsign").toInt() < 0 ? -1 : 1;
    prefs.begin("app", false); prefs.putInt("psign", g_panSign); prefs.putInt("tsign", g_tiltSign); prefs.end();
  }
  bool seen = millis() - g_faceSeen < 700;
  String j = "{\"track\":" + String(g_track ? "true" : "false") + ",\"auto_comment\":" + String(g_autoComment ? "true" : "false") +
             ",\"mirror\":" + String(g_mirror) + ",\"head_follow\":" + String(g_headFollow ? "true" : "false") +
             ",\"pansign\":" + String(g_panSign) + ",\"tiltsign\":" + String(g_tiltSign) +
             ",\"pan\":" + String(head.pan(), 1) + ",\"tilt\":" + String(head.tilt(), 1) +
             ",\"search\":\"" + String(g_search == SEARCH_ACTIVE ? "searching" : g_search == SEARCH_SLEEP ? "sleeping" : "none") + "\"" +
             ",\"face\":" + String(seen ? "true" : "false") +
             ",\"nx\":" + String(g_faceNx, 2) + ",\"ny\":" + String(g_faceNy, 2) + ",\"w\":" + String(g_faceW) +
             ",\"det_ms\":" + String(g_detMs) + ",\"det_frames\":" + String(g_detCount) + ",\"det_hits\":" + String(g_detHits) +
             ",\"det_fps\":" + String(millis() > 0 ? g_detCount * 1000.0f / millis() : 0, 2) +
             ",\"last_comment\":\"" + jsonEscape(g_lastCommentText) + "\",\"last_name\":\"" + jsonEscape(g_lastName) + "\"}";
  server.send(200, "application/json", j);
}

// /api/comment : take a photo now and comment on the clothing (async)
void handleComment() {
  g_reqCount++;
  bool ok = startComment();
  server.send(ok ? 202 : 409, "application/json", ok ? "{\"ok\":true,\"started\":true}" : "{\"ok\":false,\"busy\":true}");
}

// /api/face?mouth=0..1  : resting mouth openness
void handleFace() {
  g_reqCount++;
  if (server.hasArg("mouth")) face.setMouth(server.arg("mouth").toFloat());
  server.send(200, "application/json", "{\"ok\":true}");
}

void handleNotFound() { server.send(404, "text/plain", "not found"); }

// ---------- Wi-Fi ----------
void applyGateway() {
  if (!g_gw.length()) return;
  IPAddress gw; if (!gw.fromString(g_gw)) { Serial.println("[gw] bad ip"); return; }
  IPAddress ip = WiFi.localIP(), mask = WiFi.subnetMask(), dns = WiFi.dnsIP();
  if (WiFi.config(ip, gw, mask, dns)) Serial.printf("[gw] using gateway %s (ip %s)\n", g_gw.c_str(), ip.toString().c_str());
  else Serial.println("[gw] config failed");
}

bool connectWiFi(uint32_t timeoutMs = 20000) {
  if (!g_ssid.length()) return false;
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOSTNAME);
  WiFi.begin(g_ssid.c_str(), g_pass.c_str());
  drawStatus("Connecting to " + g_ssid + " ...");
  Serial.printf("[wifi] connecting to %s\n", g_ssid.c_str());
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeoutMs) { delay(250); Serial.print('.'); }
  Serial.println();
  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("[wifi] failed (status=%d)\n", WiFi.status());
    drawStatus("Wi-Fi connect failed", "SSID: " + g_ssid, "send: wifi <ssid> <pass>");
    return false;
  }
  Serial.printf("[wifi] connected  IP=%s  RSSI=%d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  static bool serverStarted = false;
  if (!serverStarted) { server.begin(); serverStarted = true; Serial.println("[http] server started on :80"); }
  applyGateway();
  if (MDNS.begin(HOSTNAME)) { MDNS.addService("http", "tcp", 80); Serial.printf("[mdns] http://%s.local\n", HOSTNAME); }
  showConnected();
  return true;
}

// ---------- serial console ----------
void saveWifi(const String& ssid, const String& pass) {
  prefs.begin("wifi", false); prefs.putString("ssid", ssid); prefs.putString("pass", pass); prefs.end();
  g_ssid = ssid; g_pass = pass;
}
void saveTarget(const String& url) {
  prefs.begin("app", false); prefs.putString("target", url); prefs.end();
  g_target = url;
}

void printStatus() {
  bool up = WiFi.status() == WL_CONNECTED;
  Serial.printf("[status] board=%s ssid=%s wifi=%s ip=%s rssi=%d gw=%s target=%s heap=%u\n",
    boardName(), g_ssid.c_str(), up ? "connected" : "disconnected",
    up ? WiFi.localIP().toString().c_str() : "-", up ? WiFi.RSSI() : 0,
    up ? WiFi.gatewayIP().toString().c_str() : "-", g_target.c_str(), ESP.getFreeHeap());
}

void handleSerialLine(String line) {
  line.trim();
  if (!line.length()) return;
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String rest = sp < 0 ? "" : line.substring(sp + 1);
  rest.trim();
  if (cmd == "wifi") {
    int sp2 = rest.indexOf(' ');
    String ssid = sp2 < 0 ? rest : rest.substring(0, sp2);
    String pass = sp2 < 0 ? "" : rest.substring(sp2 + 1);
    saveWifi(ssid, pass);
    Serial.printf("[ok] wifi saved ssid=%s\n", ssid.c_str());
    WiFi.disconnect(true, true);
    delay(200);
    connectWiFi();
  } else if (cmd == "target") {
    saveTarget(rest);
    Serial.printf("[ok] target=%s\n", g_target.c_str());
    if (WiFi.status() == WL_CONNECTED) showConnected();
  } else if (cmd == "fetch") {
    String url = rest.length() ? rest : g_target;
    if (!url.length()) { Serial.println("[err] no target"); return; }
    int code = 0;
    String body = doFetch(url, &code);
    Serial.printf("[fetch] %s -> %d\n%s\n", url.c_str(), code, body.substring(0, 1000).c_str());
  } else if (cmd == "scan") {
    WiFi.mode(WIFI_STA);
    WiFi.disconnect(false, false);   // stop any in-progress connect attempt so the scan can run
    delay(300);
    int n = WiFi.scanNetworks();
    Serial.printf("[scan] %d networks\n", n);
    for (int i = 0; i < n; i++)
      Serial.printf("  %-32s ch%-2d %4d dBm %s\n", WiFi.SSID(i).c_str(), WiFi.channel(i), WiFi.RSSI(i),
                    WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "open" : "enc");
    WiFi.scanDelete();
    if (g_ssid.length()) { g_lastReconnect = millis(); WiFi.begin(g_ssid.c_str(), g_pass.c_str()); }
  } else if (cmd == "gw") {
    // gw <ip>  : route via this gateway (e.g. the Mac running Tailscale) ; "gw off" = back to DHCP
    prefs.begin("app", false); prefs.putString("gw", rest == "off" ? "" : rest); prefs.end();
    g_gw = (rest == "off") ? "" : rest;
    Serial.printf("[ok] gateway=%s (applies after reconnect)\n", g_gw.length() ? g_gw.c_str() : "dhcp");
    if (WiFi.status() == WL_CONNECTED) applyGateway();
  } else if (cmd == "say") {
    say(rest.length() ? rest : g_greet);
  } else if (cmd == "greet") {
    prefs.begin("app", false); prefs.putString("greet", rest); prefs.end(); g_greet = rest;
    Serial.printf("[ok] greeting=%s\n", g_greet.c_str());
  } else if (cmd == "brain") {
    prefs.begin("app", false); prefs.putString("brain", rest); prefs.end(); g_commentUrl = rest;
    Serial.printf("[ok] brain=%s\n", g_commentUrl.c_str());
  } else if (cmd == "tts") {
    prefs.begin("app", false); prefs.putString("tts", rest); prefs.end(); g_tts = rest;
    Serial.printf("[ok] tts=%s\n", g_tts.c_str());
  } else if (cmd == "head") {    // head nod | head shake | head center | head <pan> <tilt>
    if (!head.isAttached()) head.begin();
    if (rest == "nod") head.nod(); else if (rest == "shake") head.shake(); else if (rest == "center") head.center();
    else { int sp2 = rest.indexOf(' '); if (sp2 > 0) head.moveTo(rest.substring(0, sp2).toFloat(), rest.substring(sp2 + 1).toFloat()); }
  } else if (cmd == "track") {   // track on|off|mirror
    if (rest == "on") g_track = true; else if (rest == "off") g_track = false;
    else if (rest == "mirror") { g_mirror = !g_mirror; prefs.begin("app", false); prefs.putInt("mirror", g_mirror); prefs.end(); }
    Serial.printf("[track] on=%d mirror=%d face=%d nx=%.2f ny=%.2f w=%d\n", g_track, g_mirror, millis() - g_faceSeen < 700, g_faceNx, g_faceNy, g_faceW);
  } else if (cmd == "follow") {  // follow on|off|flippan|fliptilt
    if (rest == "on") g_headFollow = true; else if (rest == "off") g_headFollow = false;
    else if (rest == "flippan") g_panSign = -g_panSign; else if (rest == "fliptilt") g_tiltSign = -g_tiltSign;
    prefs.begin("app", false); prefs.putInt("psign", g_panSign); prefs.putInt("tsign", g_tiltSign); prefs.end();
    Serial.printf("[follow] on=%d pansign=%d tiltsign=%d pan=%.0f tilt=%.0f\n", g_headFollow, g_panSign, g_tiltSign, head.pan(), head.tilt());
  } else if (cmd == "i2cscan") {   // scan Port B (G9/G8) and Port C (G18/G17) as I2C. Port A (G2/G1) is unusable while the camera runs (G2 = XCLK).
    struct { const char* name; int sda, scl; } ports[] = {{"PortB", 9, 8}, {"PortC", 18, 17}};
    head.end();
    for (auto& pt : ports) {
      Wire.end(); Wire.begin(pt.sda, pt.scl, 100000);
      Serial.printf("[i2c] %s sda=%d scl=%d:", pt.name, pt.sda, pt.scl);
      int n = 0;
      for (int a = 0x08; a < 0x78; a++) { Wire.beginTransmission(a); if (Wire.endTransmission() == 0) { Serial.printf(" 0x%02X", a); n++; } }
      Serial.println(n ? "" : " (none)");
      Wire.end();
    }
    head.begin();
  } else if (cmd == "i2cint") {  // scan the internal I2C bus (G12/G11) via M5Unified
    bool found[120]; M5.In_I2C.scanID(found);
    Serial.print("[i2cint]"); int n = 0;
    for (int a = 8; a < 120; a++) if (found[a]) { Serial.printf(" 0x%02X", a); n++; }
    Serial.println(n ? "" : " (none)");
  } else if (cmd == "i2crecover") {   // release the driver, clock SCL to free a stuck slave, re-init
    M5.In_I2C.release();
    pinMode(11, OUTPUT_OPEN_DRAIN); pinMode(12, INPUT_PULLUP);
    for (int i = 0; i < 16; i++) { digitalWrite(11, LOW); delayMicroseconds(5); digitalWrite(11, HIGH); delayMicroseconds(5); }
    Serial.printf("[i2crecover] sda=%d after clocking\n", digitalRead(12));
    M5.In_I2C.begin();
  } else if (cmd == "camtest") {
    g_track = false; delay(100);
    if (g_camOk) { esp_camera_deinit(); g_camOk = false; }
    g_camOk = cam.begin();
    if (g_camOk) cam.sensor->set_framesize(cam.sensor, FRAMESIZE_QVGA);
    Serial.printf("[camtest] camera=%s\n", g_camOk ? "ok" : "FAILED");
    g_track = true;
  } else if (cmd == "vmen") {    // vmen on|off : servo power via the base IO expander
    baseIO.setServoPower(rest != "off");
    Serial.printf("[base] servo power=%d\n", baseIO.servoPower());
  } else if (cmd == "ping") {
    head.begin();
    Serial.printf("[head] raw pos yaw=%d pitch=%d\n", head.readRawPan(), head.readRawTilt());
  } else if (cmd == "power") {
    M5.Power.setExtOutput(true);
    Serial.printf("[power] ext5V=%d usb=%d bat=%d%% \n", M5.Power.getExtOutput(), M5.Power.isCharging(), M5.Power.getBatteryLevel());
  } else if (cmd == "learn") {    // learn <face_id> : record 4 s from the mic and send it to the brain /learn (test helper)
    static String fid; fid = rest;
    xTaskCreatePinnedToCore([](void*) {
      face.overlay("きいてるで... (4秒)", 4500, 2);
      size_t wlen = 0; uint8_t* wav = recordWav(4, &wlen);
      if (wav) {
        String learnUrl = g_commentUrl; learnUrl.replace("/visit", "/learn");
        JsonDocument d;
        int c = postJson(learnUrl + "?face_id=" + fid, "audio/wav", wav, wlen, d, 300000);
        free(wav);
        String reply = d["say"] | "", heard = d["heard"] | "", nm = d["name"] | "";
        Serial.printf("[learn] http %d heard=%s name=%s\n", c, heard.c_str(), nm.c_str());
        if (reply.length()) { face.overlay(reply, 5000, 1); say(reply); }
      } else Serial.println("[learn] record failed");
      vTaskDelete(nullptr);
    }, "learn", 16384, nullptr, 1, nullptr, 1);
  } else if (cmd == "search") {   // search | search off
    if (rest == "off") { g_search = SEARCH_NONE; head.center(); g_headCentered = true; } else startSearch();
  } else if (cmd == "comment") {
    Serial.println(startComment() ? "[comment] started" : "[comment] busy/unavailable");
  } else if (cmd == "talk") {
    face.talk(rest.length() ? rest.toInt() : 2000);
  } else if (cmd == "status") {
    printStatus();
  } else if (cmd == "clear") {
    prefs.begin("wifi", false); prefs.clear(); prefs.end();
    prefs.begin("app", false); prefs.clear(); prefs.end();
    Serial.println("[ok] cleared, rebooting"); delay(300); ESP.restart();
  } else if (cmd == "reboot") {
    ESP.restart();
  } else {
    Serial.println("[help] wifi <ssid> <pass> | scan | target <url> | gw <ip>|off | fetch [url] | say [text] | greet <text> | tts <url> | brain <url> | head nod|shake|center|<pan> <tilt> | track on|off|mirror | follow on|off|flippan|fliptilt | search [off] | comment | learn <id> | ping | vmen on|off | i2cint | camtest | status | clear | reboot");
  }
}

void pollSerial() {
  static String buf;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') { handleSerialLine(buf); buf = ""; }
    else if (buf.length() < 512) buf += c;
  }
}

// ---------- setup / loop ----------
void setup() {
  auto cfg = M5.config();
  cfg.internal_spk = true;
  cfg.internal_mic = true;
  M5.begin(cfg);
  Serial.begin(115200);
  delay(300);
  M5.Speaker.begin();
  M5.Speaker.setVolume(CFG_SPEAKER_VOLUME);
  M5.Power.setExtOutput(true);

  g_camMutex = xSemaphoreCreateMutex();
  // The first esp_camera_init sometimes fails with "i2c driver install error" (SCCB vs. the M5Unified I2C driver);
  // the failed attempt cleans the port up, so a retry succeeds.
  for (int attempt = 0; attempt < 3 && !g_camOk; attempt++) {
    if (attempt) { Serial.printf("[camera] init retry %d\n", attempt); delay(200); }
    g_camOk = cam.begin();
  }
  if (g_camOk) { cam.sensor->set_framesize(cam.sensor, FRAMESIZE_QVGA);
                 xTaskCreatePinnedToCore(faceTask, "face", 16384, nullptr, 1, nullptr, 0); }
  // StackChan base: PY32 IO expander (0x6F) pin 0 = VM_EN, the servo power switch
  if (baseIO.begin()) { baseIO.setServoPower(true); delay(200); Serial.printf("[base] io expander v0x%02X, servo power ON\n", baseIO.ver()); }
  else Serial.println("[base] io expander not found (0x6F) - servo power may be off");
  head.begin();   // SCS serial servos on UART1 (G6/G7)
  M5.Display.setRotation(1);
  face.begin();
  Serial.printf("\n[boot] Stack-chan Web Server  board=%s camera=%s\n", boardName(), g_camOk ? "ok" : "FAILED");

  prefs.begin("wifi", true); g_ssid = prefs.getString("ssid", ""); g_pass = prefs.getString("pass", ""); prefs.end();
  prefs.begin("app", true);  g_target = prefs.getString("target", CFG_FETCH_TARGET); g_gw = prefs.getString("gw", "");
  g_tts = prefs.getString("tts", CFG_TTS_URL);
  g_greet = prefs.getString("greet", CFG_GREETING);
  g_mirror = prefs.getInt("mirror", 0); g_commentUrl = prefs.getString("brain", CFG_BRAIN_URL);
  g_panSign = prefs.getInt("psign", CFG_PAN_SIGN); g_tiltSign = prefs.getInt("tsign", CFG_TILT_SIGN); prefs.end();
  head.pitchCenterDeg = CFG_PITCH_CENTER_DEG;

  server.on("/", handleRoot);
  server.on("/api/status", handleStatus);
  server.on("/api/fetch", handleFetch);
  server.on("/api/display", handleDisplay2);
  server.on("/api/head", handleHead);
  server.on("/api/track", handleTrack);
  server.on("/api/comment", handleComment);
  server.on("/api/camera.jpg", handleCamera);
  server.on("/api/talk", handleTalk);
  server.on("/api/face", handleFace);
  server.on("/api/say", handleSay);
  server.onNotFound(handleNotFound);

  if (g_ssid.length()) { if (connectWiFi()) { delay(300); say(g_greet); } }
  else { drawStatus("No Wi-Fi config.", "Send over USB serial:", "wifi <ssid> <password>"); Serial.println("[wifi] no credentials; send: wifi <ssid> <pass>"); }
  printStatus();
}

void loop() {
  M5.update();
  face.update();
  head.update();
  if (g_wav && !M5.Speaker.isPlaying() && millis() - g_wavStart > 300) { free(g_wav); g_wav = nullptr; }

  // --- pupils follow the detected face ---
  bool faceNow = g_track && (millis() - g_faceSeen < 700);
  face.setLook(g_faceNx, g_faceNy, faceNow);

  // --- head follows the face: proportional steps toward centering the face in the camera image ---
  // motion profile: quick while acquiring/searching, slow and gentle once a person is being watched
  const bool gentle = g_facePresent && (g_commentedThisVisit || !g_autoComment) && millis() - g_faceSince > 2500;
  const uint32_t stepInterval = gentle ? 350 : 150;
  const float gainPan = gentle ? 5.f : 14.f, gainTilt = gentle ? 3.5f : 9.f, servoSpeed = gentle ? 1.5f : 6.f;
  if (g_headFollow && head.isAttached() && !head.isGesturing() && millis() >= g_holdUntil && millis() - g_lastHeadStep > stepInterval) {
    g_lastHeadStep = millis();
    if (faceNow) {
      float nx = g_faceNx, ny = g_faceNy;
      // self-calibration: if a step made the face drift further from center 3 times in a row, the servo runs
      // the other way round -> flip that axis' sign (persisted)
      if (g_stepPending && g_faceSeen > g_lastHeadStep + 120) {   // judge only on a frame taken after the head moved
        bool flip = false;
        if (fabsf(g_prevNx) > 0.10f) { if (fabsf(nx) > fabsf(g_prevNx) + 0.04f) g_panWorse++; else g_panWorse = 0; }
        if (fabsf(g_prevNy) > 0.12f) { if (fabsf(ny) > fabsf(g_prevNy) + 0.04f) g_tiltWorse++; else g_tiltWorse = 0; }
        if (g_panWorse >= 4)  { g_panSign = -g_panSign;  g_panWorse = 0;  flip = true; }
        if (g_tiltWorse >= 4) { g_tiltSign = -g_tiltSign; g_tiltWorse = 0; flip = true; }
        if (flip) { prefs.begin("app", false); prefs.putInt("psign", g_panSign); prefs.putInt("tsign", g_tiltSign); prefs.end();
                    Serial.printf("[follow] auto-flip -> pansign=%d tiltsign=%d\n", g_panSign, g_tiltSign); }
        g_stepPending = false;
      }
      float dp = (fabsf(nx) > (gentle ? 0.15f : 0.08f)) ? g_panSign * nx * gainPan : 0;    // degrees per step
      float dt = (fabsf(ny) > (gentle ? 0.18f : 0.10f)) ? g_tiltSign * ny * gainTilt : 0;
      if (dp != 0 || dt != 0) {
        head.moveTo(head.pan() + dp, head.tilt() + dt, servoSpeed); g_prevNx = nx; g_prevNy = ny; g_stepPending = true;
        static uint32_t lastLog = 0;
        if (millis() - lastLog > 1000) { lastLog = millis(); Serial.printf("[follow] face nx=%.2f ny=%.2f -> pan %.0f tilt %.0f\n", nx, ny, head.pan() + dp, head.tilt() + dt); }
      }
      g_headCentered = false;
      if (g_search != SEARCH_NONE) { Serial.println("[search] face found"); g_search = SEARCH_NONE; }
    } else {
      uint32_t sinceFace = millis() - g_faceSeen;
      switch (g_search) {
        case SEARCH_NONE:
          if (sinceFace > SEARCH_START_AFTER_MS) startSearch();   // also right after boot when nobody is there
          break;
        case SEARCH_ACTIVE:
          if (millis() - g_searchStart > SEARCH_DURATION_MS) {
            g_search = SEARCH_SLEEP; g_searchWakeAt = millis() + SEARCH_SLEEP_MS;
            head.moveTo(90, 90, 1.5f); g_headCentered = true;
            Serial.println("[search] gave up, retry in 10 min");
          } else if (head.atTarget()) {
            // sweep legs: pan alternates 30 <-> 150, tilt cycles 90 / 105 / 75
            g_searchLeg++;
            static const float tilts[] = {90, 105, 75};
            head.moveTo((g_searchLeg & 1) ? 150 : 30, tilts[(g_searchLeg / 2) % 3], 2.0f);
          }
          break;
        case SEARCH_SLEEP:
          if ((int32_t)(millis() - g_searchWakeAt) >= 0) startSearch();
          break;
      }
    }
  }

  // --- auto clothing comment: once per visit, after the face has been present 1.5 s, 30 s cooldown ---
  uint32_t now = millis();
  if (faceNow) {
    if (!g_facePresent) {
      g_facePresent = true; g_faceSince = now; g_commentedThisVisit = false;
      g_holdUntil = now + 2500;                       // found someone: stop the head for the photo
      head.moveTo(head.pan(), head.tilt(), 6.f);      // cancel any pending motion
      Serial.println("[visit] face acquired, holding still for a photo");
    }
  } else if (g_facePresent && now - g_faceSeen > 4000) { g_facePresent = false; g_faceGone = now; }
  // while someone keeps sitting there, check on them every 15 min (the brain decides whether to say anything)
  if (g_autoComment && g_facePresent && g_commentedThisVisit && !g_commentBusy && now - g_lastComment > CFG_REVISIT_INTERVAL) {
    g_holdUntil = now + 1500; startComment();
  }
  if (g_autoComment && g_facePresent && !g_commentedThisVisit && !g_commentBusy &&
      now - g_faceSince > 600 && (g_lastComment == 0 || now - g_lastComment > 20000)) {
    g_commentedThisVisit = true;
    g_holdUntil = now + 1500;                          // keep still while the picture is taken
    startComment();
  }
  pollSerial();
  server.handleClient();
  if (g_ssid.length() && WiFi.status() != WL_CONNECTED && millis() - g_lastReconnect > 15000) {
    g_lastReconnect = millis();
    Serial.println("[wifi] reconnecting...");
    WiFi.reconnect();
  }
  if (M5.BtnA.wasPressed() && WiFi.status() == WL_CONNECTED) showConnected();
  delay(1);
}
