#!/usr/bin/env python3
"""Two Stack-chans do a manzai (Kansai comedy duo) routine about a topic.
A = tsukkomi (straight man), B = boke (funny man). The script is written by the LLM, lines are pre-synthesized,
then played alternately with head gestures (tsukkomi shakes, boke nods/tilts, the other reacts).
usage: manzai.py --a http://<boardA> --b http://<boardB> [--topic AI] [--lines 12]
env: STACKCHAN_VLM_URL (Ollama /api/chat), STACKCHAN_VLM_MODEL, STACKCHAN_TTS_A, STACKCHAN_TTS_B (proxy /say URLs for cache warm-up)"""
import os, sys, json, re, time, argparse, urllib.request, urllib.parse
ap = argparse.ArgumentParser(); ap.add_argument("--a", required=True); ap.add_argument("--b", required=True)
ap.add_argument("--topic", default="AI"); ap.add_argument("--lines", type=int, default=12); ap.add_argument("--script", help="use a saved script JSON instead of the LLM")
ap.add_argument("--face", action="store_true", help="turn the heads toward each other first (A pan 120, B pan 60)")
ap.add_argument("--news", action="store_true", help="write the routine about today's headlines (Google News RSS)")
ap.add_argument("--if-owner-around", type=int, default=0, help="skip unless the brain saw the owner within N minutes (env STACKCHAN_BRAIN)")
args = ap.parse_args()
VLM_URL = os.environ.get("STACKCHAN_VLM_URL", "http://127.0.0.1:11435/api/chat"); VLM_MODEL = os.environ.get("STACKCHAN_VLM_MODEL", "qwen3.8:27b-mxfp8")
TTS = {"A": os.environ.get("STACKCHAN_TTS_A", "http://127.0.0.1:9001/say"), "B": os.environ.get("STACKCHAN_TTS_B", "http://127.0.0.1:9003/say")}
BOARD = {"A": args.a.rstrip("/"), "B": args.b.rstrip("/")}

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
    for q in ("トップニュース", "AI 最新", "テクノロジー"):
        url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=ja&gl=JP&ceid=JP:ja"
        try:
            xml = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20).read()
            out += [(it.findtext("title") or "").strip() for it in ET.fromstring(xml).iter("item")][:3]
        except Exception as e: print("news error:", e)
    return out[:n]

def write_script():
    theme = args.topic
    if args.news:
        hl = headlines(); print("headlines:", *hl, sep="\n  ")
        theme = "今日のニュース（" + time.strftime("%m月%d日") + "）。次の見出しから面白くできるものを2〜3つ選ぶ: " + " / ".join(hl)
    prompt = (f"2体の小さなロボット「スタックちゃん」A と B が関西弁で漫才をします。テーマは「{theme}」。\n"
              "A はツッコミ役（冷静で切れ味がいい、たまに呆れる）、B はボケ役（のんきで妙に自信がある、少しズレたことを言う）。\n"
              "「はいどうもー」で始まり、途中でテーマについての具体的なボケとツッコミを3〜4回繰り返し（ニュースなら見出しの内容を踏まえて、事実を捏造しない）、最後は「もうええわ」「ありがとうございましたー」で締める。\n"
              f"全部で{args.lines}行前後、1行は35文字以内、A と B は交互。各行に動きを付ける: act は shake（首を横に振るツッコミ）、nod（うなずく）、tilt（首をかしげる）、none のいずれか。\n"
              "次のJSONだけを返してください。\n"
              '{"lines": [{"who": "A", "text": "はいどうもー、スタックちゃんです", "act": "nod"}, {"who": "B", "text": "...", "act": "tilt"}]}')
    txt = llm(prompt); m = re.search(r"\{.*\}", txt, re.S)
    lines = json.loads(m.group(0))["lines"]
    return [{"who": l.get("who", "A").strip().upper()[:1], "text": re.sub(r"[\x00-\x1f]", " ", str(l.get("text", ""))).strip(), "act": str(l.get("act", "none")).lower()} for l in lines if l.get("text")]

def playing(who):
    d = get(BOARD[who] + "/api/status", 5)
    try: return bool(json.loads(d)["speech"]["playing"])
    except Exception: return False

def say(who, text, act):
    other = "B" if who == "A" else "A"
    q = urllib.parse.quote(text)
    get(BOARD[who] + f"/api/display?text={q}&size=2&ms={max(3000, len(text) * 350)}", 5)
    if act in ("nod", "shake"): get(BOARD[who] + f"/api/head?gesture={act}&n=1", 5)
    elif act == "tilt": get(BOARD[who] + "/api/head?tilt=105&speed=4", 5)
    # the partner reacts: a nod when being scolded, a small tilt when hearing a boke
    if act == "shake": get(BOARD[other] + "/api/head?gesture=nod&n=1", 5)
    get(BOARD[who] + f"/api/say?text={q}&emotion=happy", 90)
    t0 = time.time(); time.sleep(0.4)
    while time.time() - t0 < 30 and playing(who): time.sleep(0.25)
    if act == "tilt": get(BOARD[who] + "/api/head?tilt=90&speed=4", 5)
    time.sleep(0.25)

if args.if_owner_around:
    try:
        d = json.loads(get(os.environ.get("STACKCHAN_BRAIN", "http://127.0.0.1:9002") + "/presence", 5))
        if d.get("owner_seen_min", 9999) > args.if_owner_around: print("owner not around, skipping"); sys.exit(0)
    except Exception as e: print("presence check failed:", e)
if args.script: lines = json.load(open(args.script))["lines"]
else:
    print("writing the script ..."); lines = write_script()
    json.dump({"topic": args.topic, "lines": lines}, open("manzai_last.json", "w"), ensure_ascii=False, indent=1)
for l in lines: print(f"  {l['who']} [{l['act']}] {l['text']}")
print("warming the voices ...")
for l in lines: get(TTS[l["who"]] + "?text=" + urllib.parse.quote(l["text"]) + "&emotion=happy", 120)
if args.face:
    get(BOARD["A"] + "/api/head?pan=120&tilt=90&speed=3", 5); get(BOARD["B"] + "/api/head?pan=60&tilt=90&speed=3", 5); time.sleep(1.5)
print("go!")
for l in lines: say(l["who"], l["text"], l["act"])
get(BOARD["A"] + "/api/head?gesture=center", 5); get(BOARD["B"] + "/api/head?gesture=center", 5)
print("done")
