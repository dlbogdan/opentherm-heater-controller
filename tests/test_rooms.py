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

from rooms import NullRoomsSource, make_rooms_source  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
