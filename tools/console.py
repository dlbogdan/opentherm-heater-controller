#!/usr/bin/env python3
"""Line-based debug console client for the Pico's network REPL.

Connects to the device's TCP console port, sends one line of Python at a time,
and prints the device's response. The device executes each line in a persistent
namespace (so state carries across lines, like a real REPL) and returns the
captured stdout/stderr followed by an END marker.

Protocol: client -> "<line>\n"; device -> "<output>\n<<<END>>>\n".
Type `exit` or `quit` (or Ctrl-D) to disconnect.
"""

import socket
import sys

END_MARKER = "<<<END>>>"


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    host, port = sys.argv[1], int(sys.argv[2])
    print("Connecting to %s:%s ... (type 'exit' to quit)" % (host, port))
    try:
        s = socket.create_connection((host, port), timeout=5)
    except OSError as e:
        sys.exit("could not connect to %s:%s: %s" % (host, port, e))
    s.settimeout(None)
    print("Connected. Device banner:")
    _drain_until_end(s)

    buf = b""
    while True:
        try:
            line = input("pico> ")
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            break
        line = line.strip()
        if not line:
            continue
        if line in ("exit", "quit"):
            break
        s.sendall((line + "\n").encode())
        _drain_until_end(s)
    s.close()


def _drain_until_end(s):
    """Read from the socket until the END marker; print what we got."""
    buf = b""
    marker = (END_MARKER + "\n").encode()
    while marker not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    idx = buf.find(marker)
    if idx >= 0:
        buf = buf[:idx]
    if buf:
        sys.stdout.write(buf.decode("utf-8", "replace"))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
