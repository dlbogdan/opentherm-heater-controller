"""Live pin: the shell ``selftest`` must not grow sys.path (PLAN.md P5).

``_run_selftest`` puts the ACTIVE slot first so the running firmware's
selftest module wins (and covers the raw-REPL path where no slot is on
sys.path yet). The old code inserted two entries per call and never removed
them, so every ``selftest`` invocation leaked two sys.path entries -- a slow
leak plus import-order noise on a long-lived board.

This runs the REAL control-core selftest (pure logic, no I/O, no actuation)
and asserts sys.path is unchanged afterwards. Read-only against the live app.
"""

import sys
import unittest

import app_entry  # the LIVE app module (already imported in the loop)


class TestSelftestPathStability(unittest.TestCase):
    timeout_s = 30  # the core selftest is fast, but bound it like every suite

    def test_run_selftest_leaves_sys_path_unchanged(self):
        before = list(sys.path)
        result = app_entry._run_selftest()
        first = result.splitlines()[0] if result else "(no result)"
        self.assertTrue(result.startswith("PASS"), first)
        self.assertEqual(before, list(sys.path),
                         "selftest must remove the sys.path entries it adds")
