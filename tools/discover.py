#!/usr/bin/env python3
"""Find the Pico on the LAN by probing its HTTP API port.

Discovers the local subnet from the host's primary IPv4 address, scans the /24
concurrently, and reports any host whose ``/status`` endpoint looks like this
project's device. Usage:  tools/discover.py [port]   (default 8080)
"""

import concurrent.futures
import json
import socket
import struct
import sys


def local_subnet():
    """Return (network_octets, host_octet) for the primary IPv4, e.g. (b'10.9.30', 76)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packet sent; just picks the egress interface
        ip = s.getsockname()[0]
    except OSError:
        ip = socket.gethostbyname(socket.gethostname())
    finally:
        s.close()
    a, b, c, d = ip.split(".")
    return (a, b, c), int(d)


def probe(port, candidate):
    try:
        with socket.create_connection((candidate, port), timeout=0.15) as s:
            s.sendall(b"GET /status HTTP/1.0\r\nHost: x\r\n\r\n")
            head = s.recv(2048).decode("utf-8", "replace")
        if '"service"' in head or '"device"' in head or '"version"' in head:
            return candidate
    except OSError:
        pass
    return None


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    base, me = local_subnet()
    prefix = ".".join(base)
    print("Scanning %s.0/24 on port %s for the OpenTherm Pico..." % (prefix, port))
    hosts = [prefix + ".%d" % i for i in range(1, 255) if i != me]
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        for hit in ex.map(lambda h: probe(port, h), hosts):
            if hit:
                print("Found device: %s   (try:  export OTC_IP=%s)" % (hit, hit))
                return
    print("No device found. Is it powered on and connected to the same network?")
    sys.exit(1)


if __name__ == "__main__":
    main()
