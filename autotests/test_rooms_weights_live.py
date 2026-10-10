"""Live pin: the P6a weights registry + weighted demand on the running app.

Read-only against ``app_entry.LIVE["rooms"]`` (the live collector): the
registry API exists and is populated by discovery, the aggregate carries the
weighted denominator, and at default weights that denominator equals the plain
thermostat count (parity). No config writes, no mode change, no actuation.

The Bug 2 class below is the exception: it exercises the REAL forget/remember
API on the live registry and restores the pre-test state in tearDown.
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
            for key in ("room_id", "name", "kind", "weight", "sensors_alive",
                        "forgotten"):
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


class TestRoomsForgetLive(unittest.TestCase):
    """Bug 2 live proof: ``forget`` TOMBSTONES a room -- it leaves the very
    next pass (no RPC, out of demand, no card readings) and ``remember``
    restores it at the preserved weight.

    Method: call the real public API on the LIVE collector, then run one
    direct ``await rooms.read()`` (the periodic task may overlap -- value
    reads are independent RPCs, the last writer wins on the cached
    aggregate). Deliberate costs (bracketed by the autotest markers): two
    extra rooms passes and a few registry flash writes. tearDown ALWAYS
    restores the pre-test flag (the runner runs it even on timeout).
    """

    timeout_s = 480  # two full passes at ~2.8 s/RPC x ~14 RPCs + margin

    def setUp(self):
        rooms = app_entry.LIVE.get("rooms")
        if rooms is None or not hasattr(rooms, "remember"):
            self.skipTest("live rooms collector predates forget tombstones")
        agg = rooms.last()
        if not agg or not (agg.get("room_data") or {}):
            self.skipTest("no rooms aggregate with readings yet")
        self.rooms = rooms
        self.rid = None
        for entry in rooms.weights():
            if entry["room_id"] in agg["room_data"] \
                    and not entry.get("forgotten"):
                self.rid = entry["room_id"]
                break
        if self.rid is None:
            self.skipTest("no present, unforgotten room to test with")
        self._was_forgotten = bool(
            rooms._registry[self.rid].get("forgotten"))

    def tearDown(self):
        entry = self.rooms._registry.get(self.rid)
        if entry is not None:
            entry["forgotten"] = self._was_forgotten
            self.rooms._save_registry()

    async def test_forget_excludes_then_remember_restores(self):
        rooms = self.rooms
        weight = rooms._weight_for(self.rid)
        ok, msg = rooms.forget(self.rid)
        self.assertTrue(ok, msg)
        self.assertTrue(rooms._is_forgotten(self.rid))
        base = rooms.last()
        agg = await rooms.read()  # one real pass with the tombstone in place
        self.assertNotIn(self.rid, agg["room_data"])
        if base is not None and base.get("weighted_thermostats") is not None \
                and agg.get("weighted_thermostats") is not None:
            # The denominator drops by exactly this room's weight (only
            # comparable when no other room changed state between passes).
            if self.rid in (base.get("room_data") or {}):
                # The on-device unittest shim has NO delta= kwarg (host
                # CPython does) -- explicit abs check (same pattern as
                # test_freshness_live).
                drop = (base["weighted_thermostats"]
                        - agg["weighted_thermostats"])
                self.assertLess(abs(drop - weight), 0.01,
                                "denominator dropped %s != weight %s"
                                % (drop, weight))
        ok, msg = rooms.remember(self.rid)
        self.assertTrue(ok, msg)
        self.assertFalse(rooms._is_forgotten(self.rid))
        agg2 = await rooms.read()
        self.assertIn(self.rid, agg2["room_data"])
