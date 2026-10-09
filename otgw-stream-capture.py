#!/usr/bin/env python3
"""OTGW stream capture: read-only debug mirror of the gateway's serial stream.

The OTGW network firmware (otgw.tclcode.com) mirrors its entire serial stream on
TCP port 12700: every command any client sends (CS=.., CH=..), every ack
(CS: ..), and every OpenTherm frame (T/B/R/A + 8 hex digits). Connecting a
second read-only client is safe -- the gateway supports several simultaneous
connections (HA's opentherm_gw integration holds one). This is the host-side
diagnostic for validating the board's OTGW driver against a real gateway: it
shows exactly what the Pico writes and what the gateway acks.

Usage:
    python3 otgw-stream-capture.py <otgw-host> [port] [--out FILE]

Output: one line per gateway line, prefixed with wall-clock time:
    2026-10-05 21:14:03.412  CS=42.0
    2026-10-05 21:14:03.455  CS: 42.0
    2026-10-05 21:14:03.900  B8200000
Reconnects forever (5 s backoff). Ctrl-C to stop. Pure stdlib -- runs on any
Python 3 (HA host add-on terminal, a Pi, this Mac).

Notes:
- READ-ONLY: this script never writes to the socket.
- Port 12701 (if present) is the gateway's own event log; point the script at
  it for gateway-side events instead of the raw serial mirror.
"""
import socket
import sys
import time
from datetime import datetime

RECONNECT_S = 5


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    host = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 12700
    out_path = None
    if "--out" in sys.argv:
        out_path = sys.argv[sys.argv.index("--out") + 1]

    def emit(line):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        record = f"{stamp}  {line}"
        print(record, flush=True)
        if out_path:
            with open(out_path, "a") as f:
                f.write(record + "\n")

    while True:
        try:
            sock = socket.create_connection((host, port), timeout=10)
            emit(f"### connected {host}:{port}")
            buf = b""
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    emit(line.decode(errors="replace").rstrip("\r"))
                if len(buf) > 4096:  # unterminated garbage guard
                    emit(buf.decode(errors="replace"))
                    buf = b""
            emit("### gateway closed the connection")
        except OSError as exc:
            emit(f"### connect failed: {exc}")
        time.sleep(RECONNECT_S)


if __name__ == "__main__":
    sys.exit(main())
