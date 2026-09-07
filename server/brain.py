#!/usr/bin/env python3
"""Stack-chan brain: face recognition + name learning + VLM greeting.
Needs: insightface, onnxruntime, opencv-python, numpy, mlx-whisper (Apple Silicon). VLM = any Ollama vision model.
  POST /visit  (image/jpeg)            -> {"person","vr","known","name","face_id","say","ask"}
  POST /learn?face_id=..  (audio/wav)  -> {"name","say"}     (whisper -> name extraction -> remember the face)
  GET  /people                         -> known people
  GET  /forget?name=..                 -> delete a person
Text to speak is returned; the board speaks it through the TTS proxy (/say). VLM = Ollama via the ssh tunnel."""
import os, sys, json, time, uuid, re, base64, io, urllib.request, urllib.parse, threading
os.environ.setdefault("HF_HUB_OFFLINE", "1")   # the HF online check stalls for minutes here; models are cached
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import numpy as np, cv2

PORT      = int(sys.argv[1]) if len(sys.argv) > 1 else 9002
VLM_URL   = os.environ.get("STACKCHAN_VLM_URL", "http://127.0.0.1:11434/api/chat")   # Ollama /api/chat
VLM_MODEL = os.environ.get("STACKCHAN_VLM_MODEL", "qwen2.5vl:7b")                        # any Ollama vision model
WHISPER   = os.environ.get("STACKCHAN_WHISPER", "mlx-community/whisper-large-v3-turbo")
DB_DIR    = os.path.expanduser("~/Library/Application Support/stackchan"); os.makedirs(DB_DIR, exist_ok=True)
DB_PATH   = os.path.join(DB_DIR, "faces.json")
SIM_THRESHOLD = 0.45          # cosine similarity for "same person" (buffalo_l)
CHECKIN_INTERVAL_S = 3600     # how often to comment on a known person's condition
ASK_AGAIN_S = 600             # do not ask an unknown person their name again within this window
asked = []                    # [(emb, t)] unknown faces we already asked
lock = threading.Lock()

# ---------- face DB ----------
def load_db():
    try: return json.load(open(DB_PATH))
    except Exception: return []
def save_db(db): json.dump(db, open(DB_PATH, "w"), ensure_ascii=False, indent=1)
people = load_db()
pending = {}    # face_id -> {"emb": [...], "t": time, "jpg": path}

print("loading insightface...", flush=True)
from insightface.app import FaceAnalysis
fa = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"]); fa.prepare(ctx_id=0, det_size=(320, 320))
print("insightface ready", flush=True)

def embed(jpeg):
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None: return None
    faces = fa.get(img)
    if not faces: return None
    f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    return f.normed_embedding.astype(float)

def match(emb):
    best, bestSim = None, 0.0
    for p in people:
        for e in p["embs"]:
            s = float(np.dot(emb, np.array(e)))
            if s > bestSim: best, bestSim = p, s
    return (best, bestSim) if bestSim >= SIM_THRESHOLD else (None, bestSim)

