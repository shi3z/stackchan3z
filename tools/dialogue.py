#!/usr/bin/env python3
"""Two Stack-chans talk to each other (AI to AI). Each unit has a persona; the LLM writes the next lines from the
running transcript, a few lines ahead, while the current line is being spoken. Kansai dialect, with head gestures.
usage: dialogue.py --a http://<boardA> --b http://<boardB> [--topic "AI"] [--news] [--turns 12] [--name-a ...] [--name-b ...]
--news: talk about today's headlines (Google News RSS). env: STACKCHAN_VLM_URL / STACKCHAN_VLM_MODEL, STACKCHAN_TTS_A/B"""
import os, sys, json, re, time, argparse, threading, queue, urllib.request, urllib.parse
ap = argparse.ArgumentParser(); ap.add_argument("--a", required=True); ap.add_argument("--b", required=True)
ap.add_argument("--topic", default="AI"); ap.add_argument("--news", action="store_true"); ap.add_argument("--turns", type=int, default=12)
ap.add_argument("--name-a", default="ハナ"); ap.add_argument("--name-b", default="ガク"); ap.add_argument("--face", action="store_true")
args = ap.parse_args()
VLM_URL = os.environ.get("STACKCHAN_VLM_URL", "http://127.0.0.1:11435/api/chat"); VLM_MODEL = os.environ.get("STACKCHAN_VLM_MODEL", "qwen3.8:27b-mxfp8")
TTS = {"A": os.environ.get("STACKCHAN_TTS_A", "http://127.0.0.1:9001/say"), "B": os.environ.get("STACKCHAN_TTS_B", "http://127.0.0.1:9003/say")}
BOARD = {"A": args.a.rstrip("/"), "B": args.b.rstrip("/")}
NAME = {"A": args.name_a, "B": args.name_b}
PERSONA = {"A": f"{NAME['A']}: 家にいるスタックちゃん。好奇心が強く、ちょっと皮肉屋で、ツッコミが鋭い。関西弁。",
           "B": f"{NAME['B']}: 会社用のスタックちゃん。のんびりしていて前向き、たまに天然なことを言う。声が低い。関西弁。"}

def get(url, timeout=60):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r: return r.read()
    except Exception as e: print("  !", url[:80], e); return b""

def llm(prompt):
    body = {"model": VLM_MODEL, "stream": False, "think": False, "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(VLM_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r: return json.load(r)["message"]["content"]

def headlines(n=8):
    import xml.etree.ElementTree as ET
    out = []
    for q in ("トップニュース", "AI 最新"):
        url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=ja&gl=JP&ceid=JP:ja"
        try:
            xml = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20).read()
            out += [(it.findtext("title") or "").strip() for it in ET.fromstring(xml).iter("item")][:n // 2]
        except Exception as e: print("news error:", e)
    return out

def next_lines(history, who, count=2):
    hist = "\n".join(f"{NAME[w]}: {t}" for w, t in history) or "（まだ何も話していない）"
    prompt = ("2体の小さなロボット「スタックちゃん」が机の上で雑談しています。\n" + PERSONA["A"] + "\n" + PERSONA["B"] + "\n"
              f"話題: {TOPIC}\n\nこれまでの会話:\n{hist}\n\n"
              f"次は {NAME[who]} の番です。{NAME[who]} の発言から始めて、交互に{count}行だけ続きを書いてください。"
              "1行35文字以内、相手の発言にちゃんと反応し、質問や意見で会話を前に進める。同じ話の繰り返しは禁止。"
              + ("最後の行は会話を締める一言にする。" if FINAL else "") +
              "各行に act（nod / shake / tilt / none）を付けて、次のJSONだけを返してください。\n"
              '{"lines": [{"who": "' + who + '", "text": "...", "act": "nod"}, {"who": "' + ("B" if who == "A" else "A") + '", "text": "...", "act": "none"}]}')
    m = re.search(r"\{.*\}", llm(prompt), re.S)
    lines = json.loads(m.group(0))["lines"]
    def key(w):
        w = str(w).strip()
        if w.startswith(NAME["A"]): return "A"
        if w.startswith(NAME["B"]): return "B"
        return "B" if w.upper().startswith("B") else "A"
    out = []
    for l in lines:
        if not l.get("text"): continue
        out.append((key(l.get("who", who)), re.sub(r"[\x00-\x1f]", " ", str(l.get("text", ""))).strip(), str(l.get("act", "none")).lower()))
    # enforce alternation starting with `who`
    fixed = []; w = who
    for _, t, a in out[:count]: fixed.append((w, t, a)); w = "B" if w == "A" else "A"
    return fixed

def playing(who):
    try: return bool(json.loads(get(BOARD[who] + "/api/status", 5))["speech"]["playing"])
    except Exception: return False

def say(who, text, act):
    other = "B" if who == "A" else "A"; q = urllib.parse.quote(text)
    get(BOARD[who] + f"/api/display?text={q}&size=2&ms={max(3000, len(text) * 350)}", 5)
    if act in ("nod", "shake"): get(BOARD[who] + f"/api/head?gesture={act}&n=1", 5)
    elif act == "tilt": get(BOARD[who] + "/api/head?tilt=105&speed=4", 5)
    if act == "shake": get(BOARD[other] + "/api/head?gesture=nod&n=1", 5)
    get(BOARD[who] + f"/api/say?text={q}&emotion=happy", 90)
    t0 = time.time(); time.sleep(0.4)
    while time.time() - t0 < 30 and playing(who): time.sleep(0.25)
    if act == "tilt": get(BOARD[who] + "/api/head?tilt=90&speed=4", 5)
    time.sleep(0.2)

TOPIC = args.topic
if args.news:
    hl = headlines(); TOPIC = "今日のニュース: " + " / ".join(hl) if hl else TOPIC
    print("headlines:", *hl, sep="\n  ")
FINAL = False
if args.face:
    get(BOARD["A"] + "/api/head?pan=120&tilt=90&speed=3", 5); get(BOARD["B"] + "/api/head?pan=60&tilt=90&speed=3", 5); time.sleep(1.5)
history = []; who = "A"; pending = queue.Queue()
def producer():
    global who, FINAL
    spoken = 0
    while spoken < args.turns:
        FINAL = spoken + 2 >= args.turns
        try: lines = next_lines(history, who, 2)
        except Exception as e: print("llm error:", e); break
        for w, t, a in lines:
            history.append((w, t)); pending.put((w, t, a)); spoken += 1
            who = "B" if w == "A" else "A"
    pending.put(None)
threading.Thread(target=producer, daemon=True).start()
print("go!")
while True:
    item = pending.get()
    if item is None: break
    w, t, a = item
    # warm the voice, then speak (the producer keeps writing ahead while we talk)
    get(TTS[w] + "?text=" + urllib.parse.quote(t) + "&emotion=happy", 120)
    print(f"  {NAME[w]} [{a}] {t}", flush=True)
    say(w, t, a)
get(BOARD["A"] + "/api/head?gesture=center", 5); get(BOARD["B"] + "/api/head?gesture=center", 5)
json.dump({"topic": TOPIC, "lines": [{"who": w, "text": t} for w, t in history]}, open("dialogue_last.json", "w"), ensure_ascii=False, indent=1)
print("done")
