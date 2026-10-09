"""Host tests for the app shell-command adapters (app/shell_commands.py).

Focus: the ``sensors`` command (P4 surface) -- freshness and age are
recomputed AT QUERY TIME from the sources' own stamps (not the per-tick
state.data_state snapshot), and the last-known readings are shown even
past expiry (that is the whole point of keeping the cache).
"""

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from ccu3 import _now_ms  # noqa: E402
from shell_commands import SENSORS_USAGE, make_sensors_handler  # noqa: E402


class Cfg(dict):
    def get(self, key):
        return self[key]


class Src:
    """Weather source stand-in with the (t_out, lux, ts) last() shape."""

    def __init__(self, values):
        self._values = values

    def last(self):
        return self._values


class NoLastSrc:
    """NullSensorSource shape: no last() at all (hasattr guard path)."""


class Rooms:
    poll_s = 120

    def __init__(self, agg):
        self._agg = agg

    def last(self):
        return self._agg


def query(sensor_values, agg, cache_s=14400, poll_s=60):
    cfg = Cfg(sensor_cache_s=cache_s, ccu3_poll_s=poll_s)
    handle = make_sensors_handler(Src(sensor_values), Rooms(agg), cfg)
    return json.loads(handle())


class SensorsCommandTests(unittest.TestCase):
    def test_fresh_shows_tiers_values_and_small_ages(self):
        now = _now_ms()
        out = query((8.5, 12000.0, now), {"demand_pct": 4.2, "ts": now})
        self.assertEqual(out["weather"]["tier"], "fresh")
        self.assertEqual(out["weather"]["t_out"], 8.5)
        self.assertEqual(out["weather"]["lux"], 12000.0)
        self.assertLessEqual(out["weather"]["age_s"], 1)
        self.assertEqual(out["demand"]["tier"], "fresh")
        self.assertEqual(out["demand"]["demand_pct"], 4.2)
        self.assertEqual(out["cache_s"], 14400)

    def test_age_is_live_not_the_tick_snapshot(self):
        # A stamp 300 s old must report ~300 s of age at query time,
        # regardless of when the last control tick computed data_state.
        old = _now_ms() - 300_000
        out = query((8.5, 12000.0, old), {"demand_pct": 4.2, "ts": old})
        self.assertEqual(out["weather"]["tier"], "cached")
        self.assertTrue(299 <= out["weather"]["age_s"] <= 301)
        self.assertTrue(299 <= out["demand"]["age_s"] <= 301)

    def test_expired_still_shows_last_known_values(self):
        # P4: the cache is kept past expiry precisely so this command
        # (and the UI) can show WHAT the loop last saw.
        expired = _now_ms() - (14400 + 600) * 1000
        out = query((8.5, 12000.0, expired),
                    {"demand_pct": 4.2, "ts": expired})
        self.assertEqual(out["weather"]["tier"], "expired")
        self.assertEqual(out["weather"]["t_out"], 8.5)
        self.assertEqual(out["demand"]["tier"], "expired")
        self.assertEqual(out["demand"]["demand_pct"], 4.2)

    def test_no_sources_report_none_without_crashing(self):
        cfg = Cfg(sensor_cache_s=14400, ccu3_poll_s=60)
        handle = make_sensors_handler(NoLastSrc(), Rooms(None), cfg)
        out = json.loads(handle())
        self.assertEqual(out["weather"]["tier"], "none")
        self.assertIsNone(out["weather"]["t_out"])
        self.assertEqual(out["demand"]["tier"], "none")
        self.assertIsNone(out["demand"]["demand_pct"])

    def test_extra_args_return_usage(self):
        cfg = Cfg(sensor_cache_s=14400, ccu3_poll_s=60)
        handle = make_sensors_handler(Src(None), Rooms(None), cfg)
        self.assertEqual(handle("whatever"), SENSORS_USAGE)


if __name__ == "__main__":
    unittest.main()
