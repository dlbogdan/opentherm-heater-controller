"""Host tests for the ``rooms`` shell command (app/shell_commands.py).

Resource grammar (mirrors ``config``): bare = room names, ``all`` = every
card, ``<id|name>`` = one card, ``get/set <id|name>.<field>`` = one field
(writable: weight), ``forget`` = prune. A fake collector stands in for
HeatingGroups so the handler logic (quote-aware refs, last-dot field
split, error hints) runs on a host without any CCU3 I/O.
"""

import json
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from shell_commands import make_rooms_handler  # noqa: E402


class FakeGroups:
    def __init__(self):
        self.set_calls = []
        self.forget_calls = []
        self._cards = {
            "2": {"room_id": "2", "name": "Room Small", "kind": "etrv",
                  "weight": 0.4, "sensors_alive": True, "last_seen_ms": 1,
                  "setpoint": 21.0, "actual": 20.0, "demand_delta": 1.0},
            "7": {"room_id": "7", "name": "Room Large", "kind": "group",
                  "weight": 1.0, "sensors_alive": False, "last_seen_ms": 2,
                  "setpoint": None, "actual": None, "demand_delta": None},
        }

    def weights(self):
        return [{"room_id": rid, "name": c["name"], "kind": c["kind"],
                 "weight": c["weight"], "sensors_alive": c["sensors_alive"],
                 "last_seen_ms": c["last_seen_ms"]}
                for rid, c in self._cards.items()]

    def card(self, ref):
        for rid, c in self._cards.items():
            if rid == ref or c["name"].lower() == str(ref).lower():
                return dict(c), None
        return None, "no such room: %s" % ref

    def cards(self):
        return [dict(c) for c in self._cards.values()]

    def set_weight(self, ref, value):
        self.set_calls.append((ref, value))
        card, err = self.card(ref)
        if err:
            return False, err
        if not 0.0 <= float(value) <= 1.0:
            return False, "weight must be in 0..1"
        return True, "weight %s (%s) = %s" % (card["name"], card["room_id"],
                                              value)

    def forget(self, ref):
        self.forget_calls.append(ref)
        if ref == "nope":
            return False, "no such room: nope"
        return True, "removed Room Small (2) from the weights registry"


class RoomsShellTests(unittest.TestCase):
    def setUp(self):
        self.groups = FakeGroups()
        self.handle = make_rooms_handler(self.groups)

    def test_bare_lists_room_names(self):
        out = json.loads(self.handle(""))
        self.assertEqual([r["name"] for r in out],
                         ["Room Small", "Room Large"])
        self.assertEqual(set(out[0]), {"id", "name", "kind"})

    def test_all_returns_every_card(self):
        out = json.loads(self.handle("all"))
        self.assertEqual(len(out), 2)
        self.assertIn("demand_delta", out[0])

    def test_one_card_by_id(self):
        out = json.loads(self.handle("2"))
        self.assertEqual(out["name"], "Room Small")
        self.assertEqual(out["weight"], 0.4)

    def test_one_card_by_quoted_name(self):
        out = json.loads(self.handle('"Room Small"'))
        self.assertEqual(out["room_id"], "2")

    def test_one_card_by_unquoted_spaced_name(self):
        out = json.loads(self.handle("Room Small"))
        self.assertEqual(out["room_id"], "2")

    def test_get_field(self):
        self.assertEqual(json.loads(self.handle("get 2.weight")),
                         {"Room Small.weight": 0.4})

    def test_get_quoted_spaced_name(self):
        self.assertEqual(json.loads(self.handle('get "Room Small".WEIGHT')),
                         {"Room Small.weight": 0.4})  # field case-insensitive

    def test_get_unknown_field_lists_fields(self):
        out = self.handle("get 2.bogus")
        self.assertTrue(out.startswith("unknown field: bogus"))
        self.assertIn("demand_delta", out)

    def test_get_unknown_room_points_at_bare(self):
        self.assertEqual(self.handle("get nope.weight"),
                         "no such room: nope (try: rooms)")

    def test_get_without_field_is_usage(self):
        self.assertTrue(self.handle("get 2").startswith("usage: rooms get"))

    def test_set_weight_by_id(self):
        out = self.handle("set 2.weight 0.6")
        self.assertEqual(self.groups.set_calls, [("2", "0.6")])
        self.assertTrue(out.startswith("OK: weight Room Small"))

    def test_set_weight_quoted_spaced_name(self):
        self.handle('set "Room Small".weight 0.6')
        self.assertEqual(self.groups.set_calls, [("Room Small", "0.6")])

    def test_set_read_only_field(self):
        self.assertTrue(self.handle("set 2.actual 30").startswith("read-only"))

    def test_set_unknown_room_gets_the_hint(self):
        self.assertEqual(self.handle("set nope.weight 0.4"),
                         "no such room: nope (try: rooms)")

    def test_set_bad_value_is_prefixed(self):
        self.assertTrue(self.handle("set 2.weight 1.7")
                        .startswith("set failed:"))

    def test_old_weight_verb_teaches_new_grammar(self):
        out = self.handle("weight 2 0.4")
        self.assertTrue(out.startswith("unknown verb: weight"))
        self.assertIn("rooms set <id|name>.weight", out)

    def test_forget_prunes(self):
        out = self.handle("forget Room Small")
        self.assertEqual(self.groups.forget_calls, ["Room Small"])
        self.assertIn("removed", out)

    def test_forget_failure_is_prefixed(self):
        self.assertTrue(self.handle("forget nope").startswith("forget failed:"))

    def test_unknown_room_is_not_usage_but_hint(self):
        self.assertEqual(self.handle("bogus"),
                         "no such room: bogus (try: rooms)")


if __name__ == "__main__":
    unittest.main()
