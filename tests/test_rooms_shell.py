"""Host tests for the ``rooms`` shell subcommands (app/shell_commands.py).

A fake collector stands in for HeatingGroups so the handler logic (bare
aggregate vs. weights | weight | forget dispatch, error prefixes, usage)
runs on a host without any CCU3 I/O.
"""

import json
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from shell_commands import ROOMS_USAGE, make_rooms_handler  # noqa: E402


class FakeGroups:
    def __init__(self):
        self.weights_calls = 0
        self.set_calls = []
        self.forget_calls = []

    def last(self):
        return {"demand_pct": 42.0, "weighted_thermostats": 3.0}

    def weights(self):
        self.weights_calls += 1
        return [{"room_id": "2", "name": "Room Small", "kind": "etrv",
                 "weight": 0.4, "sensors_alive": True, "last_seen_ms": 1}]

    def set_weight(self, ref, value):
        self.set_calls.append((ref, value))
        if ref == "nope":
            return False, "no such room: nope"
        return True, "weight Room Small (2) = %s" % value

    def forget(self, ref):
        self.forget_calls.append(ref)
        if ref == "nope":
            return False, "no such room: nope"
        return True, "removed Room Small (2) from the weights registry"


class RoomsShellTests(unittest.TestCase):
    def setUp(self):
        self.groups = FakeGroups()
        self.handle = make_rooms_handler(self.groups)

    def test_bare_returns_aggregate_json(self):
        self.assertEqual(json.loads(self.handle(""))["demand_pct"], 42.0)

    def test_weights_lists_registry(self):
        snap = json.loads(self.handle("weights"))
        self.assertEqual(self.groups.weights_calls, 1)
        self.assertEqual(snap[0]["room_id"], "2")
        self.assertEqual(snap[0]["weight"], 0.4)

    def test_weight_sets_and_reports(self):
        out = self.handle("weight 2 0.4")
        self.assertEqual(self.groups.set_calls, [("2", "0.4")])
        self.assertIn("= 0.4", out)

    def test_weight_accepts_a_name_with_spaces(self):
        out = self.handle("weight Room Small 0.4")
        self.assertEqual(self.groups.set_calls, [("Room Small", "0.4")])
        self.assertIn("= 0.4", out)

    def test_weight_failure_is_prefixed(self):
        self.assertTrue(self.handle("weight nope 0.4").startswith("set failed:"))

    def test_weight_wrong_arity_falls_back_to_usage(self):
        self.assertEqual(self.handle("weight 2"), ROOMS_USAGE)

    def test_forget_prunes(self):
        out = self.handle("forget Room Small")
        self.assertEqual(self.groups.forget_calls, ["Room Small"])
        self.assertIn("removed", out)

    def test_forget_failure_is_prefixed(self):
        self.assertTrue(self.handle("forget nope").startswith("forget failed:"))

    def test_unknown_subcommand_returns_usage(self):
        self.assertEqual(self.handle("bogus"), ROOMS_USAGE)


if __name__ == "__main__":
    unittest.main()
