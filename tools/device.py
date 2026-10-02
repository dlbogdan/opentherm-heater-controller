#!/usr/bin/env python3
"""Shim: the canonical USB device CLI lives in the micropy-system framework
(micropy-system/tools/device.py). Commands: reset, monitor, state, log, files,
selftest, probe, exec. mpremote/pyserial come from the framework
requirements-dev.txt (installed by tools/setup_build_env.sh)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["MICROPY_APP_ROOT"] = ROOT
os.execv(
    sys.executable,
    [sys.executable,
     os.path.join(ROOT, "micropy-system", "tools", "device.py")] + sys.argv[1:],
)


