#!/usr/bin/env python3
"""Stack-chan brain: face recognition + name learning + VLM greeting.
Needs: insightface, onnxruntime, opencv-python, numpy, mlx-whisper (Apple Silicon). VLM = any Ollama vision model.
  POST /visit  (image/jpeg)            -> {"person","known","name","face_id","say","ask"}
  POST /learn?face_id=..  (audio/wav)  -> {"name","say"}     (whisper -> name extraction -> remember the face)
  GET  /people                         -> known people (owner flag)
  GET  /forget?name=..  /owner?name=.. -> delete a person / make someone the owner ("ご主人")
  GET  /                               -> dashboard: photos and what was said, newest first
  GET  /events.json  /photos/<file>    -> raw event log / saved photos (in ~/Library/Application Support/stackchan)
Text to speak is returned; the board speaks it through the TTS proxy (/say). VLM = Ollama via the ssh tunnel."""
import os, sys, json, time, uuid, re, base64, io, hashlib, urllib.request, urllib.parse, threading
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
OWNER_PHOTO_INTERVAL_S = int(os.environ.get("STACKCHAN_OWNER_PHOTO_INTERVAL", 24 * 3600))  # daily owner portrait
REPORT_COOLDOWN_S = 3600      # report the same non-owner at most once per hour
NAG_INTERVAL_S = 1800         # "go to bed" reminder for a dozing owner at most every 30 min
asked = []                    # [(emb, t)] unknown faces we already asked
PHOTO_DIR = os.path.join(DB_DIR, "photos"); os.makedirs(PHOTO_DIR, exist_ok=True)
EVENTS_PATH = os.path.join(DB_DIR, "events.jsonl")
reported = {}                 # name/"unknown" -> last report time (spam guard)
recent_sightings = []         # [(emb, t, name)] for stranger cooldown
lock = threading.Lock()

# ---------- face DB ----------
def load_db():
    try: return json.load(open(DB_PATH))
    except Exception: return []
def save_db(db): json.dump(db, open(DB_PATH, "w"), ensure_ascii=False, indent=1)
people = load_db()
pending = {}    # face_id -> {"emb": [...], "t": time, "jpg": path}

def log_event(kind, **kw):
    ev = {"ts": time.time(), "time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, **kw}
    with lock:
        with open(EVENTS_PATH, "a") as f: f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev

def load_events(limit=500):
    try: lines = open(EVENTS_PATH).read().splitlines()
    except Exception: return []
    out = []
    for ln in lines[-limit:]:
        try: out.append(json.loads(ln))
        except Exception: pass
    return out

def save_photo(jpeg, label):
    # ASCII-only file names (the board fetches them by URL); the person's name lives in the event log
    tag = re.sub(r"[^A-Za-z0-9]", "", label or "") or ("p" + hashlib.sha1((label or "unknown").encode()).hexdigest()[:6])
    name = time.strftime("%Y%m%d_%H%M%S") + "_" + tag + ".jpg"
    open(os.path.join(PHOTO_DIR, name), "wb").write(jpeg); return name

def owner():
    return next((p for p in people if p.get("owner")), None)

def notify_mac(title, text):
    try: import subprocess; subprocess.Popen(["osascript", "-e", f'display notification "{text}" with title "{title}"'])
    except Exception: pass

def pending_reports():
    """non-owner sightings not yet reported to the owner"""
    return [e for e in load_events(300) if e.get("kind") == "sighting" and not e.get("reported")]

def mark_reported():
    evs = load_events(100000)
    for e in evs:
        if e.get("kind") == "sighting": e["reported"] = True
    with lock:
        with open(EVENTS_PATH, "w") as f:
            for e in evs: f.write(json.dumps(e, ensure_ascii=False) + "\n")

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
              '{"person": true, ' + want + '}')
    return parse_json(ask_vlm(prompt, jpeg))

def vlm_sleeping(jpeg, name):
    prompt = (f"この写真は小さなロボットが目の前の人（{name}さん）を撮ったものです。この人は目を閉じて眠っている、"
              "またはうたた寝している状態ですか。次のJSONだけを返してください。\n"
              '{"sleeping": 寝ている・うたた寝ならtrue、起きていればfalse}')
    return bool(parse_json(ask_vlm(prompt, jpeg)).get("sleeping"))

