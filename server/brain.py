#!/usr/bin/env python3
"""Stack-chan brain: face recognition + name learning + VLM greeting.
Needs: insightface, onnxruntime, opencv-python, numpy, mlx-whisper (Apple Silicon). VLM = any Ollama vision model.
  POST /visit  (image/jpeg)            -> {"person","known","name","face_id","say","ask"}
  POST /learn?face_id=..  (audio/wav)  -> {"name","say"}     (whisper -> name extraction -> remember the face)
  POST /clothes?name=..   (image/jpeg) -> {"say","changed"}  (head tilted down -> first outfit of the day gets a comment;
                                                              later shots are compared with the previous one and only a change is called out)
  GET  /people                         -> known people (owner flag)
  GET  /forget?name=..  /owner?name=.. -> delete a person / make someone the owner ("ご主人")
  POST /answer?key=..&q=..(audio/wav)  -> the owner's answer to a profile question -> stored in profile.json
  GET  /profile  /topics  /topics/refresh -> owner profile facts / news topics picked for the owner / search now
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
ASK_INTERVAL_S = int(os.environ.get("STACKCHAN_ASK_INTERVAL", 7200))      # profile question at most every 2 h
TOPIC_INTERVAL_S = int(os.environ.get("STACKCHAN_TOPIC_INTERVAL", 3 * 3600))  # look for news this often
BOARD_URL = os.environ.get("STACKCHAN_BOARD_URL", "")                      # e.g. http://192.168.1.50 (to push topics to the screen)
PROFILE_PATH = os.path.join(DB_DIR, "profile.json")
TOPICS_PATH = os.path.join(DB_DIR, "topics.json")
talking_until = 0.0           # while > now, the owner is in a conversation: do not push topics to the screen
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

# ---------- owner profile ----------
PROFILE_QUESTIONS = [
    ("job",      "お仕事は何してはるん？"),
    ("hobby",    "趣味はなに？"),
    ("home",     "家はどのへんに住んでるん？"),
    ("office",   "会社はどこにあるん？"),
    ("food",     "好きな食べもんは？"),
    ("interest", "最近気になってることある？"),
    ("weekend",  "休みの日は何してるん？"),
    ("team",     "応援してるチームとか推しとかある？"),
    ("hometown", "出身はどこ？"),
    ("pet",      "ペット飼ってる？"),
]
def load_profile():
    try: return json.load(open(PROFILE_PATH))
    except Exception: return {"facts": {}, "last_ask": 0, "asked": {}}
def save_profile(pr): json.dump(pr, open(PROFILE_PATH, "w"), ensure_ascii=False, indent=1)
profile = load_profile()

def profile_summary():
    return "\n".join(f"- {v['q']} -> {v['a']}" for v in profile["facts"].values()) or "（まだ何も知らない）"

def next_question():
    """(key, question) to ask next: an unfilled standard one, otherwise let the LLM invent one."""
    now = time.time()
    for key, q in PROFILE_QUESTIONS:
        if key not in profile["facts"] and now - profile["asked"].get(key, 0) > 24 * 3600: return key, q
    prompt = ("あなたは小さなロボットで、ご主人のことをもっと知りたいと思っています。これまでに分かっていること:\n" + profile_summary() +
              "\n\nまだ聞いていない、ご主人を知るのに役立つ質問を1つだけ考えて、次のJSONだけを返してください。"
              "質問は関西弁で短く、答えやすいものにしてください。keyは英小文字のスネークケース。\n"
              '{"key": "例: favorite_music", "question": "例: 好きな音楽は何？"}')
    j = parse_json(ask_vlm(prompt, None, 60))
    key = re.sub(r"[^a-z0-9_]", "", str(j.get("key", "")).lower()) or f"q{int(now)}"
    q = str(j.get("question", "")).strip()
    return (key, q) if q and key not in profile["facts"] else (None, None)

def extract_answer(question, text):
    if not text.strip(): return ""
    prompt = (f"ロボットが「{question}」と質問し、ご主人が次のように答えました（音声認識なので誤変換があるかもしれません）。\n"
              f"答え: {text}\n質問への答えの要点だけを短く（20文字以内）取り出して、次のJSONだけを返してください。答えていない・分からない場合はanswerを空にしてください。\n"
              '{"answer": "要点"}')
    return str(parse_json(ask_vlm(prompt, None, 60)).get("answer", "")).strip()

# ---------- news / topics for the owner ----------
def load_topics():
    try: return json.load(open(TOPICS_PATH))
    except Exception: return []
def save_topics(t): json.dump(t, open(TOPICS_PATH, "w"), ensure_ascii=False, indent=1)

def google_news(query, n=6):
    import xml.etree.ElementTree as ET
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + "&hl=ja&gl=JP&ceid=JP:ja"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r: xml = r.read()
    items = []
    for it in ET.fromstring(xml).iter("item"):
        items.append({"title": (it.findtext("title") or "").strip(), "url": (it.findtext("link") or "").strip(),
                      "date": (it.findtext("pubDate") or "").strip(), "source": (it.findtext("source") or "").strip()})
        if len(items) >= n: break
    return items

def refresh_topics():
    if not profile["facts"]: print("topics: no profile yet", flush=True); return []
    prompt = ("ご主人について分かっていること:\n" + profile_summary() +
              "\n\nこの人が興味を持ちそうなニュースを探すための日本語の検索語を3つ考えて、次のJSONだけを返してください。\n"
              '{"queries": ["検索語1", "検索語2", "検索語3"]}')
    queries = [str(q) for q in parse_json(ask_vlm(prompt, None, 60)).get("queries", [])][:3]
    found = []
    for q in queries:
        try: found += [dict(i, query=q) for i in google_news(q)]
        except Exception as e: print("news fetch error:", q, e, flush=True)
    if not found: return []
    listing = "\n".join(f"{i+1}. [{it['query']}] {it['title']} ({it['source']})" for i, it in enumerate(found[:18]))
    prompt = ("ご主人について分かっていること:\n" + profile_summary() + "\n\n最近のニュース見出し:\n" + listing +
              "\n\nご主人に関係が深そう・喜びそうなものを最大3件選び、それぞれ関西弁のやわらかい口調で40文字以内に要約してください。次のJSONだけを返してください。\n"
              '{"topics": [{"n": 見出しの番号, "summary": "要約", "why": "選んだ理由を一言"}]}')
    sel = parse_json(ask_vlm(prompt, None, 120)).get("topics", [])
    topics = load_topics(); seen_urls = {t["url"] for t in topics}
    new = []
    for t in sel:
        try: it = found[int(t["n"]) - 1]
        except Exception: continue
        if it["url"] in seen_urls: continue
        new.append({"ts": time.time(), "time": time.strftime("%Y-%m-%d %H:%M"), "title": it["title"], "url": it["url"],
                    "source": it["source"], "query": it["query"], "summary": str(t.get("summary", "")).strip(), "why": str(t.get("why", "")).strip(), "shown": False})
    topics = (new + topics)[:100]; save_topics(topics)
    print(f"topics: {len(new)} new from {queries}", flush=True)
    return new

def wrap_jp(text, width=20, max_lines=5):
    lines = []
    for para in text.split("\n"):
        while para:
            lines.append(para[:width]); para = para[width:]
    return "\n".join(lines[:max_lines])

def push_topic_to_board(topic):
    if not BOARD_URL: return False
    text = wrap_jp("📰 " + topic["title"] + "\n" + topic["summary"])
    try:
        urllib.request.urlopen(BOARD_URL.rstrip("/") + "/api/display?" + urllib.parse.urlencode({"text": text, "size": 1, "ms": 25000}), timeout=10)
        return True
    except Exception as e: print("board push failed:", e, flush=True); return False

def topics_loop():
    time.sleep(90)
    while True:
        try:
            new = refresh_topics()
            if new and time.time() > talking_until:
                push_topic_to_board(new[0]); new[0]["shown"] = True
                t = load_topics()
                for x in t:
                    if x["url"] == new[0]["url"]: x["shown"] = True
                save_topics(t)
        except Exception as e: print("topics loop error:", e, flush=True)
        time.sleep(TOPIC_INTERVAL_S)
threading.Thread(target=topics_loop, daemon=True).start()

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
def ask_vlm(prompt, jpeg=None, timeout=120, extra_images=()):
    msg = {"role": "user", "content": prompt}
    imgs = [j for j in list(extra_images) + ([jpeg] if jpeg else []) if j]
    if imgs: msg["images"] = [base64.b64encode(j).decode() for j in imgs]
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
    """Strict dozing check. The camera looks up from the desk, so narrowed or downcast eyes are normal."""
    prompt = (f"この写真は机の上の小さなロボットが、下から見上げる角度で{name}さんを撮ったものです。"
              "この人が「眠っている・うたた寝している」かを厳密に判定してください。\n"
              "眠っている＝両目が完全に閉じていて、かつ頭が前に垂れている・後ろにもたれている・机に伏せているなど姿勢が崩れている。\n"
              "次は眠っていない：目を細めている、下（手元や画面）を見ている、横を向いている、まばたき、考え事、片目だけ見える。\n"
              "迷ったら false。次のJSONだけを返してください。\n"
              '{"eyes_closed": 両目が完全に閉じていればtrue, "posture_slumped": 姿勢が崩れていればtrue, "sleeping": 上の2つが両方trueのときだけtrue, "confidence": 0から1の確信度}')
    j = parse_json(ask_vlm(prompt, jpeg))
    try: conf = float(j.get("confidence", 0))
    except Exception: conf = 0
    return bool(j.get("sleeping")) and bool(j.get("eyes_closed")) and bool(j.get("posture_slumped")) and conf >= 0.8

def vlm_clothes_changed(prev_jpeg, jpeg, name):
    """Compare today's previous outfit photo with the new one. Returns (changed, say)."""
    who = f"{name}さん" if name else "この人"
    prompt = ("2枚の写真は小さなロボットが首を下に向けて撮った、同じ人の服装です。1枚目が前回、2枚目が今回です。"
              f"{who}の服装（上着・シャツの色や種類、柄、小物）が前回から変わっているか判定してください。"
              "照明や角度の違い、写り方の違いは変化とみなさないでください。次のJSONだけを返してください。\n"
              '{"changed": 服装が明らかに変わっていればtrue、同じか判断できなければfalse, '
              '"say": "changedがtrueのときだけ、着替えに気づいた関西弁の一言（35文字以内。例:「あれ、着替えたん？その青いシャツもええやん」）。falseなら空文字"}')
    j = parse_json(ask_vlm(prompt, jpeg, extra_images=[prev_jpeg]))
    return bool(j.get("changed")), str(j.get("say", "")).strip()

