#!/usr/bin/env python3
"""TCP forwarder: LAN <listen_port> on this Mac -> a tailnet host:port.
Lets the M5Stack (LAN only) reach a tailnet server through this Mac without any root/NAT setup.
usage: tailnet_proxy.py <listen_port> <tailnet_host> <tailnet_port>
example: tailnet_proxy.py 9000 my-gpu-box 8000   ->  board target http://<mac-lan-ip>:9000/"""
import sys, socket, threading
lport, host, port = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
def pipe(a, b):
    try:
        while (d := a.recv(65536)): b.sendall(d)
    except OSError: pass
    finally:
        for s in (a, b):
            try: s.shutdown(socket.SHUT_RDWR)
            except OSError: pass
def handle(c):
    try: r = socket.create_connection((host, port), timeout=10)
    except OSError as e: print("connect failed:", e); c.close(); return
    threading.Thread(target=pipe, args=(c, r), daemon=True).start()
    threading.Thread(target=pipe, args=(r, c), daemon=True).start()
ls = socket.socket(); ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
ls.bind(("0.0.0.0", lport)); ls.listen(16)
print(f"forwarding 0.0.0.0:{lport} -> {host}:{port}")
while True: handle(ls.accept()[0])
