"""Live pin: the P6a weights registry + weighted demand on the running app.

Read-only against ``app_entry.LIVE["rooms"]`` (the live collector): the
registry API exists and is populated by discovery, the aggregate carries the
weighted denominator, and at default weights that denominator equals the plain
thermostat count (parity). No config writes, no mode change, no actuation.
"""

import unittest

import app_entry  # the LIVE app module (already imported in the loop)


class TestRoomsWeightsLive(unittest.TestCase):
    def _rooms(self):
        rooms = app_entry.LIVE.get("rooms")
        if rooms is None or not hasattr(rooms, "weights"):
            self.skipTest("live rooms collector missing/old")
        return rooms

    def test_registry_api_and_snapshot_shape(self):
        snap = self._rooms().weights()
        self.assertIsInstance(snap, list)
        for entry in snap:
            for key in ("room_id", "name", "kind", "weight", "sensors_alive"):
                self.assertIn(key, entry)

    def test_weighted_field_and_parity_at_default_weights(self):
        agg = self._rooms().last()
        if agg is None:
            self.skipTest("no rooms aggregate yet")
        self.assertIn("weighted_thermostats", agg)
        # Default weights are all 1.0 -> the weighted denominator equals the
        # plain thermostat count (behaviour unchanged until a weight is set).
        self.assertAlmostEqual(agg["weighted_thermostats"], agg["thermostats"])

    def test_room_cards_merge_registry_and_readings(self):
        rooms = self._rooms()
        if not hasattr(rooms, "cards"):
            self.skipTest("live rooms collector predates room cards")
        cards = rooms.cards()
        self.assertEqual(len(cards), len(rooms.weights()))
        for card in cards:
            for key in ("room_id", "name", "kind", "weight", "sensors_alive",
                        "setpoint", "actual", "demand_delta"):
                self.assertIn(key, card)
        agg = rooms.last()
        rd = (agg or {}).get("room_data") or {}
        if rd:
            # Only rooms PRESENT in this pass carry readings (a
            # mode-excluded/dead room has a card but no room_data entry).
            rid = sorted(rd)[0]
            card, err = rooms.card(rid)
            self.assertIsNone(err)
            self.assertEqual(card["setpoint"], rd[rid]["sp"])
            self.assertEqual(card["actual"], rd[rid]["act"])
