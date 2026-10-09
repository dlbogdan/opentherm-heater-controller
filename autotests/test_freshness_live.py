"""Live suite: the P4 freshness path in the DEGRADED states (firmware 1.1.85+).

Host pins cover the pure tier logic; this suite proves what only the board
can prove: the .mpy-compiled helpers on OLD stamps, the main.py glue
(state.data_state recomputed from patched sources, the edge-triggered WARN
landing in /log.txt EXACTLY once per transition -- the flash-write storm
this policy exists to avoid), and the failsafe actuation landing on the
human's manual_setpoint when weather goes expired.

Method (the proven P3 pattern): patch the LIVE singletons via
app_entry.LIVE (instance attrs shadow class methods), wait for the next
control tick to run the patched state, assert through the app's OWN
state.data_state + the flash log, then restore with ``del`` (NEVER rebind
the class function -- see MICROPYTHON-GOTCHAS §1) and pin the call shape.

Costs (deliberate): one WARN line per demo (that IS the evidence) and a
few ticks steering the DUMMY transport at the failsafe target (no real
hardware behind board writes; HA still drives the boiler). tearDown
ALWAYS restores (the runner runs it even on timeout).

Class order is alphabetical on purpose: the patch classes run before
TestZLiveGlueRestored, which proves the loop came back healthy.
"""

import unittest

import app_entry

import uasyncio as asyncio

from ccu3 import _now_ms
from config import config
from state import state

LOG_FILE = "/log.txt"
WEATHER_MARK = b"too stale"
DEMAND_MARK = b"Rooms: demand expired"


def _log_count(marker, keep=6000):
    """Occurrences of ``marker`` in the last ``keep`` log bytes (O(1) RAM)."""
    tail = b""
    try:
        with open(LOG_FILE, "rb") as handle:
            while True:
                chunk = handle.read(512)
                if not chunk:
                    break
                tail = (tail + chunk)[-keep:]
    except OSError:
        pass
    return tail.count(marker)


async def _wait_data_state(key, tier, timeout_s=75):
    """Bounded wait for the NEXT control tick to record ``tier`` for ``key``."""
    waited_ms = 0
    while waited_ms < timeout_s * 1000:
        data = state.data_state
        if data is not None and data.get(key) == tier:
            return True
        await asyncio.sleep(1.0)
        waited_ms += 1000
    return False


class TestFreshnessTiersSynthetic(unittest.TestCase):
    """Pure tier logic INSIDE the device runtime (the .mpy-compiled code):
    old stamps, expiry boundary, and the ticks_ms wrap -- zero I/O, zero
    patching. Host-pinned semantics, executed where they actually run."""

    def test_synthetic_tiers_on_device(self):
        from rooms import pick_demand
        from sensors import TIER_CACHED, TIER_EXPIRED, TIER_FRESH, weather_tier
        now = _now_ms()
        poll_s = int(config.get("rooms_poll_s"))
        cache_s = int(config.get("sensor_cache_s"))

        # 1 h old: cached, value still steers.
        value, tier, age = pick_demand(
            {"demand_pct": 4.2, "ts": now - 3600 * 1000}, now, poll_s, cache_s)
        self.assertEqual((value, tier), (4.2, TIER_CACHED))
        self.assertEqual(age, 3600)

        # Past the shared window: no value (the defined no-sensor input).
        value, tier, _age = pick_demand(
            {"demand_pct": 4.2, "ts": now - (cache_s + 60) * 1000},
            now, poll_s, cache_s)
        self.assertIsNone(value)
        self.assertEqual(tier, TIER_EXPIRED)

        # Fresh window boundary: exactly 2 x poll is still fresh.
        _v, tier, _a = pick_demand(
            {"demand_pct": 1.0, "ts": now - 2 * poll_s * 1000},
            now, poll_s, cache_s)
        self.assertEqual(tier, TIER_FRESH)

        # Wrap: stamp just below 2**32, now just above -> true age 15 s.
        wrap = 2 ** 32
        _v, tier, age = pick_demand(
            {"demand_pct": 9.9, "ts": wrap - 10000}, 5000, poll_s, cache_s)
        self.assertEqual((tier, age), (TIER_FRESH, 15))

        # Weather mirrors the same policy.
        tier, age = weather_tier((8.0, 900.0, now - 700 * 1000), now,
                                 config.get("ccu3_poll_s"), cache_s)
        self.assertEqual((tier, age), (TIER_CACHED, 700))
        tier, _age = weather_tier((8.0, 900.0, now - (cache_s + 1) * 1000),
                                  now, config.get("ccu3_poll_s"), cache_s)
        self.assertEqual(tier, TIER_EXPIRED)