def vlm_unknown(jpeg):
    prompt = ("この写真は小さなロボットが目の前の人を撮ったものです。次のJSONだけを返してください。\n"
              '{"person": 人物が写っていればtrue}')
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
            self._json(200, [{"name": p["name"], "owner": bool(p.get("owner")), "samples": len(p["embs"]), "seen": p.get("seen", 0), "last": p.get("last", "")} for p in people])
        elif u.path == "/forget" and q.get("name"):
            with lock:
                n0 = len(people); people[:] = [p for p in people if p["name"] != q["name"][0]]; save_db(people)
            self._json(200, {"removed": n0 - len(people)})
        elif u.path == "/owner" and q.get("name"):
            with lock:
                for p in people: p["owner"] = (p["name"] == q["name"][0])
                save_db(people)
            self._json(200, {"owner": q["name"][0]})
        elif u.path == "/events.json":
            self._json(200, list(reversed(load_events(int(q.get("limit", ["300"])[0])))))
        elif u.path.startswith("/photos/"):
            fn = os.path.basename(urllib.parse.unquote(u.path)); fp = os.path.join(PHOTO_DIR, fn)
            if not os.path.exists(fp): return self._json(404, {"error": "no such photo"})
            data = open(fp, "rb").read()
            self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        elif u.path == "/":
            data = DASHBOARD.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
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
                if not j.get("person"): return self._json(200, {"person": False, "known": False, "say": "", "ask": False})
                return self._json(200, {"person": True, "known": False, "say": "顔がよう見えへんわ。もうちょい近う寄ってや", "ask": False})
            person, sim = match(emb)
            if person:
                now = time.time(); today = time.strftime("%Y-%m-%d")
                is_owner = bool(person.get("owner"))
                mode = "greet" if person.get("greet_date") != today else \
                       "checkin" if now - person.get("checkin_ts", 0) >= CHECKIN_INTERVAL_S else "quiet"
                with lock:
                    person["seen"] = person.get("seen", 0) + 1; person["last"] = time.strftime("%Y-%m-%d %H:%M")
                    if sim < 0.7 and len(person["embs"]) < 8: person["embs"].append(emb.tolist())   # keep learning this face
                    save_db(people)
                photo = None
                if is_owner and now - person.get("photo_ts", 0) >= OWNER_PHOTO_INTERVAL_S:
                    photo = save_photo(body, person["name"]); person["photo_ts"] = now; save_db(people)
                    log_event("owner_photo", name=person["name"], photo=photo)
                if not is_owner and now - reported.get(person["name"], 0) >= REPORT_COOLDOWN_S:
                    photo = photo or save_photo(body, person["name"]); reported[person["name"]] = now
                    log_event("sighting", name=person["name"], known=True, photo=photo, reported=False)
                    notify_mac("Stack-chan", f"{person['name']}さんが来ています")
                if is_owner and now - person.get("nag_ts", 0) >= NAG_INTERVAL_S and vlm_sleeping(body, person["name"]):
                    say = "ご主人、そんなとこで寝たら風邪ひくで。ベッド行きな。"
                    with lock: person["nag_ts"] = now; person["checkin_ts"] = now; save_db(people)
                    log_event("visit", name=person["name"], known=True, owner=True, mode="sleeping", say=say, photo=photo or save_photo(body, person["name"]))
                    print(f"visit: owner {person['name']} sleeping -> nag ({time.time()-t0:.1f}s)", flush=True)
                    return self._json(200, {"person": True, "known": True, "owner": True, "name": person["name"], "sim": round(sim, 2), "say": say, "ask": False, "mode": "sleeping"})
                if mode == "quiet":
                    print(f"visit: known {person['name']} sim={sim:.2f} quiet ({time.time()-t0:.1f}s)", flush=True)
                    return self._json(200, {"person": True, "known": True, "owner": is_owner, "name": person["name"], "sim": round(sim, 2), "say": "", "ask": False, "mode": mode})
                j = vlm_known(body, person["name"], mode)
                say = str(j.get("say", "")).strip() or (f"{person['name']}さん{time_greeting()}ー" if mode == "greet" else "")
                show = None
                if is_owner:
                    reps = pending_reports()
                    if reps:
                        names = sorted({r.get("name") or "知らん人" for r in reps})
                        who = "、".join(n + ("さん" if n != "知らん人" else "") for n in names)
                        say = (say + " " if say else "") + f"あと、さっき{who}が来てたで。写真見せるわ。"
                        last = [r for r in reps if r.get("photo")]
                        if last: show = "/photos/" + last[-1]["photo"]
                        mark_reported()
                with lock:
                    if mode == "greet": person["greet_date"] = today
                    person["checkin_ts"] = now; save_db(people)
                log_event("visit", name=person["name"], known=True, owner=is_owner, mode=mode, say=say, photo=photo)
                print(f"visit: known {person['name']} sim={sim:.2f} {mode} say={say} ({time.time()-t0:.1f}s)", flush=True)
                return self._json(200, {"person": True, "known": True, "owner": is_owner, "name": person["name"], "sim": round(sim, 2), "say": say, "ask": False, "mode": mode, "show": show})
            # unknown: do not pester the same stranger repeatedly
            now = time.time()
            asked[:] = [(e, t) for e, t in asked if now - t < ASK_AGAIN_S]
            if any(float(np.dot(emb, e)) >= SIM_THRESHOLD for e, t in asked):
                print(f"visit: unknown (asked recently) sim={sim:.2f} ({time.time()-t0:.1f}s)", flush=True)
                return self._json(200, {"person": True, "known": False, "say": "", "ask": False})
            fid = uuid.uuid4().hex[:8]
            with lock: pending[fid] = {"emb": emb.tolist(), "t": time.time()}; asked.append((emb, time.time()))
            photo = save_photo(body, "unknown")
            log_event("sighting", name=None, known=False, photo=photo, face_id=fid, reported=False)
            notify_mac("Stack-chan", "知らない人が来ています")
            print(f"visit: unknown sim={sim:.2f} face_id={fid} ({time.time()-t0:.1f}s)", flush=True)
            return self._json(200, {"person": True, "known": False, "face_id": fid, "say": "あんた、だれ？", "ask": True})
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
            evs = load_events(100000)
            for e in evs:
                if e.get("face_id") == fid: e["name"] = name
            with lock:
                with open(EVENTS_PATH, "w") as f:
                    for e in evs: f.write(json.dumps(e, ensure_ascii=False) + "\n")
            log_event("learn", name=name, heard=text)
            return self._json(200, {"name": name, "heard": text, "say": f"{name}さんやな。覚えたで！"})
        self._json(404, {"error": "not found"})

