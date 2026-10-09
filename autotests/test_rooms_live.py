"""Live room-model suite: the demand signal the control loop consumes.

The highest-value on-device checks (pushed to /autotests): the aggregate
production feeds into the demand P-term has never been validated against
the REAL CCU3 -- host tests use a fake. Read-only by contract: the app's
HeatingGroups only calls Interface.getValue (plus discovery reads on a
cache miss); this suite adds no writes. NEVER extend it with setValue --
the CCU3 is production.

Design decisions learned live:
* ALL CCU3 I/O runs on the ONE pooled session (ccu3_live_session): the
  board's app already holds ~2 sessions and the CCU3 pool is small with
  minutes-long lingering.
* The independent pass borrows the pooled rpc on a suite-LOCAL
  HeatingGroups (one private attribute swap): everything above the
  transport stays production code; the app's live object is observed,
  never driven (no concurrent reads, no shared mutable state).
* The first rooms pass lands at Wi-Fi-up + ~50 s after a reset (the app's
  task waits for the network, then ~14 RPCs at ~2.8 s each); awaited
  setUp waits (bounded) for the app's first aggregate instead of failing
  on that documented boot race. Worst-case budgets sit under timeout_s.
"""

import unittest

import uasyncio as asyncio

from ccu3 import _now_ms
from config import config

import ccu3_live_session as session


def _age_ms(ts):
    """Elapsed ms since a ticks_ms stamp, wrap-safe where available."""
    try:
        import time
        if hasattr(time, "ticks_diff"):
            return time.ticks_diff(_now_ms(), ts)
    except (ImportError, OSError):
        pass
    return _now_ms() - ts


async def _wait_first_pass(rooms, timeout_s=140):
    """Bounded wait for the app's periodic task to produce its first
    aggregate (expected transient right after a reset, not a fault)."""
    deadline = _now_ms() + timeout_s * 1000
    while _now_ms() < deadline:
        if rooms.last() is not None:
            return True
        await asyncio.sleep(2.0)
    return False


def _live_rooms():
    import app_entry  # the LIVE app module (already imported in the loop)
    live = getattr(app_entry, "LIVE", None)
    return live.get("rooms") if live else None


class TestRoomsAggregate(unittest.TestCase):
    """The app's OWN cached aggregate: shape, consistency, freshness."""

    timeout_s = 250  # >= wait_wifi(90) + wait_first_pass(140) + slack

    async def setUp(self):
        if not config.get("ccu3_url"):
            self.skipTest("no ccu3_url configured")
        self.rooms = _live_rooms()
        if self.rooms is None:
            self.skipTest("no LIVE rooms handle (firmware too old?)")
        if not self.rooms.enabled:
            self.skipTest("rooms source disabled")
        if not await session.wait_wifi():
            self.skipTest("Wi-Fi did not come up")
        if not await _wait_first_pass(self.rooms):
            self.skipTest("first rooms pass did not land within the window")

    def test_shape_and_ranges(self):
        agg = self.rooms.last()
        for key in ("total", "reading", "thermostats", "demanding_rooms",
                    "avg_delta", "demand_pct", "max_delta", "ts"):
            self.assertTrue(key in agg, "missing field: %s" % key)
        self.assertGreater(agg["total"], 0)
        self.assertLessEqual(agg["reading"], agg["total"])
        self.assertLessEqual(agg["demanding_rooms"], agg["thermostats"])
        self.assertLessEqual(agg["thermostats"], agg["total"])
        if agg["demand_pct"] is not None:
            self.assertTrue(0.0 <= agg["demand_pct"] <= 100.0)
        if agg["max_delta"] is not None:
            self.assertGreater(agg["max_delta"], 0.0)

    def test_recipe_is_self_consistent(self):
        # demand_pct MUST be the clamp of avg_delta / cap (AGENTS.md ReGaHd
        # recipe) -- catches fold/aggregation drift inside the live object.
        agg = self.rooms.last()
        if agg["demand_pct"] is None:
            self.assertIsNone(agg["avg_delta"])
            return
        cap = float(config.get("demand_delta_cap") or 3.0)
        self.assertAlmostEqual(agg["demand_pct"],
                               _clamp(agg["avg_delta"] / cap * 100.0),
                               places=4)

    def test_values_are_physics_sane(self):
        agg = self.rooms.last()
        if agg["avg_setpoint"] is not None:
            self.assertTrue(4.0 <= agg["avg_setpoint"] <= 31.0)
        if agg["avg_actual"] is not None:
            self.assertTrue(5.0 <= agg["avg_actual"] <= 40.0)
        if agg["max_delta"] is not None:
            self.assertLess(agg["max_delta"], 20.0)  # a >20 degC gap is a bug

    def test_aggregate_is_fresh(self):
        # The periodic task must actually be running: an aggregate older
        # than the cadence + pass time means the rooms task died (or CCU3
        # has been unreachable across an interval -- either way the control
        # loop is steering on stale demand).
        agg = self.rooms.last()
        poll_s = int(config.get("rooms_poll_s") or 300)
        self.assertLessEqual(_age_ms(agg["ts"]), (poll_s + 180) * 1000)

    def test_pick_demand_live_selection(self):
        # P4: the EXACT selection main.py makes, against the LIVE
        # aggregate: a healthy periodic task must resolve fresh (cached
        # tolerated for a slow pass), keep the value steering, and the
        # shared sensor_cache_s key must exist in the live config.
        from rooms import pick_demand
        from sensors import TIER_CACHED, TIER_FRESH
        agg = self.rooms.last()
        poll_s = int(config.get("rooms_poll_s") or 300)
        cache_s = int(config.get("sensor_cache_s"))
        self.assertGreaterEqual(cache_s, 2 * poll_s)
        value, tier, age_s = pick_demand(agg, _now_ms(), poll_s, cache_s)
        self.assertIn(tier, (TIER_FRESH, TIER_CACHED))
        self.assertEqual(value, agg["demand_pct"])
        self.assertGreaterEqual(age_s, 0)


