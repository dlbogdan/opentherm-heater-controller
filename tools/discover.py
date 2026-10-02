#!/usr/bin/env python3
"""Shim: the canonical LAN scanner lives in the micropy-system framework
(micropy-system/tools/discover.py). Supports [pos_port], --host, --subnet,
--marker; the default marker subsumes this project's former 3-string match."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["MICROPY_APP_ROOT"] = ROOT
os.execv(
    sys.executable,
    [sys.executable,
     os.path.join(ROOT, "micropy-system", "tools", "discover.py")] + sys.argv[1:],
)