DASHBOARD = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Stack-chan log</title>
<style>
body{font-family:-apple-system,"Hiragino Sans",sans-serif;margin:0;background:#111;color:#eee}
header{padding:14px 20px;background:#000;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
h1{font-size:18px;margin:0}select,button{background:#222;color:#eee;border:1px solid #444;padding:4px 8px;border-radius:6px}
.people{padding:10px 20px;display:flex;gap:10px;flex-wrap:wrap}.person{background:#1c1c1c;border-radius:10px;padding:8px 12px}
.person.owner{outline:2px solid #f5c542}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;padding:12px 20px}
.card{background:#1c1c1c;border-radius:12px;overflow:hidden}.card img{width:100%;aspect-ratio:4/3;object-fit:cover;display:block;background:#000}
.card .b{padding:8px 10px;font-size:13px}.t{color:#999;font-size:12px}.k{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;margin-right:6px}
.k.sighting{background:#7a2b2b}.k.visit{background:#2b4a7a}.k.owner_photo{background:#7a6a2b}.k.learn{background:#2b7a4a}
</style>
<header><h1>Stack-chan ログ</h1><label>種類 <select id=kind><option value="">すべて</option><option value=visit>訪問</option><option value=sighting>ご主人以外</option><option value=owner_photo>ご主人の写真</option><option value=learn>名前学習</option></select></label>
<label>人 <select id=who><option value="">すべて</option></select></label><button onclick="load()">更新</button></header>
<div class=people id=people></div><div class=grid id=grid></div>
<script>
async function load(){
  const [ev, ppl] = await Promise.all([fetch('/events.json?limit=1000').then(r=>r.json()), fetch('/people').then(r=>r.json())]);
  const who=document.getElementById('who'); const cur=who.value; const names=[...new Set(ev.map(e=>e.name||'unknown'))];
  who.innerHTML='<option value="">すべて</option>'+names.map(n=>`<option ${n===cur?'selected':''}>${n}</option>`).join('');
  document.getElementById('people').innerHTML = ppl.map(p=>`<div class="person ${p.owner?'owner':''}">${p.owner?'👑 ':''}${p.name} <span class=t>${p.seen}回 / 最終 ${p.last||''}</span>
     <button onclick="fetch('/owner?name='+encodeURIComponent('${p.name}')).then(load)">ご主人にする</button> <button onclick="if(confirm('${p.name} を忘れる？'))fetch('/forget?name='+encodeURIComponent('${p.name}')).then(load)">忘れる</button></div>`).join('');
  const k=document.getElementById('kind').value, w=who.value;
  document.getElementById('grid').innerHTML = ev.filter(e=>(!k||e.kind===k)&&(!w||(e.name||'unknown')===w)).map(e=>`<div class=card>${e.photo?`<a href="/photos/${e.photo}" target=_blank><img loading=lazy src="/photos/${e.photo}"></a>`:''}
     <div class=b><span class="k ${e.kind}">${{visit:'訪問',sighting:'ご主人以外',owner_photo:'ご主人の写真',learn:'名前学習'}[e.kind]||e.kind}</span><b>${e.name||'知らない人'}</b> <span class=t>${e.time}${e.mode?' · '+e.mode:''}</span>
     ${e.say?`<div>「${e.say}」</div>`:''}${e.heard?`<div class=t>聞き取り: ${e.heard}</div>`:''}</div></div>`).join('');
}
document.getElementById('kind').onchange=load; document.getElementById('who').onchange=load; load(); setInterval(load, 30000);
</script>"""

print(f"brain on :{PORT}  people={len(people)}  vlm={VLM_MODEL}", flush=True)
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
