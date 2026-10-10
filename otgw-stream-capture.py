#!/usr/bin/env python3
"""OTGW stream capture: read-only debug mirror of the gateway's serial stream.

The gateway's network firmware mirrors its entire serial stream on a TCP
"net2serial" port: every command any client sends (CS=.., CH=..), every ack
(CS: ..), and every OpenTherm frame (direction char + 8 hex digits).
PORT DEPENDS ON THE NETWORK FIRMWARE (verified live 2026-10-10 on the house
gateway 10.9.30.52, rvdbreemen OTGW-firmware 0.10.3 + PIC 6.6):
  * rvdbreemen ESP firmware (this house): net2ser port **25238**
  * older otgw.tclcode.com network firmware: port 12700
Connecting a second read-only client is safe -- the mirror supports several
simultaneous connections. This is the host-side diagnostic for validating
the board's OTGW driver against a real gateway: it shows exactly what the
Pico writes and what the gateway acks.

Usage:
    python3 otgw-stream-capture.py <otgw-host> [port] [--out FILE]
    python3 otgw-stream-capture.py 10.9.30.52 --out otgw-capture.log   # 25238 default

Output: one line per gateway line, prefixed with wall-clock time:
    2026-10-05 21:14:03.412  CS=42.0
    2026-10-05 21:14:03.455  CS: 42.0
    2026-10-05 21:14:03.900  B8200000
Reconnects forever (backoff 5 s .. 60 s, reset on connect). Ctrl-C to stop.
Pure stdlib -- runs on any Python 3 (Linux server, HA host add-on terminal,
this Mac); nothing OS-specific.

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
MAX_BACKOFF_S = 60


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    host = sys.argv[1]
    # argv[2] is the port ONLY if it is one: `--out FILE` may come first.
    # Default = the house gateway's rvdbreemen net2ser port (old firmware: 12700).
    port = (int(sys.argv[2])
            if len(sys.argv) > 2 and not sys.argv[2].startswith("-")
            else 25238)
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

    backoff = RECONNECT_S
    while True:
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=10)
            backoff = RECONNECT_S
            emit(f"### connected {host}:{port}")
            buf = b""
            while True:
                data = sock.recv(4096)
                if not data:
                    emit("### gateway closed the connection")
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    emit(line.decode(errors="replace").rstrip("\r"))
                if len(buf) > 4096:  # unterminated garbage guard
                    emit(buf.decode(errors="replace"))
                    buf = b""
        except OSError as exc:
            # Also catches recv() TimeoutError: the gateway streams ~1
            # line/s, so 10 s of silence IS a dead link -> reconnect.
            emit(f"### connect/read failed: {exc}")
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        time.sleep(backoff)
        # Grow 5 s .. 60 s across consecutive failures (a rebooting
        # gateway cannot fill the log overnight); reset on connect.
        backoff = min(backoff * 2, MAX_BACKOFF_S)


if __name__ == "__main__":
    sys.exit(main())
