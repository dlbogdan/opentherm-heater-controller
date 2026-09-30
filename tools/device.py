#!/usr/bin/env python3
"""USB device CLI for the OpenTherm heater controller.

Wraps the ad-hoc ``mpremote`` / pyserial calls used during development into one
discoverable command. It talks to the Pico over its USB-CDC serial port (the
board must be connected to this machine).

Examples
--------
    tools/device.py state                 # version, OTA state, system config
    tools/device.py log --lines 40        # tail the on-device /log.txt
    tools/device.py monitor --seconds 30  # passively watch the console (no reset)
    tools/device.py selftest              # run the control-core self-test on-device
    tools/device.py probe --seconds 15    # run app main() for N s, report result
    tools/device.py exec "print(1+1)"     # run arbitrary MicroPython on the device
    tools/device.py reset                 # soft-reset the board
    tools/device.py files --path /apps/a  # list a directory

The serial port is auto-detected (first /dev/cu.usbmodem*); override with --port.
mpremote and pyserial are expected in the project's .venv (tools/setup_build_env.sh
plus `pip install mpremote pyserial`).
"""

import argparse
import glob
import os
import subprocess
import sys
import time

# --- Paths ---------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
VENV = os.path.join(APP_ROOT, ".venv")
MPREMOTE = os.path.join(VENV, "bin", "mpremote")
VENV_PY = os.path.join(VENV, "bin", "python")


