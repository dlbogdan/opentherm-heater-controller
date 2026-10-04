"""Read-only CCU3 integration suite (pushed to /autotests).

Exercises the REAL async CCU3 client on the board (host tests inject a fake
http_post, so only here does ``app/ccu3.py`` run against the production
CCU3). Every call is a read method: Session.login, Device.listAll,
Device.get, Interface.getValue -- the recipe in AGENTS.md. NEVER extend this
file with setValue or any config RPC: the CCU3 is production.

Session discipline: ALL sessions come from ccu3_live_session.get_rpc() --
ONE pooled login for the whole board boot, shared across every suite and
every 'test run'. Per-module or per-test logins exhausted the CCU3's
session pool (idle sessions linger minutes; the app's own two count too).

Every I/O test is async: the runner awaits it inside the app's own loop
with a bounded timeout, so a slow CCU3 slows only the test, never the board.
"""

import unittest

from ccu3 import Ccu3Rpc, Ccu3Error
from config import config

import ccu3_live_session as live


class TestCcu3Rpc(unittest.TestCase):
    """Read-only RPC against the live CCU3 (pooled shared session)."""

    timeout_s = 150  # ~2.8 s per RPC on the Pico; a cold scan is dozens

    async def setUp(self):
        if not config.get("ccu3_url"):
            self.skipTest("no ccu3_url configured")
        if not await live.wait_wifi():
            self.skipTest("Wi-Fi did not come up")

    async def test_shared_session_alive(self):
        rpc = await live.get_rpc()  # lazy login with backoff on first use
        self.assertTrue(isinstance(rpc._session, str) and rpc._session)

    async def test_device_list_all(self):
        ids = await live.call("Device.listAll", {})
        self.assertTrue(isinstance(ids, list))
        self.assertGreater(len(ids), 10)  # this house has ~50 devices

    async def test_weather_endpoint_resolves_and_reads(self):
        # The AGENTS.md recipe: an HmIP-SWO exposes ACTUAL_TEMPERATURE and
        # ILLUMINATION on channel :1 as parseable numbers. Use the app's
        # discovery cache for identity (a full Device.get scan is minutes of
        # I/O, and this suite must stay cheap on a production CCU3).
        endpoint = self._cached_endpoint()
        if endpoint is None:
            endpoint = await self._discover(config.get("ccu3_weather_type"))
        self.assertIsNotNone(endpoint, "no weather station on the CCU3")
        temp = await self._value(endpoint, "ACTUAL_TEMPERATURE")
        lux = await self._value(endpoint, "ILLUMINATION")
        self.assertTrue(-40.0 < temp < 60.0, "t_out sane: %r" % temp)
        self.assertTrue(0.0 <= lux <= 200000.0, "lux sane: %r" % lux)

    async def test_session_expiry_detector_matches_live_errors(self):
        # A deliberately bogus session must fail as a session-flavoured
        # Ccu3Error -- the exact trigger the client uses to re-login. This
        # pins that our detector still matches the CCU3's real error wording
        # (verified live: Interface.getValue under a bogus session answers
        # 'access denied ("GUEST" needed 0)'). NB: do not use System.version
        # here -- for a GUEST session the CCU answers 'method not found',
        # which is (correctly) NOT session-detectable. Uses a NEVER-LOGGED-IN
        # client, so the pooled session is untouched and no session is spent.
        from ccu3 import _looks_like_session_error
        probe = Ccu3Rpc(config.get("ccu3_url"), config.get("ccu3_user"),
                        config.get("ccu3_pass"))
        probe._session = "autotest-bogus-session"
        params = {"interface": "virtual-devices", "address": "x:1",
                  "valueKey": "ACTUAL_TEMPERATURE"}
        try:
            await probe._raw_call("Interface.getValue", params)
            self.fail("bogus session was (somehow) accepted")
        except Ccu3Error as exc:
            self.assertTrue(_looks_like_session_error(exc),
                            "error no longer detectable: %s" % exc)

    # -- helper ----------------------------------------------------------
    def _cached_endpoint(self):
        import json
        try:
            with open("/ccu3_cache.json") as handle:
                data = json.load(handle)
            if data.get("interface") and data.get("address"):
                return (data["interface"], data["address"])
        except (OSError, ValueError):
            pass
        return None

    async def _discover(self, want):
        import gc
        ids = await live.call("Device.listAll", {}) or []
        seen = 0
        for device_id in ids:
            try:
                device = await live.call("Device.get", {"id": device_id})
            except Ccu3Error:
                continue  # CCU Tcl-error devices: skip, keep scanning
            matched = (isinstance(device, dict)
                       and want in str(device.get("type", "")))
            iface, addr = (device.get("interface"), device.get("address")) \
                if matched else (None, None)
            del device
            if matched and iface and addr:
                return (iface, addr)
            seen += 1
            if seen % 10 == 0:
                gc.collect()
        return None

    async def _value(self, endpoint, key):
        iface, addr = endpoint
        raw = await live.call("Interface.getValue",
                              {"interface": iface, "address": addr + ":1",
                               "valueKey": key})
        if isinstance(raw, dict):
            raw = raw.get("value")
        return float(raw)


class TestWeatherSource(unittest.TestCase):
    """The production Ccu3SensorSource poll logic (cache + value sanity).

    A suite-local source instance exercises the REAL poll/staleness/caching
    code; its internal RPC is swapped for the pooled session (reaching one
    private attribute is the price of the session economy -- everything
    ABOVE the transport is production code).
    """

    timeout_s = 120

    async def setUp(self):
        if not config.get("ccu3_url"):
            self.skipTest("no ccu3_url configured")
        if not await live.wait_wifi():
            self.skipTest("Wi-Fi did not come up")
        from ccu3 import Ccu3SensorSource
        self.source = Ccu3SensorSource(config)
        self.source._rpc = await live.get_rpc()  # pooled session, no new login

    async def test_source_read_returns_sane_weather(self):
        t_out, lux, demand = await self.source.read()
        if t_out is None:
            # A transient socket failure on the very first poll is the
            # defined self-healing race (next interval retries).
            await self._sleep(3.0)
            t_out, lux, demand = await self.source.read()
        self.assertIsNotNone(t_out, "weather poll produced no t_out")
        self.assertTrue(-40.0 < t_out < 60.0)
        self.assertTrue(lux is None or 0.0 <= lux <= 200000.0)
        self.assertIsNone(demand)  # the weather source never carries demand

    async def test_second_read_hits_the_poll_cache(self):
        # ccu3_poll_s gates real I/O: an immediate second read must return
        # the SAME real values (proof periodic control-loop reads are
        # bounded, not one RPC storm per tick). The sanity precondition
        # makes (None, None) == (None, None) impossible to pass vacuously.
        first = await self.source.read()
        for _ in range(2):
            if first[0] is not None:
                break
            await self._sleep(3.0)
            first = await self.source.read()
        self.assertIsNotNone(first[0], "no live t_out to compare")
        second = await self.source.read()
        self.assertEqual(first, second)

    async def _sleep(self, seconds):
        import uasyncio as asyncio
        await asyncio.sleep(seconds)