class TestRoomsIndependentPass(unittest.TestCase):
    """Recompute the pass independently, cross-check the production cache.

    One full pass (~14 getValue RPCs, ~45 s on the Pico) with a suite-local
    HeatingGroups on the pooled session, compared against the app's cached
    aggregate: identity and setpoints must match (only humans change
    those), actuals/demand only within drift bounds (they legitimately
    move between the two reads).
    """

    timeout_s = 480  # >= wait_wifi(90) + wait_first_pass(140) + pass(~240)

    async def setUp(self):
        if not config.get("ccu3_url"):
            self.skipTest("no ccu3_url configured")
        self.live_rooms = _live_rooms()
        if self.live_rooms is None:
            self.skipTest("no LIVE rooms handle (firmware too old?)")
        if not self.live_rooms.enabled:
            self.skipTest("rooms source disabled")
        if not await session.wait_wifi():
            self.skipTest("Wi-Fi did not come up")
        if not await _wait_first_pass(self.live_rooms):
            self.skipTest("first rooms pass did not land within the window")

    async def test_independent_pass_matches_production_cache(self):
        from heating_groups import HeatingGroups
        cached = self.live_rooms.last()
        mine = HeatingGroups(config)     # fresh fold; identity from the
        mine._rpc = await session.get_rpc()  # app's cache file, pooled session
        started = _now_ms()
        agg = await mine.read()
        # A cache hit keeps the pass at ~2 RPCs/room; a silent discovery
        # storm (cache invalidation regression) is caught by this budget.
        self.assertLess(_age_ms(started), 240 * 1000,
                        "pass too slow -- discovery storm (cache miss)?")

        self.assertEqual(agg["total"], cached["total"],
                         "room identity drifted between instances")
        self.assertEqual(agg["thermostats"], cached["thermostats"])
        if agg["avg_setpoint"] is not None and cached["avg_setpoint"] is not None:
            self.assertLess(abs(agg["avg_setpoint"] - cached["avg_setpoint"]),
                            0.05)  # setpoints change only by human action
        if agg["avg_actual"] is not None and cached["avg_actual"] is not None:
            self.assertLess(abs(agg["avg_actual"] - cached["avg_actual"]),
                            1.5)   # actuals move slowly over minutes
        if agg["demand_pct"] is not None and cached["demand_pct"] is not None:
            # +/-10 absorbs real room drift between reads while still
            # catching a broken fold (a wrong denominator moves demand more)
            self.assertLess(abs(agg["demand_pct"] - cached["demand_pct"]),
                            10.0)
            self.assertAlmostEqual(agg["demand_pct"],
                                   _clamp(agg["avg_delta"]
                                          / float(config.get("demand_delta_cap")
                                                  or 3.0) * 100.0),
                                   places=4)


def _clamp(value):
    return 100.0 if value > 100.0 else (0.0 if value < 0.0 else value)