def find_port():
    """Best-effort detection of the Pico's USB-CDC serial port."""
    for pattern in ("/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/tty.usbmodem*"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return "/dev/cu.usbmodem203201"  # last-resort default for this board


# --- mpremote helpers ----------------------------------------------------------
def mp(port, *args, timeout=60):
    """Run an mpremote command against the board; return (rc, combined_output)."""
    if not os.path.exists(MPREMOTE):
        sys.exit("mpremote not found in .venv. Run: tools/setup_build_env.sh && "
                 ".venv/bin/pip install mpremote pyserial")
    cmd = [MPREMOTE, "connect", port] + list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "mpremote timed out"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def wait_port(port, timeout_s=120):
    """Block until the serial port node appears (board re-enumerates after reset)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if os.path.exists(port):
            return True
        time.sleep(1)
    return False


# --- Commands ------------------------------------------------------------------
def cmd_reset(args):
    port = args.port or find_port()
    if not wait_port(port, args.timeout):
        sys.exit("serial port %s did not appear within %ss" % (port, args.timeout))
    time.sleep(1)
    rc, out = mp(port, "reset", timeout=30)
    print(out.strip())
    if rc != 0:
        # Port can drop right as the board reboots; that still means the reset landed.
        print("(reset likely sent; board is re-enumerating)")
    else:
        print("reset sent")


def cmd_monitor(args):
    """Passively capture console output WITHOUT resetting the board."""
    port = args.port or find_port()
    if not wait_port(port, args.timeout):
        sys.exit("serial port %s did not appear within %ss" % (port, args.timeout))
    time.sleep(1)  # let CDC settle
    import serial  # pyserial, from .venv
    with serial.Serial(port, 115200, timeout=0.5) as s:
        s.reset_input_buffer()
        end = time.time() + args.seconds
        buf = []
        while time.time() < end:
            chunk = s.read(2048)
            if chunk:
                buf.append(chunk.decode("utf-8", "replace"))
        text = "".join(buf)
    print("=== captured %d bytes over %ss ===" % (len(text), args.seconds))
    print(text if text else "(no console output captured)")


def cmd_state(args):
    port = args.port or find_port()
    if not wait_port(port, args.timeout):
        sys.exit("serial port %s did not appear" % port)
    time.sleep(1)
    for label, cmd in (
        ("VERSION", ["cat", "/version.txt"]),
        ("OTA-STATE", ["cat", "/ota-state.json"]),
        ("SYSTEM-CONFIG", ["cat", "/system-config.json"]),
    ):
        rc, out = mp(port, *cmd, timeout=30)
        print("--- %s ---" % label)
        print(out.strip())
        print()


def cmd_log(args):
    port = args.port or find_port()
    if not wait_port(port, args.timeout):
        sys.exit("serial port %s did not appear" % port)
    time.sleep(1)
    code = ("d=open('/log.txt').read(); print('\\n'.join(d.splitlines()[-%d:]))"
            % max(1, args.lines))
    rc, out = mp(port, "exec", code, timeout=30)
    print(out.strip())


def cmd_files(args):
    port = args.port or find_port()
    if not wait_port(port, args.timeout):
        sys.exit("serial port %s did not appear" % port)
    time.sleep(1)
    rc, out = mp(port, "ls", args.path, timeout=30)
    print(out.strip())


def cmd_selftest(args):
    port = args.port or find_port()
    if not wait_port(port, args.timeout):
        sys.exit("serial port %s did not appear" % port)
    time.sleep(1)
    code = (
        "import sys\n"
        "for p in ('apps/a','apps/b'): sys.path.insert(0,p)\n"
        "import selftest\n"
        "ok=selftest.run()\n"
        "print('DEVICE SELF-TEST:', 'PASS' if ok else 'FAIL')\n"
    )
    rc, out = mp(port, "exec", code, timeout=120)
    print(out.strip())
    sys.exit(0 if "PASS" in out else 1)


def cmd_probe(args):
    port = args.port or find_port()
    if not wait_port(port, args.timeout):
        sys.exit("serial port %s did not appear" % port)
    time.sleep(1)
    code = (
        "import sys\n"
        "for p in ('apps/a','apps/b'): sys.path.insert(0,p)\n"
        "import uasyncio as asyncio, app_entry\n"
        "async def probe():\n"
        "    try:\n"
        "        await asyncio.wait_for(app_entry.main(), timeout=%d)\n"
        "        print('PROBE: main() returned (unexpected)')\n"
        "    except asyncio.TimeoutError:\n"
        "        print('PROBE: ran %ds without crashing -- OK')\n"
        "    except asyncio.CancelledError:\n"
        "        print('PROBE: cancelled')\n"
        "    except Exception as e:\n"
        "        print('PROBE EXCEPTION:'); sys.print_exception(e)\n"
        "asyncio.run(probe())\n"
        % (args.seconds, args.seconds)
    )
    rc, out = mp(port, "exec", code, timeout=args.seconds + 60)
    print(out.strip())


def cmd_exec(args):
    port = args.port or find_port()
    if not wait_port(port, args.timeout):
        sys.exit("serial port %s did not appear" % port)
    time.sleep(1)
    rc, out = mp(port, "exec", args.code, timeout=args.timeout)
    print(out.strip())
    sys.exit(rc)


# --- Parser --------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default=None, help="serial port (default: auto-detect)")
    p.add_argument("--timeout", type=int, default=120,
                   help="seconds to wait for the port to appear (default 120)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("reset", help="soft-reset the board").set_defaults(fn=cmd_reset)

    m = sub.add_parser("monitor", help="passively capture console output (no reset)")
    m.add_argument("--seconds", type=int, default=30)
    m.set_defaults(fn=cmd_monitor)

    sub.add_parser("state", help="print version, OTA state, system config").set_defaults(fn=cmd_state)

    lg = sub.add_parser("log", help="tail the on-device log.txt")
    lg.add_argument("--lines", type=int, default=40)
    lg.set_defaults(fn=cmd_log)

    f = sub.add_parser("files", help="list a directory on the device")
    f.add_argument("--path", default="/")
    f.set_defaults(fn=cmd_files)

    sub.add_parser("selftest", help="run the control-core self-test on-device").set_defaults(fn=cmd_selftest)

    pr = sub.add_parser("probe", help="run app main() for N seconds and report")
    pr.add_argument("--seconds", type=int, default=15)
    pr.set_defaults(fn=cmd_probe)

    e = sub.add_parser("exec", help="run a MicroPython snippet on the device")
    e.add_argument("code")
    e.set_defaults(fn=cmd_exec)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