class TestWeatherExpiryLive(unittest.TestCase):
    """Force the LIVE weather source expired (old stamp + failing poll) and
    prove the whole degraded chain through the app's own tick: read() keeps
    the cache but reports nothing, data_state says expired, the WARN lands
    EXACTLY once (edge-triggered), and the failsafe hold == manual_setpoint
    at the (dummy) boiler."""

    timeout_s = 150  # worst-case tick phase (control_tick_s=60) + margin

    def setUp(self):
        self.src = app_entry.LIVE.get("sensor_source")
        if self.src is None:
            raise unittest.SkipTest("no live sensor source wired")
        self.patched = False

    async def _noop_poll(self, _now):
        """The injected fault: polls stop refreshing; the old stamp ages."""

    async def test_weather_expiry_chain(self):
        cache_s = int(config.get("sensor_cache_s"))
        before = _log_count(WEATHER_MARK)

        # Expired stamp (window + 5 min) + dead poll: the next control tick
        # sees the source's OWN expiry logic report "no reading" while the
        # cache itself stays in RAM (P4: last-known data for sources/UI).
        self.src._values = (20.0, 50000.0, _now_ms() - (cache_s + 300) * 1000)
        self.src._poll = self._noop_poll
        self.patched = True

        self.assertTrue(
            await _wait_data_state("weather", "expired"),
            "data_state never went weather=expired within 75 s "
            "(control task dead or the expiry check broken?)")
        data = state.data_state
        self.assertGreater(data["weather_age_s"], cache_s)
        self.assertIsNotNone(self.src._values,
                             "expired cache was DROPPED -- P4 keeps it")

        # Failsafe actuation: no fresh t_out -> the human's flow target.
        tr = app_entry.LIVE.get("transport")
        if tr is not None and not state.candidate_boot and not state.post_failed:
            held = tr.read_setpoint()
            self.assertIsNotNone(held, "failsafe must hold a setpoint")
            target = float(config.get("manual_setpoint"))
            # NOTE: the on-device unittest shim has NO delta= kwarg on
            # assertAlmostEqual (host CPython does) -- explicit abs check.
            self.assertLess(abs(held - target), 0.01,
                            "failsafe hold %s != manual_setpoint %s"
                            % (held, target))

        await asyncio.sleep(0.3)  # let the WARN land in the file
        self.assertEqual(_log_count(WEATHER_MARK) - before, 1,
                         "stale WARN not exactly-once (edge trigger broken)")

    def _restore(self):
        # DELETE the instance attr so the CLASS method resolves again
        # (never rebind the unbound class function -- gotcha §1).
        try:
            del self.src._poll
        except AttributeError:
            pass

    def tearDown(self):
        if self.src is not None:
            self._restore()


class TestDemandExpiryLive(unittest.TestCase):
    """Force the LIVE rooms aggregate expired (patched last()) and prove
    the tick's demand glue: no-sensor input, data_state expired, and the
    edge WARN exactly once."""

    timeout_s = 150

    def setUp(self):
        self.rooms = app_entry.LIVE.get("rooms")
        if self.rooms is None:
            raise unittest.SkipTest("no live rooms handle wired")
        if not self.rooms.enabled:
            raise unittest.SkipTest("rooms source disabled")
        self.patched = False

    def _stale_last(self):
        cache_s = int(config.get("sensor_cache_s"))
        return {"demand_pct": 7.5, "total": 7,
                "ts": _now_ms() - (cache_s + 300) * 1000}

    async def test_demand_expiry_chain(self):
        cache_s = int(config.get("sensor_cache_s"))
        before = _log_count(DEMAND_MARK)

        # Instance attr shadows the method: every consumer (the control
        # tick above all) now sees an aggregate past the shared window.
        self.rooms.last = self._stale_last
        self.patched = True

        self.assertTrue(
            await _wait_data_state("demand", "expired"),
            "data_state never went demand=expired within 75 s")
        data = state.data_state
        self.assertGreater(data["demand_age_s"], cache_s)

        await asyncio.sleep(0.3)
        self.assertEqual(_log_count(DEMAND_MARK) - before, 1,
                         "demand-expired WARN not exactly-once "
                         "(edge trigger broken -- flash storm risk)")

    def _restore(self):
        try:
            del self.rooms.last
        except AttributeError:
            pass

    def tearDown(self):
        if self.rooms is not None:
            self._restore()


class TestZLiveGlueRestored(unittest.TestCase):
    """Runs LAST (alphabetical): both patches restored, the loop is healthy
    again -- pinned through the loop's OWN call shapes, not identity."""

    timeout_s = 150  # wait for one clean tick after the patches came off

    async def test_sources_back_to_fresh(self):
        self.assertTrue(
            await _wait_data_state("weather", "fresh"),
            "weather never recovered to fresh after the patch")
        self.assertTrue(
            await _wait_data_state("demand", "fresh"),
            "demand never recovered to fresh after the patch")

    async def test_live_call_shapes_intact(self):
        src = app_entry.LIVE.get("sensor_source")
        rooms = app_entry.LIVE.get("rooms")
        if src is None or rooms is None:
            raise unittest.SkipTest("no LIVE handles")
        result = await src.read()  # the control loop's exact call shape
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3,
                         "live read() wrong shape -- patch leaked")
        agg = rooms.last()  # the real method again: fresh real aggregate
        self.assertIsNotNone(agg)
        self.assertNotEqual(agg.get("demand_pct"), 7.5,
                            "still serving the synthetic stale aggregate")
