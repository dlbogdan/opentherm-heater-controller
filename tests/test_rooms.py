"""Host tests for the rooms/demand source boundary (app/rooms.py).

``make_rooms_source`` is the factory that ``main.py`` uses instead of
hardwiring a backend: with a ``ccu3_url`` it returns the CCU3 heating-group
collector, without one it returns the null source (demand disabled). Both
implement the same ``RoomsSource`` interface (``enabled`` / ``poll_s`` /
``read()`` / ``last()``), so the device layer and the control loop never
need to know which one they hold.
"""

import asyncio
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from rooms import NullRoomsSource, make_rooms_source, pick_demand  # noqa: E402
from sensors import (TIER_CACHED, TIER_EXPIRED, TIER_FRESH,  # noqa: E402
                     TIER_NONE, weather_tier)


class Cfg(dict):
    """A minimal app-config stand-in (get() is all the factory uses)."""


def run(coro):
    return asyncio.run(coro)


class FactoryTests(unittest.TestCase):
    def test_ccu3_url_selects_the_ccu3_collector(self):
        src = make_rooms_source(Cfg(ccu3_url="http://ccu:80/api/homematic.cgi",
                                    rooms_poll_s=300))
        self.assertTrue(src.enabled)
        self.assertEqual(src.poll_s, 300)
        # The CCU3 collector is duck-typed (like Ccu3SensorSource), not a
        # RoomsSource subclass -- it just implements the same interface.
        self.assertEqual(type(src).__name__, "HeatingGroups")

    def test_no_ccu3_url_returns_null_source(self):
        src = make_rooms_source(Cfg())
        self.assertFalse(src.enabled)
        self.assertEqual(src.poll_s, 0)
        self.assertIsInstance(src, NullRoomsSource)
        self.assertIsNone(src.last())
        self.assertIsNone(run(src.read()))

    def test_both_implement_the_interface(self):
        for src in (make_rooms_source(Cfg(ccu3_url="http://x")),
                    make_rooms_source(Cfg())):
            self.assertIn("enabled", dir(src))
            self.assertIn("poll_s", dir(src))
            self.assertTrue(hasattr(src, "read"))
            self.assertTrue(hasattr(src, "last"))
            # last() is sync and returns either an aggregate dict or None.
            result = src.last()
            self.assertTrue(result is None or isinstance(result, dict))


class NullSourceTests(unittest.TestCase):
    def test_read_is_a_noop_and_last_is_none(self):
        src = NullRoomsSource()
        self.assertIsNone(run(src.read()))
        self.assertIsNone(src.last())
        self.assertFalse(src.enabled)


WRAP = 2 ** 32  # ticks_ms wrap (~49.7 days)


class PickDemandTests(unittest.TestCase):
    """P4: the whole demand selection (fresh / cached / expired)."""

    POLL_S = 120      # rooms_poll_s on the board
    CACHE_S = 14400   # sensor_cache_s default (4 h, shared by all sources)

    def agg(self, ts, demand_pct=4.2):
        return {"demand_pct": demand_pct, "ts": ts, "total": 7}

    def test_fresh_value_steers(self):
        value, tier, age = pick_demand(self.agg(1000), 1000 + 60_000,
                                       self.POLL_S, self.CACHE_S)
        self.assertEqual((value, tier), (4.2, TIER_FRESH))
        self.assertEqual(age, 60)

    def test_fresh_boundary_at_two_polls_is_fresh(self):
        # Pinned semantics: stale only strictly BEYOND 2 x poll_s
        # (mirrors the weather guard's strict >).
        value, tier, _age = pick_demand(self.agg(0),
                                        2 * self.POLL_S * 1000,
                                        self.POLL_S, self.CACHE_S)
        self.assertEqual((value, tier), (4.2, TIER_FRESH))

    def test_cached_value_still_steers_with_flag(self):
        # Owner decision 2026-10-09: inside sensor_cache_s the last-known
        # demand KEEPS steering (rooms move slowly) and the tier flags it.
        value, tier, _age = pick_demand(self.agg(0), 3_600_000,
                                        self.POLL_S, self.CACHE_S)
        self.assertEqual((value, tier), (4.2, TIER_CACHED))

    def test_cache_boundary_is_inclusive(self):
        value, tier, _age = pick_demand(self.agg(0), self.CACHE_S * 1000,
                                        self.POLL_S, self.CACHE_S)
        self.assertEqual((value, tier), (4.2, TIER_CACHED))

    def test_expired_drops_to_no_sensor(self):
        value, tier, age = pick_demand(self.agg(0),
                                       self.CACHE_S * 1000 + 1,
                                       self.POLL_S, self.CACHE_S)
        self.assertIsNone(value)  # the defined "no sensor" input
        self.assertEqual(tier, TIER_EXPIRED)
        self.assertEqual(age, self.CACHE_S)

    def test_wrap_across_ticks_rollover_is_fresh(self):
        # ts just before the 2**32 wrap, now just after: true age 15 s.
        value, tier, _age = pick_demand(self.agg(WRAP - 10_000), 5_000,
                                        self.POLL_S, self.CACHE_S)
        self.assertEqual((value, tier), (4.2, TIER_FRESH))

    def test_missing_ts_counts_expired_safe_direction(self):
        value, tier, _age = pick_demand({"demand_pct": 9.0}, 5000,
                                        self.POLL_S, self.CACHE_S)
        self.assertIsNone(value)
        self.assertEqual(tier, TIER_EXPIRED)

    def test_no_aggregate_is_none_tier(self):
        for agg in (None, {}):
            value, tier, _age = pick_demand(agg, 5000, self.POLL_S,
                                            self.CACHE_S)
            self.assertEqual((value, tier), (None, TIER_NONE))

    def test_fresh_aggregate_without_demand_value(self):
        # Empty pass (no thermostats read): value None, tier kept -- the
        # controller treats it as no-sensor either way.
        value, tier, _age = pick_demand(self.agg(0, demand_pct=None),
                                        60_000, self.POLL_S, self.CACHE_S)
        self.assertEqual((value, tier), (None, TIER_FRESH))

    def test_negative_elapsed_counts_fresh(self):
        # Clock anomaly (now < ts): mirrors the weather guard -- keep the
        # value rather than fail-spam.
        value, tier, _age = pick_demand(self.agg(10_000), 0,
                                        self.POLL_S, self.CACHE_S)
        self.assertEqual((value, tier), (4.2, TIER_FRESH))


class WeatherTierTests(unittest.TestCase):
    """P4: the weather tier derived from the source's cached stamp."""

    def test_tiers(self):
        self.assertEqual(weather_tier(None, 0, 60, 14400), (TIER_NONE, None))
        self.assertEqual(weather_tier((8.5, 100.0, None), 0, 60, 14400),
                         (TIER_NONE, None))
        fresh = weather_tier((8.5, 100.0, 0), 100_000, 60, 14400)
        self.assertEqual(fresh[0], TIER_FRESH)   # 100 s <= 2 x 60 s window
        cached = weather_tier((8.5, 100.0, 0), 700_000, 60, 14400)
        self.assertEqual(cached[0], TIER_CACHED)  # 700 s < 4 h
        expired = weather_tier((8.5, 100.0, 0), 14_401_000, 60, 14400)
        self.assertEqual(expired[0], TIER_EXPIRED)

    def test_wrap_across_rollover(self):
        tier, age = weather_tier((8.5, 100.0, WRAP - 10_000), 5_000,
                                 60, 14400)
        self.assertEqual((tier, age), (TIER_FRESH, 15))


if __name__ == "__main__":
    unittest.main()
