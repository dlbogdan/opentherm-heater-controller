#!/usr/bin/env python3
"""Shim: the canonical assembler lives in the micropy-system framework
(micropy-system/tools/assemble.py). The project tools/ directory is kept as
thin execv shims so existing CLIs and docs keep working; the framework tools
are updated via tools/update_framework.sh."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["MICROPY_APP_ROOT"] = ROOT
os.execv(
    sys.executable,
    [sys.executable,
     os.path.join(ROOT, "micropy-system", "tools", "assemble.py"),
     "--app-root", ROOT] + sys.argv[1:],
)
