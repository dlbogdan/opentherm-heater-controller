#!/usr/bin/env python3
"""Shim: the canonical network REPL client lives in the micropy-system
framework (micropy-system/tools/console.py). See that file for the protocol."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["MICROPY_APP_ROOT"] = ROOT
os.execv(
    sys.executable,
    [sys.executable,
     os.path.join(ROOT, "micropy-system", "tools", "console.py")] + sys.argv[1:],
)