def vlm_clothes(jpeg, name):
    who = f"{name}さん" if name else "この人"
    prompt = ("この写真は小さなロボットが首を下に向けて、目の前の人の服装を撮ったものです。"
              f"{who}の服装（色・種類・柄・小物）について関西弁で一言コメントしてください。次のJSONだけを返してください。\n"
              '{"say": "服装への一言（35文字以内、褒めるか軽いツッコミ。具体的な色や種類に触れる。顔や背景の話はしない）"}')
    return str(parse_json(ask_vlm(prompt, jpeg)).get("say", "")).strip()

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

    def _maybe_question(self, now):
        if now - profile.get("last_ask", 0) < ASK_INTERVAL_S: return None
        try: key, q = next_question()
        except Exception as e: print("question error:", e, flush=True); return None
        if not q: return None
        profile["last_ask"] = now; profile["asked"][key] = now; save_profile(profile)
        return {"url": f"/answer?key={urllib.parse.quote(key)}&q={urllib.parse.quote(q)}", "seconds": 6, "prompt": q}

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
        elif u.path == "/profile": self._json(200, profile)
        elif u.path == "/topics": self._json(200, load_topics())
        elif u.path == "/topics/refresh":
            try: self._json(200, {"new": refresh_topics()})
            except Exception as e: self._json(502, {"error": str(e)})
        elif u.path == "/forget_fact" and q.get("key"):
            profile["facts"].pop(q["key"][0], None); save_profile(profile); self._json(200, {"ok": True})
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
                    # two strikes: only nag when a second check, 5-30 min after the first suspicion, also says sleeping
                    first = person.get("sleep_suspect_ts", 0)
                    if not (300 <= now - first <= 1800):
                        person["sleep_suspect_ts"] = now; save_db(people)
                        print(f"visit: owner {person['name']} looks asleep (first strike, waiting for a second look)", flush=True)
                        return self._json(200, {"person": True, "known": True, "owner": True, "name": person["name"], "sim": round(sim, 2), "say": "", "ask": False, "mode": "quiet", "clothes": False, "recheck": 420})
                    say = "ご主人、そんなとこで寝たら風邪ひくで。ベッド行きな。"
                    with lock: person["nag_ts"] = now; person["checkin_ts"] = now; person["sleep_suspect_ts"] = 0; save_db(people)
                    log_event("visit", name=person["name"], known=True, owner=True, mode="sleeping", say=say, photo=photo or save_photo(body, person["name"]))
                    print(f"visit: owner {person['name']} sleeping -> nag ({time.time()-t0:.1f}s)", flush=True)
                    return self._json(200, {"person": True, "known": True, "owner": True, "name": person["name"], "sim": round(sim, 2), "say": say, "ask": False, "mode": "sleeping"})
                if mode == "quiet":
                    resp = {"person": True, "known": True, "owner": is_owner, "name": person["name"], "sim": round(sim, 2), "say": "", "ask": False, "mode": mode, "clothes": True}
                    if is_owner:
                        listen = self._maybe_question(now)
                        if listen: resp["say"] = f"{person['name']}さん、{listen['prompt']}"; resp["listen"] = listen
                        else:
                            t = next((x for x in load_topics() if not x.get("shown")), None)
                            if t:
                                resp["display"] = {"text": wrap_jp("📰 " + t["title"] + "\n" + t["summary"]), "size": 1, "ms": 25000}
                                tl = load_topics()
                                for x in tl:
                                    if x["url"] == t["url"]: x["shown"] = True
                                save_topics(tl)
                    print(f"visit: known {person['name']} sim={sim:.2f} quiet{' +question' if resp.get('listen') else ''}{' +topic' if resp.get('display') else ''} ({time.time()-t0:.1f}s)", flush=True)
                    return self._json(200, resp)
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
                resp = {"person": True, "known": True, "owner": is_owner, "name": person["name"], "sim": round(sim, 2), "say": say, "ask": False, "mode": mode, "show": show, "clothes": True}
                if is_owner:
                    listen = self._maybe_question(now)
                    if listen: say = (say + " ところで、" if say else "") + listen["prompt"]; resp["say"] = say; resp["listen"] = listen
                    global talking_until; talking_until = now + 120
                log_event("visit", name=person["name"], known=True, owner=is_owner, mode=mode, say=say, photo=photo)
                print(f"visit: known {person['name']} sim={sim:.2f} {mode} say={say} ({time.time()-t0:.1f}s)", flush=True)
                return self._json(200, resp)
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
            return self._json(200, {"person": True, "known": False, "face_id": fid, "say": "あんた、だれ？", "ask": True, "clothes": True,
                                    "listen": {"url": f"/learn?face_id={fid}", "seconds": 4, "prompt": "あんた、だれ？"}})
        if u.path == "/clothes":     # POST image/jpeg  ?name=..  -> {"say": ..., "changed": bool}
            name = q.get("name", [""])[0]; today = time.strftime("%Y-%m-%d")
            p = next((p for p in people if p["name"] == name), None) if name else None
            prev = None
            if p and p.get("clothes_date") == today and p.get("clothes_photo"):
                try: prev = open(os.path.join(PHOTO_DIR, p["clothes_photo"]), "rb").read()
                except Exception: prev = None
            try:
                if prev is None: changed, say = True, vlm_clothes(body, name)          # first outfit of the day
                else: changed, say = vlm_clothes_changed(prev, body, name)              # same day: only speak if it changed
            except Exception as e:
                print("clothes vlm error:", e, flush=True); return self._json(502, {"say": "", "changed": False})
            photo = save_photo(body, (name or "unknown") + "clothes")
            with lock:
                if p:
                    if prev is None or changed: p["clothes_date"] = today; p["clothes_photo"] = photo
                    save_db(people)
            log_event("clothes", name=name or None, say=say, photo=photo, changed=changed)
            print(f"clothes: {name or 'unknown'} changed={changed} say={say} ({time.time()-t0:.1f}s)", flush=True)
            return self._json(200, {"say": say, "changed": changed})
        if u.path == "/answer":      # POST audio/wav ?key=..&q=..  -> the owner's answer to a profile question
            key = q.get("key", ["misc"])[0]; question = q.get("q", [""])[0]
            text = transcribe(body)
            ans = extract_answer(question, text)
            print(f"answer: key={key} heard={text!r} -> {ans!r} ({time.time()-t0:.1f}s)", flush=True)
            if not ans: return self._json(200, {"ok": False, "heard": text, "say": "ごめん、よう聞き取れへんかった。また今度聞くわ"})
            profile["facts"][key] = {"q": question, "a": ans, "time": time.strftime("%Y-%m-%d %H:%M")}; save_profile(profile)
            log_event("answer", name=(owner() or {}).get("name"), heard=text, say=f"{question} -> {ans}")
            return self._json(200, {"ok": True, "heard": text, "answer": ans, "say": f"なるほど、{ans}なんやな。覚えとくわ"})
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
.k.sighting{background:#7a2b2b}.k.visit{background:#2b4a7a}.k.owner_photo{background:#7a6a2b}.k.learn{background:#2b7a4a}.k.clothes{background:#5a2b7a}
</style>
<header><h1>Stack-chan ログ</h1><label>種類 <select id=kind><option value="">すべて</option><option value=visit>訪問</option><option value=sighting>ご主人以外</option><option value=owner_photo>ご主人の写真</option><option value=learn>名前学習</option><option value=clothes>服装</option></select></label>
<label>人 <select id=who><option value="">すべて</option></select></label><button onclick="load()">更新</button></header>
<div class=people id=people></div>
<div class=people id=profile></div>
<div class=people id=topics></div>
<div class=grid id=grid></div>
<script>
async function load(){
  const [ev, ppl, pr, tp] = await Promise.all([fetch('/events.json?limit=1000').then(r=>r.json()), fetch('/people').then(r=>r.json()), fetch('/profile').then(r=>r.json()), fetch('/topics').then(r=>r.json())]);
  document.getElementById('profile').innerHTML = '<div class=person><b>ご主人のプロフィール</b> ' + (Object.entries(pr.facts||{}).map(([k,v])=>`<div>${v.q} → <b>${v.a}</b> <span class=t>${v.time}</span> <button onclick="fetch('/forget_fact?key='+encodeURIComponent('${k}')).then(load)">×</button></div>`).join('')||'<span class=t>まだ何も聞いていません</span>') + '</div>';
  document.getElementById('topics').innerHTML = '<div class=person><b>話題</b> <button onclick="refreshTopics()">今すぐ探す</button>' + (tp.slice(0,10).map(t=>`<div>📰 <a href="${t.url}" target=_blank style="color:#9cf">${t.title}</a> <span class=t>${t.source} · ${t.time}${t.shown?' · 掲示済':''}</span><div>${t.summary}</div></div>`).join('')||'<span class=t>まだありません</span>') + '</div>';
  const who=document.getElementById('who'); const cur=who.value; const names=[...new Set(ev.map(e=>e.name||'unknown'))];
  who.innerHTML='<option value="">すべて</option>'+names.map(n=>`<option ${n===cur?'selected':''}>${n}</option>`).join('');
  document.getElementById('people').innerHTML = ppl.map(p=>`<div class="person ${p.owner?'owner':''}">${p.owner?'👑 ':''}${p.name} <span class=t>${p.seen}回 / 最終 ${p.last||''}</span>
     <button onclick="fetch('/owner?name='+encodeURIComponent('${p.name}')).then(load)">ご主人にする</button> <button onclick="if(confirm('${p.name} を忘れる？'))fetch('/forget?name='+encodeURIComponent('${p.name}')).then(load)">忘れる</button></div>`).join('');
  const k=document.getElementById('kind').value, w=who.value;
  document.getElementById('grid').innerHTML = ev.filter(e=>(!k||e.kind===k)&&(!w||(e.name||'unknown')===w)).map(e=>`<div class=card>${e.photo?`<a href="/photos/${e.photo}" target=_blank><img loading=lazy src="/photos/${e.photo}"></a>`:''}
     <div class=b><span class="k ${e.kind}">${{visit:'訪問',sighting:'ご主人以外',owner_photo:'ご主人の写真',learn:'名前学習',clothes:'服装',answer:'プロフィール'}[e.kind]||e.kind}</span><b>${e.name||'知らない人'}</b> <span class=t>${e.time}${e.mode?' · '+e.mode:''}</span>
     ${e.say?`<div>「${e.say}」</div>`:''}${e.heard?`<div class=t>聞き取り: ${e.heard}</div>`:''}</div></div>`).join('');
}
function refreshTopics(){ fetch('/topics/refresh').then(load); }
document.getElementById('kind').onchange=load; document.getElementById('who').onchange=load; load(); setInterval(load, 30000);
</script>"""

print(f"brain on :{PORT}  people={len(people)}  vlm={VLM_MODEL}", flush=True)
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
