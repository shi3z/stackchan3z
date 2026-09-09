#!/usr/bin/env python3
"""TTS proxy for the Stack-chan: GET /say?text=...&emotion=happy -> audio/wav (16 kHz, 16-bit mono, clean header).
Backends (env STACKCHAN_TTS_BACKEND):
  tsukasa : POST {text, emotion} to STACKCHAN_TTS_API (a Tsukasa-Speech / StyleTTS2 server returning base64 WAV JSON)
  say     : macOS built-in `say` (voice STACKCHAN_SAY_VOICE, default Kyoko) - works out of the box, no GPU needed
Results are cached on disk, so repeated phrases (e.g. the boot greeting) are served instantly.
STACKCHAN_TTS_PITCH (semitones) or &pitch= shifts the voice with ffmpeg rubberband - run a second instance for a second unit.
usage: tts_proxy.py [port]"""
import sys, os, json, base64, wave, io, hashlib, subprocess, tempfile, urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
PORT    = int(sys.argv[1]) if len(sys.argv) > 1 else 9001
API     = os.environ.get("STACKCHAN_TTS_API", "")                      # e.g. http://100.x.y.z:8000/synthesize
BACKEND = os.environ.get("STACKCHAN_TTS_BACKEND", "tsukasa" if API else "say")
VOICE   = os.environ.get("STACKCHAN_SAY_VOICE", "Kyoko")
CACHE   = os.path.expanduser("~/Library/Caches/stackchan-tts"); os.makedirs(CACHE, exist_ok=True)
RATE    = 16000   # the ESP32 plays 16 kHz mono 16-bit
PITCH   = float(os.environ.get("STACKCHAN_TTS_PITCH", "0"))   # semitones; gives a second unit a different voice (ffmpeg rubberband)

def to_wav16k(wav_bytes):
    """Any PCM WAV -> 16 kHz mono 16-bit WAV bytes."""
    import audioop
    src = wave.open(io.BytesIO(wav_bytes)); frames = src.readframes(src.getnframes())
    sr, ch, sw = src.getframerate(), src.getnchannels(), src.getsampwidth()
    if ch != 1: frames = audioop.tomono(frames, sw, 0.5, 0.5)
    if sw != 2: frames = audioop.lin2lin(frames, sw, 2)
    if sr != RATE: frames, _ = audioop.ratecv(frames, 2, 1, sr, RATE, None)
    out = io.BytesIO(); w = wave.open(out, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
    w.writeframes(frames); w.close(); return out.getvalue()

def synth_tsukasa(text, emotion):
    req = urllib.request.Request(API, data=json.dumps({"text": text, "emotion": emotion}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r: d = json.load(r)
    return to_wav16k(base64.b64decode(d["audio_data"]))

def synth_say(text, emotion):
    with tempfile.TemporaryDirectory() as td:
        aiff = os.path.join(td, "s.aiff"); wav = os.path.join(td, "s.wav")
        subprocess.run(["say", "-v", VOICE, "-o", aiff, text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, wav], check=True)
        return open(wav, "rb").read()

def normalize(data, peak=0.9):
    """Scale the WAV so its peak hits `peak` of full scale (the little speakers are quiet)."""
    import audioop
    w = wave.open(io.BytesIO(data)); frames = w.readframes(w.getnframes()); params = w.getparams()
    mx = audioop.max(frames, 2)
    if mx <= 0: return data
    frames = audioop.mul(frames, 2, min(4.0, peak * 32767 / mx))
    out = io.BytesIO(); o = wave.open(out, "wb"); o.setparams(params); o.writeframes(frames); o.close(); return out.getvalue()

def shift_pitch(data, semitones):
    """Pitch-shift a WAV with ffmpeg's rubberband (tempo preserved)."""
    if not semitones: return data
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in.wav"); dst = os.path.join(td, "out.wav"); open(src, "wb").write(data)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", src, "-af", f"rubberband=pitch={2 ** (semitones / 12):.4f}",
                        "-ar", str(RATE), "-ac", "1", "-sample_fmt", "s16", dst], check=True)
        return open(dst, "rb").read()

def synth(text, emotion, pitch=None):
    pitch = PITCH if pitch is None else pitch
    key = hashlib.sha1(f"{BACKEND}|{VOICE}|{RATE}|{emotion}|{pitch}|norm|{text}".encode()).hexdigest()
    path = os.path.join(CACHE, key + ".wav")
    if os.path.exists(path): return open(path, "rb").read()
    data = synth_tsukasa(text, emotion) if BACKEND == "tsukasa" else synth_say(text, emotion)
    data = normalize(shift_pitch(data, pitch))
    open(path, "wb").write(data); return data

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if u.path == "/health":
            b = json.dumps({"ok": True, "backend": BACKEND, "pitch": PITCH}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b); return
        if u.path != "/say" or not q.get("text"):
            self.send_response(400); self.end_headers(); self.wfile.write(b"usage: /say?text=...&emotion=happy"); return
        try: data = synth(q["text"][0], q.get("emotion", ["neutral"])[0], float(q["pitch"][0]) if q.get("pitch") else None)
        except Exception as e:
            print("tts error:", e, flush=True); self.send_response(502); self.end_headers(); self.wfile.write(str(e).encode()); return
        self.send_response(200); self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, f, *a): print(self.address_string(), f % a, flush=True)

print(f"tts proxy on :{PORT} backend={BACKEND}" + (f" api={API}" if API else f" voice={VOICE}"), flush=True)
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
