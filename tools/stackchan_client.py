#!/usr/bin/env python3
"""Stack-chan API client for any machine on the tailnet (Python 3 stdlib only).
  stackchan_client.py status
  stackchan_client.py say "こんにちはー" [--emotion happy]
  stackchan_client.py display "こんにちは\\n元気？" [--size 2] [--ms 4000]
  stackchan_client.py head nod|shake|center [--n 2]
  stackchan_client.py head --pan 60 --tilt 100 [--speed 3]
  stackchan_client.py photo out.jpg [--q 80]
  stackchan_client.py fetch [URL]           (make the board GET a URL)
Base URL: $STACKCHAN_URL (e.g. https://<your-mac>.<tailnet>.ts.net:8443 or http://stackchan.local)"""
import sys, os, json, argparse, urllib.request, urllib.parse
BASE = os.environ.get("STACKCHAN_URL", "http://stackchan.local").rstrip("/")

def get(path, params=None, timeout=60):
    url = BASE + path + ("?" + urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None}) if params else "")
    with urllib.request.urlopen(url, timeout=timeout) as r: return r.headers.get_content_type(), r.read()

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
sub = ap.add_subparsers(dest="cmd", required=True)
sub.add_parser("status")
p = sub.add_parser("say"); p.add_argument("text"); p.add_argument("--emotion", default="happy")
p = sub.add_parser("display"); p.add_argument("text"); p.add_argument("--size", type=int, default=2); p.add_argument("--ms", type=int, default=4000)
p = sub.add_parser("head"); p.add_argument("gesture", nargs="?"); p.add_argument("--n", type=int); p.add_argument("--pan", type=float); p.add_argument("--tilt", type=float); p.add_argument("--speed", type=float)
p = sub.add_parser("photo"); p.add_argument("out", nargs="?", default="photo.jpg"); p.add_argument("--q", type=int, default=80)
p = sub.add_parser("fetch"); p.add_argument("url", nargs="?")
a = ap.parse_args()

if a.cmd == "status":   ct, b = get("/api/status")
elif a.cmd == "say":    ct, b = get("/api/say", {"text": a.text, "emotion": a.emotion}, timeout=120)
elif a.cmd == "display": ct, b = get("/api/display", {"text": a.text.replace("\\n", "\n"), "size": a.size, "ms": a.ms})
elif a.cmd == "head":   ct, b = get("/api/head", {"gesture": a.gesture, "n": a.n, "pan": a.pan, "tilt": a.tilt, "speed": a.speed})
elif a.cmd == "photo":
    ct, b = get("/api/camera.jpg", {"q": a.q}); open(a.out, "wb").write(b); print(f"saved {a.out} ({len(b)} bytes)"); sys.exit(0)
elif a.cmd == "fetch":  ct, b = get("/api/fetch", {"url": a.url})
print(json.dumps(json.loads(b), ensure_ascii=False, indent=1) if ct == "application/json" else b.decode(errors="replace"))
