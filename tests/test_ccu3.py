"""Host tests for the CCU3 weather-station sensor source (app/ccu3.py).

A fake JSON-RPC endpoint stands in for the CCU3 so the whole protocol logic
(login, session expiry + retry, discovery, caching, staleness) runs on a host.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from ccu3 import Ccu3SensorSource  # noqa: E402
import ccu3  # noqa: E402  (module-level patch target for _now_ms)
from sensors import NullSensorSource, make_sensor_source  # noqa: E402


def run(coro):
    """Drive a coroutine to completion (host tests only)."""
    return asyncio.run(coro)


class FakeCcu3:
    """Simulates the CCU3 JSON-RPC endpoint (login / discovery / getValue)."""

    def __init__(self):
        self.devices = [
            {"id": "B1", "type": "HmIP-HEATCTRL2",
             "interface": "HmIPRf", "address": "AAA"},
            {"id": "B2", "type": "HmIP-SWO",
             "interface": "HmIPRf", "address": "BBB"},
        ]
        self.weather = {"ACTUAL_TEMPERATURE": "8.5", "ILLUMINATION": "12000"}
        self.session = "sid-1"
        self.sessions = ["sid-1"]
        self.methods = []
        self.fail_next_value = False

    async def __call__(self, url, body):
        request = json.loads(body.decode())
        method = request["method"]
        params = request["params"]
        self.methods.append(method)
        if method == "Session.login":
            self.session = "sid-%d" % (len(self.sessions) + 1)
            self.sessions.append(self.session)
            return ("HTTP/1.0 200 OK", {},
                    self._ok(request["id"], self.session))
        if params.get("_session_id_") != self.session:
            return ("HTTP/1.0 200 OK", {},
                    self._err(request["id"], -1, "not logged in"))
        if method == "Device.listAll":
            return ("HTTP/1.0 200 OK", {},
                    self._ok(request["id"], [d["id"] for d in self.devices]))
        if method == "Device.get":
            device = [d for d in self.devices if d["id"] == params["id"]][0]
            return ("HTTP/1.0 200 OK", {}, self._ok(request["id"], device))
        if method == "Interface.getValue":
            if self.fail_next_value:
                self.fail_next_value = False
                return ("HTTP/1.0 200 OK", {},
                        self._err(request["id"], -1, "session expired"))
            if not params["address"].startswith("BBB:"):
                return ("HTTP/1.0 200 OK", {},
                        self._err(request["id"], 1, "no such device"))
            value = self.weather.get(params["valueKey"], "0")
            return ("HTTP/1.0 200 OK", {},
                    self._ok(request["id"], {"value": value}))
        raise AssertionError("unexpected method " + method)

    @staticmethod
    def _ok(rpc_id, result):
        return json.dumps(
            {"result": result, "error": None, "id": rpc_id}).encode()

    @staticmethod
    def _err(rpc_id, code, message):
        return json.dumps(
            {"result": None, "error": {"code": code, "message": message},
             "id": rpc_id}).encode()


async def fail_post(url, body):
    raise OSError("connection refused")


class FakeConfig:
    def __init__(self, **overrides):
        self.values = {
            "ccu3_url": "http://127.0.0.1:1/api/",
            "ccu3_user": "admin",
            "ccu3_pass": "secret",
            "ccu3_weather_type": "HmIP-SWO",
            "ccu3_poll_s": 60,
            "sensor_cache_s": 14400,
            "t_out_source": "auto",
            "lux_mult": 1.0,
        }
        self.values.update(overrides)

    def get(self, key):
        return self.values[key]


class Ccu3SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = str(Path(self.tmp.name) / "ccu3_cache.json")

    def make(self, fake):
        return Ccu3SensorSource(
            FakeConfig(), cache_path=self.cache, http_post=fake,
            sleep=lambda _s: None)

    def test_discovery_picks_weather_station_and_reads_values(self):
        fake = FakeCcu3()
        source = self.make(fake)
        self.assertEqual(run(source.read()), (8.5, 12000.0, None))
        cache = json.loads(Path(self.cache).read_text())
        self.assertEqual(cache["interface"], "HmIPRf")
        self.assertEqual(cache["address"], "BBB")  # the HmIP-SWO, not the valve

    def test_cache_hit_skips_discovery(self):
        fake = FakeCcu3()
        run(self.make(fake).read())  # populates the cache
        fake2 = FakeCcu3()
        source = self.make(fake2)
        self.assertEqual(run(source.read()), (8.5, 12000.0, None))
        self.assertNotIn("Device.listAll", fake2.methods)
        self.assertNotIn("Device.get", fake2.methods)

    def test_session_expiry_relogs_in_and_retries(self):
        fake = FakeCcu3()
        fake.fail_next_value = True  # first getValue: session expired
        source = self.make(fake)
        self.assertEqual(run(source.read()), (8.5, 12000.0, None))
        self.assertEqual(fake.methods.count("Session.login"), 2,
                         "must login, expire, re-login")

    def test_poll_failure_with_no_value_reports_no_reading(self):
        source = self.make(fail_post)
        self.assertEqual(run(source.read()), (None, None, None))

    # -- lux_mult calibration (PLAN.md P1, blueprint parity) -----------------
    # The HA blueprint computes lux = round(lux_raw * lux_mult) BEFORE the
    # solar math (boiler_weather_compensation.yaml:369) and the board's
    # production calibration is lux_mult 1.4. The multiplier is applied at
    # the sensor-source layer (control/solar_accum.py docstring) -- these
    # pins are what keeps it from silently going dead again.

    def test_lux_mult_calibration_applied_at_the_source(self):
        fake = FakeCcu3()  # ILLUMINATION = 12000
        source = self.make_with(fake, lux_mult=1.4)
        _t, lux, _d = run(source.read())
        self.assertEqual(lux, 16800)  # round(12000 * 1.4)

    def test_lux_mult_default_is_transparent(self):
        source = self.make(FakeCcu3())  # FakeConfig default lux_mult 1.0
        _t, lux, _d = run(source.read())
        self.assertEqual(lux, 12000)

    def test_lux_mult_zero_means_no_solar_gain(self):
        source = self.make_with(FakeCcu3(), lux_mult=0)
        _t, lux, _d = run(source.read())
        self.assertEqual(lux, 0)  # 0 is a real calibration, not "missing"

    def test_missing_lux_stays_missing_under_any_mult(self):
        fake = FakeCcu3()
        fake.weather["ILLUMINATION"] = ""  # unparseable -> no value
        source = self.make_with(fake, lux_mult=1.4)
        _t, lux, _d = run(source.read())
        self.assertIsNone(lux)

    def make_with(self, fake, **overrides):
        return Ccu3SensorSource(
            FakeConfig(**overrides), cache_path=self.cache, http_post=fake,
            sleep=lambda _s: None)

    def test_recent_value_survives_poll_failure(self):
        import ccu3
        failing = Ccu3SensorSource(
            FakeConfig(), cache_path=self.cache, http_post=fail_post,
            sleep=lambda _s: None)
        failing._endpoint = ("HmIPRf", "BBB")
        failing._values = (8.5, 12000.0, ccu3._now_ms() - 10_000)  # 10 s old
        self.assertEqual(run(failing.read()), (8.5, 12000.0, None))

    def test_value_beyond_cache_window_reports_no_reading_but_keeps_cache(self):
        # P4: past sensor_cache_s the CONTROLLER sees the failsafe input,
        # but the last-known value is KEPT (for `sources`/UI display).
        import ccu3
        failing = Ccu3SensorSource(
            FakeConfig(sensor_cache_s=600), cache_path=self.cache,
            http_post=fail_post, sleep=lambda _s: None)
        failing._endpoint = ("HmIPRf", "BBB")
        failing._values = (8.5, 12000.0,
                           ccu3._now_ms() - 700_000)  # > 600 s cache
        self.assertEqual(run(failing.read()), (None, None, None))
        self.assertIsNotNone(failing._values)  # cache retained, not dropped

    def test_value_within_cache_window_still_steers(self):
        # P4 cached tier: polls failing, value inside sensor_cache_s ->
        # the last-known value KEEPS steering (owner decision 2026-10-09).
        import ccu3
        failing = Ccu3SensorSource(
            FakeConfig(), cache_path=self.cache, http_post=fail_post,
            sleep=lambda _s: None)
        failing._endpoint = ("HmIPRf", "BBB")
        failing._values = (8.5, 12000.0,
                           ccu3._now_ms() - 700_000)  # 700 s < 14400 s
        self.assertEqual(run(failing.read()), (8.5, 12000.0, None))

    def test_expired_warn_is_edge_triggered(self):
        # One flash WARN per transition into expired -- never one per tick
        # (logging policy: a per-tick WARN would be a flash-write storm).
        import ccu3
        warned = []
        failing = Ccu3SensorSource(
            FakeConfig(sensor_cache_s=600), cache_path=self.cache,
            http_post=fail_post, sleep=lambda _s: None,
            warn=lambda m: warned.append(m))
        failing._endpoint = ("HmIPRf", "BBB")
        failing._values = (8.5, 12000.0,
                           ccu3._now_ms() - 700_000)  # already expired
        for _ in range(5):  # five expired reads (each also logs a poll fail)
            self.assertEqual(run(failing.read()), (None, None, None))
        stale = [m for m in warned if "too stale" in m]
        self.assertEqual(len(stale), 1)

    # -- ticks_ms wrap (2**32 ms ~ 49.7 days uptime) --------------------------
    # The stamps below straddle the wrap: the value was recorded just BEFORE
    # ticks_ms rolls over, the read happens just AFTER it. Raw subtraction
    # would go hugely negative -- no poll, no staleness, a frozen reading for
    # a whole wrap cycle. _diff_ms must restore the true elapsed time.

    WRAP = 2 ** 32

    def patch_now(self, ms):
        import ccu3
        real = ccu3._now_ms
        ccu3._now_ms = lambda: ms
        self.addCleanup(setattr, ccu3, "_now_ms", real)

    def test_fresh_across_the_wrap_is_not_repolled(self):
        fake = FakeCcu3()
        source = self.make(fake)
        source._endpoint = ("HmIPRf", "BBB")
        source._values = (8.5, 12000.0, self.WRAP - 10_000)  # 10 s pre-wrap
        self.patch_now(5_000)  # 5 s post-wrap -> true age 15 s (< poll 60 s)
        self.assertEqual(run(source.read()), (8.5, 12000.0, None))
        self.assertNotIn("Interface.getValue", fake.methods)

    def test_poll_fires_across_the_wrap_when_interval_elapsed(self):
        fake = FakeCcu3()
        source = self.make(fake)
        source._endpoint = ("HmIPRf", "BBB")
        source._values = (1.0, 200.0, self.WRAP - 100_000)  # 100 s pre-wrap
        self.patch_now(5_000)  # true age 105 s > poll 60 s -> must re-poll
        self.assertEqual(run(source.read()), (8.5, 12000.0, None))
        self.assertIn("Interface.getValue", fake.methods)

    def test_stale_limit_triggers_across_the_wrap(self):
        failing = Ccu3SensorSource(
            FakeConfig(sensor_cache_s=600), cache_path=self.cache,
            http_post=fail_post, sleep=lambda _s: None)
        failing._endpoint = ("HmIPRf", "BBB")
        failing._values = (8.5, 12000.0, self.WRAP - 700_000)  # 700 s pre-wrap
        self.patch_now(5_000)  # true age 705 s > 600 s cache -> no reading
        self.assertEqual(run(failing.read()), (None, None, None))


class MakeSourceTests(unittest.TestCase):
    def test_auto_with_url_selects_ccu3(self):
        source = make_sensor_source(FakeConfig())
        self.assertIsInstance(source, Ccu3SensorSource)

    def test_auto_without_url_falls_back_to_null(self):
        source = make_sensor_source(FakeConfig(ccu3_url=""))
        self.assertIsInstance(source, NullSensorSource)

    def test_local_stays_null(self):
        source = make_sensor_source(FakeConfig(t_out_source="local"))
        self.assertIsInstance(source, NullSensorSource)


class FakeClock:
    """Controllable ``_now_ms`` substitute (module-patched in the tests below)."""

    def __init__(self, start_ms=1000000):
        self.now = start_ms

    def __call__(self):
        return self.now

    def advance_s(self, seconds):
        self.now += int(seconds * 1000)


async def _noop_sleep(_seconds):
    return None


class DiscoveryBackoffTests(unittest.TestCase):
    """P2: a failed discovery must back off instead of re-running the full
    scan on every read (the ~50-device storm that used to degrade the
    control tick to one tick per ~7.5 min)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = str(Path(self.tmp.name) / "ccu3_cache.json")
        self.clock = FakeClock()
        saved = ccu3._now_ms
        ccu3._now_ms = self.clock
        self.addCleanup(lambda: setattr(ccu3, "_now_ms", saved))

    def make(self, fake):
        return Ccu3SensorSource(
            FakeConfig(), cache_path=self.cache, http_post=fake,
            sleep=_noop_sleep)

    def _no_weather(self):
        fake = FakeCcu3()
        fake.devices = [d for d in fake.devices if d["type"] != "HmIP-SWO"]
        return fake

    def test_failed_discovery_backs_off_instead_of_re_scanning(self):
        fake = self._no_weather()
        source = self.make(fake)
        run(source.read())  # first read: full 3-attempt scan, then fails
        self.assertEqual(fake.methods.count("Device.listAll"), 3)
        self.assertEqual(run(source.read()), (None, None, None))
        # Inside the backoff window: no scan at all, immediate no-reading.
        self.assertIsNone(source._endpoint)
        run(source.read())
        self.assertEqual(fake.methods.count("Device.listAll"), 3)
        # After the window elapses: exactly one more scan cycle.
        self.clock.advance_s(Ccu3SensorSource.DISCOVERY_BACKOFF_S + 1)
        run(source.read())
        self.assertEqual(fake.methods.count("Device.listAll"), 6)

    def test_successful_discovery_clears_the_marker(self):
        fake = self._no_weather()
        source = self.make(fake)
        run(source.read())  # fails -> backoff marker set
        self.assertIsNotNone(source._discovery_failed_ms)
        self.clock.advance_s(Ccu3SensorSource.DISCOVERY_BACKOFF_S + 1)
        source._rpc._post = FakeCcu3()  # the weather station is back
        t_out, _lux, _ts = run(source.read())
        self.assertIsNotNone(t_out)
        self.assertIsNone(source._discovery_failed_ms)  # success clears it


if __name__ == "__main__":
    unittest.main()