# ---------- VLM ----------
def ask_vlm(prompt, jpeg=None, timeout=120):
    msg = {"role": "user", "content": prompt}
    if jpeg: msg["images"] = [base64.b64encode(jpeg).decode()]
    body = {"model": VLM_MODEL, "stream": False, "think": False, "messages": [msg]}
    req = urllib.request.Request(VLM_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.load(r)["message"]["content"].strip()

def parse_json(txt):
    m = re.search(r"\{.*\}", txt, re.S)
    try: return json.loads(m.group(0)) if m else {}
    except Exception: return {}

HEADWEAR = ('"headwear": "頭部に装着しているもの。なし / 帽子 / 眼鏡 / VRヘッドセット のいずれか。'
            '目や顔の上半分を覆う箱型・ゴーグル型の機器（Meta Quest、Apple Vision Proなど）はすべて VRヘッドセット と答える", '
            '"vr": headwearがVRヘッドセットならtrue')

def time_greeting():
    h = time.localtime().tm_hour
    return "おはよう" if 5 <= h < 11 else "こんにちは" if h < 18 else "こんばんは"

def vlm_known(jpeg, name, mode):
    """mode: 'greet' (first time today) or 'checkin' (hourly condition check)"""
    if mode == "greet":
        g = time_greeting()
        want = (f'"say": "{name}さんへの関西弁の挨拶（{g}）＋顔の様子から読み取った一言（合計35文字以内）。'
                f'例: 「{name}さん{g}ー。今日はご機嫌よさそうやな」「{name}さん{g}。今日は顔つかれてない？」 実際の表情に合わせる"')
    else:
        want = (f'"say": "{name}さんの今の様子を気づかう関西弁の一言（35文字以内、挨拶は不要）。'
                f'例: 「{name}さん、疲れてない？少し休んだら？」「{name}さん、ええ顔してるやん。調子よさそうやな」 実際の表情・顔色・目の様子に合わせる"')
    prompt = (f"この写真は小さなロボットが目の前の人を撮ったものです。この人は「{name}」さんです。"
              "顔色・表情・目の様子から今日の調子や機嫌を読み取り、次のJSONだけを返してください。\n"
              '{"person": true, ' + HEADWEAR + ', ' + want + '}')
    return parse_json(ask_vlm(prompt, jpeg))

def vlm_unknown(jpeg):
    prompt = ("この写真は小さなロボットが目の前の人を撮ったものです。次のJSONだけを返してください。\n"
              '{"person": 人物が写っていればtrue, ' + HEADWEAR + '}')
    return parse_json(ask_vlm(prompt, jpeg))

def extract_name(text):
    if not text.strip(): return ""
    prompt = ("次の発話は「あんた誰？」と聞かれた人の返事を音声認識したものです（誤変換を含むことがあります。例:「清水屋で」は「清水やで」）。"
              "この人の名前（呼び名）だけを抜き出して、名前そのものだけを返してください。"
              "敬称（さん・くん）や語尾（です・やで）は付けず、名前が含まれていなければ「不明」とだけ返してください。\n発話: " + text)
    name = ask_vlm(prompt, None, 60).strip().strip("「」\"'。 ")
    name = re.sub(r"(です|だよ|やで|やねん|です。)$", "", name)
    if not name or "不明" in name or len(name) > 12: return ""
    return name

def transcribe(wav_bytes):
    import mlx_whisper, tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f: f.write(wav_bytes); path = f.name
    kw = dict(path_or_hf_repo=WHISPER, language="ja",
              initial_prompt="「あんた誰？」と聞かれて名前を名乗る返事。例: 私は田中です。山田やで。鈴木といいます。")
    try:
        try: r = mlx_whisper.transcribe(path, **kw)
        except Exception as e:                       # model not cached yet -> allow one online download
            print("whisper offline failed, retrying online:", e, flush=True)
            os.environ["HF_HUB_OFFLINE"] = "0"; r = mlx_whisper.transcribe(path, **kw); os.environ["HF_HUB_OFFLINE"] = "1"
        return r["text"].strip()
    finally: os.unlink(path)

def is_vr(j): return bool(j.get("vr")) or any(k in str(j.get("headwear", "")) for k in ("VR", "ヘッドセット", "ゴーグル"))

# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, f, *a): print(self.address_string(), f % a, flush=True)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if u.path == "/people":
            self._json(200, [{"name": p["name"], "samples": len(p["embs"]), "seen": p.get("seen", 0), "last": p.get("last", "")} for p in people])
        elif u.path == "/forget" and q.get("name"):
            with lock:
                n0 = len(people); people[:] = [p for p in people if p["name"] != q["name"][0]]; save_db(people)
            self._json(200, {"removed": n0 - len(people)})
        elif u.path == "/health": self._json(200, {"ok": True, "people": len(people)})
        else: self._json(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        n = int(self.headers.get("Content-Length", "0")); body = self.rfile.read(n)
        t0 = time.time()
        if u.path == "/visit":
            try: open(os.path.join(DB_DIR, "last_visit.jpg"), "wb").write(body)
            except Exception: pass
            emb = embed(body)
            if emb is None:
                j = vlm_unknown(body)
                if not j.get("person") or is_vr(j): return self._json(200, {"person": bool(j.get("person")), "vr": is_vr(j), "known": False, "say": "", "ask": False})
                return self._json(200, {"person": True, "vr": False, "known": False, "say": "顔がよう見えへんわ。もうちょい近う寄ってや", "ask": False})
            person, sim = match(emb)
            if person:
                now = time.time(); today = time.strftime("%Y-%m-%d")
                mode = "greet" if person.get("greet_date") != today else \
                       "checkin" if now - person.get("checkin_ts", 0) >= CHECKIN_INTERVAL_S else "quiet"
                with lock:
                    person["seen"] = person.get("seen", 0) + 1; person["last"] = time.strftime("%Y-%m-%d %H:%M")
                    if sim < 0.7 and len(person["embs"]) < 8: person["embs"].append(emb.tolist())   # keep learning this face
                    save_db(people)
                if mode == "quiet":
                    print(f"visit: known {person['name']} sim={sim:.2f} quiet ({time.time()-t0:.1f}s)", flush=True)
                    return self._json(200, {"person": True, "vr": False, "known": True, "name": person["name"], "sim": round(sim, 2), "say": "", "ask": False, "mode": mode})
                j = vlm_known(body, person["name"], mode)
                if is_vr(j): return self._json(200, {"person": True, "vr": True, "known": True, "name": person["name"], "say": "", "ask": False})
                say = str(j.get("say", "")).strip() or (f"{person['name']}さん{time_greeting()}ー" if mode == "greet" else "")
                with lock:
                    if mode == "greet": person["greet_date"] = today
                    person["checkin_ts"] = now; save_db(people)
                print(f"visit: known {person['name']} sim={sim:.2f} {mode} say={say} ({time.time()-t0:.1f}s)", flush=True)
                return self._json(200, {"person": True, "vr": False, "known": True, "name": person["name"], "sim": round(sim, 2), "say": say, "ask": False, "mode": mode})
            # unknown: do not pester the same stranger repeatedly
            now = time.time()
            asked[:] = [(e, t) for e, t in asked if now - t < ASK_AGAIN_S]
            if any(float(np.dot(emb, e)) >= SIM_THRESHOLD for e, t in asked):
                print(f"visit: unknown (asked recently) sim={sim:.2f} ({time.time()-t0:.1f}s)", flush=True)
                return self._json(200, {"person": True, "vr": False, "known": False, "say": "", "ask": False})
            j = vlm_unknown(body)
            if is_vr(j): return self._json(200, {"person": True, "vr": True, "known": False, "say": "", "ask": False})
            fid = uuid.uuid4().hex[:8]
            with lock: pending[fid] = {"emb": emb.tolist(), "t": time.time()}; asked.append((emb, time.time()))
            print(f"visit: unknown sim={sim:.2f} face_id={fid} ({time.time()-t0:.1f}s)", flush=True)
            return self._json(200, {"person": True, "vr": False, "known": False, "face_id": fid, "say": "あんた、だれ？", "ask": True})
        if u.path == "/learn":
            fid = q.get("face_id", [""])[0]
            pend = pending.get(fid)
            if not pend: return self._json(404, {"error": "unknown face_id", "say": "ごめん、顔を忘れてもうた。もう一回見せてな"})
            try: open(os.path.join(DB_DIR, "last_learn.wav"), "wb").write(body)
            except Exception: pass
            text = transcribe(body)
            name = extract_name(text)
            print(f"learn: heard={text!r} name={name!r} ({time.time()-t0:.1f}s)", flush=True)
            if not name: return self._json(200, {"name": "", "heard": text, "say": "ごめん、聞き取れへんかったわ。また教えてな"})
            with lock:
                existing = next((p for p in people if p["name"] == name), None)
                if existing: existing["embs"].append(pend["emb"])
                else: people.append({"name": name, "embs": [pend["emb"]], "seen": 1, "last": time.strftime("%Y-%m-%d %H:%M")})
                save_db(people); pending.pop(fid, None)
            return self._json(200, {"name": name, "heard": text, "say": f"{name}さんやな。覚えたで！"})
        self._json(404, {"error": "not found"})

print(f"brain on :{PORT}  people={len(people)}  vlm={VLM_MODEL}", flush=True)
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
