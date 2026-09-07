#!/usr/bin/env python3
"""Send command lines to the M5Stack over USB serial and print replies.
Opens the port with default DTR/RTS (no toggling => no reset).
usage: serial_cmd.py [-t SECONDS] ["<command>" ...]"""
import sys, os, time, argparse, glob, serial
ap = argparse.ArgumentParser(); ap.add_argument('-t', type=float, default=4.0); ap.add_argument('cmds', nargs='*')
a = ap.parse_args()
port = os.environ.get('STACKCHAN_PORT') or (glob.glob('/dev/cu.usbmodem*') + glob.glob('/dev/ttyACM*') or ['/dev/cu.usbmodem1'])[0]
s = serial.Serial(port, 115200, timeout=0.2)
time.sleep(0.5); s.reset_input_buffer()
for c in a.cmds:
    s.write((c + '\n').encode()); s.flush(); time.sleep(0.3)
end = time.time() + a.t
while time.time() < end:
    d = s.read(4096)
    if d: sys.stdout.write(d.decode('utf-8', 'replace')); sys.stdout.flush()
s.close()
