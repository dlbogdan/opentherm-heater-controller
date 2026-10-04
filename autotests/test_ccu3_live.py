"""Read-only CCU3 integration suite (pushed to /autotests).

Exercises the REAL async CCU3 client on the board (host tests inject a fake
http_post, so only here does ``app/ccu3.py`` run against the production
CCU3). Every call is a read method: Session.login, Device.listAll,
Device.get, Interface.getValue -- the recipe in AGENTS.md. NEVER extend this
file with setValue or any config RPC: the CCU3 is production.

Session discipline (learned the hard way, live): the CCU3 caps concurrent
RPC sessions -- one login PER TEST exhausted the pool and every login then
failed with 'invalid credentials or too many sessions' until the idle
sessions expired (~minutes). So the whole module shares ONE session,
created lazily, and shares ONE weather source instance: a full run costs
the production CCU3 exactly two sessions (it also runs ALONGSIDE the app's
own periodic sessions). Every login retries with backoff so a transiently
full pool degrades to a clear failure, not a cascade.

Every I/O test is async: the runner awaits it inside the app's own loop
with a bounded timeout, so a slow CCU3 slows only the test, never the board.
"""

import gc
import unittest

import uasyncio as asyncio

from ccu3 import Ccu3Rpc, Ccu3Error, _looks_like_session_error, _now_ms
from config import config

# Module-level shared session (the runner re-imports this module per run).
_state = {"rpc": None, "source": None}


async def _login_with_backoff(rpc, attempts=4, base_s=3.0):
    """Login, retrying a full session pool with backoff (production-safe)."""
    last = None
    for attempt in range(attempts):
        try:
            return await rpc.login()
        except (Ccu3Error, OSError) as exc:
            last = exc
            if attempt < attempts - 1:
                await asyncio.sleep(base_s * (attempt + 1))
    raise Ccu3Error("CCU3 login failed (%d tries): %s" % (attempts, last))


async def _rpc():
    if _state["rpc"] is None:
        rpc = Ccu3Rpc(config.get("ccu3_url"), config.get("ccu3_user"),
                      config.get("ccu3_pass"))
        await _login_with_backoff(rpc)
        _state["rpc"] = rpc
    return _state["rpc"]


async def _retry_once(coro_factory, what):
    """Run an RPC step once; retry once on a transient socket OSError.

    The suite shares the board with the app's OWN periodic CCU3 polls, and
    the Pico heap is tight: a concurrent socket open can transiently fail
    with ENOMEM/EHOSTUNREACH (AGENTS.md). One retry separates that real
    contention from a genuine protocol failure (which fails twice).
    """
    try:
        return await coro_factory()
    except OSError:
        await asyncio.sleep(1.0)
        return await coro_factory()


async def _call(rpc, method, params):
    return await _retry_once(lambda: rpc.call(method, params), method)


async def _source():
    if _state["source"] is None:
        from ccu3 import Ccu3SensorSource
        _state["source"] = Ccu3SensorSource(config)
    return _state["source"]


async def _wait_wifi(timeout_s=60):
    """Wait (bounded) for the app's Wi-Fi to come up, like main.py does.

    A sync test cannot be wrapped by the runner's wait_for, so I/O tests
    must START with Wi-Fi already up: running at t+4s after a push-reset
    means racing DHCP and the app's own boot-time poll on a tight heap
    (that race is real -- the app's first poll can ENOMEM -- and a suite
    must not fail for a documented, self-healing transient).
    """
    deadline = _now_ms() + timeout_s * 1000
    import network
    sta = network.WLAN(network.STA_IF)
    while _now_ms() < deadline:
        try:
            if sta.active() and sta.isconnected():
                return True
        except OSError:
            pass
        await asyncio.sleep(1.0)
    return False


class TestCcu3Rpc(unittest.TestCase):
    """Read-only RPC against the live CCU3 (one shared module session)."""

    timeout_s = 150  # ~2.8 s per RPC on the Pico; a cold scan is dozens

    async def setUp(self):
        if not config.get("ccu3_url"):
            self.skipTest("no ccu3_url configured")
        if not await _wait_wifi():
            self.skipTest("Wi-Fi did not come up")

    async def test_login_returns_session(self):
        rpc = await _rpc()
        self.assertTrue(isinstance(rpc._session, str) and rpc._session)

    async def test_device_list_all(self):
        rpc = await _rpc()
        ids = await _call(rpc, "Device.listAll", {})
        self.assertTrue(isinstance(ids, list))
        self.assertGreater(len(ids), 10)  # this house has ~50 devices

    async def test_weather_endpoint_resolves_and_reads(self):
        # The AGENTS.md recipe: an HmIP-SWO exposes ACTUAL_TEMPERATURE and
        # ILLUMINATION on channel :1 as parseable numbers. Use the cached
        # endpoint (the app discovers once and caches to flash); a full
        # Device.get scan on the Pico is minutes of I/O, reserved for the
        # genuinely cold-cache case.
        want = config.get("ccu3_weather_type")
        rpc = await _rpc()
        endpoint = self._cached_endpoint()
        if endpoint is None:
            endpoint = await self._discover(rpc, want)
        self.assertIsNotNone(endpoint, "no %s on the CCU3" % want)
        temp = await self._value(rpc, endpoint, "ACTUAL_TEMPERATURE")
        lux = await self._value(rpc, endpoint, "ILLUMINATION")
        self.assertTrue(-40.0 < temp < 60.0, "t_out sane: %r" % temp)
        self.assertTrue(0.0 <= lux <= 200000.0, "lux sane: %r" % lux)

    async def test_session_expiry_recovery(self):
        # A deliberately bogus session must fail as a session-flavoured
        # Ccu3Error -- the exact trigger the client uses to re-login. This
        # pins that our detector still matches the CCU3's real error wording
        # (verified live: Interface.getValue under a bogus session answers
        # 'access denied ("GUEST" needed 0)'). NB: do not use System.version
        # here -- for a GUEST session the CCU answers 'method not found',
        # which is (correctly) NOT session-detectable. Uses its OWN client
        # (no login) so the shared session is never disturbed.
        from ccu3 import _looks_like_session_error as detect
        probe_client = Ccu3Rpc(config.get("ccu3_url"),
                               config.get("ccu3_user"),
                               config.get("ccu3_pass"))
        probe_client._session = "autotest-bogus-session"
        probe = {"interface": "virtual-devices", "address": "x:1",
                 "valueKey": "ACTUAL_TEMPERATURE"}
        try:
            await probe_client._raw_call("Interface.getValue", probe)
            self.fail("bogus session was (somehow) accepted")
        except Ccu3Error as exc:
            self.assertTrue(detect(exc), "error no longer detectable: %s"
                            % exc)

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

    async def _discover(self, rpc, want):
        ids = await rpc.call("Device.listAll", {}) or []
        seen = 0
        for device_id in ids:
            try:
                device = await _call(rpc, "Device.get", {"id": device_id})
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

    async def _value(self, rpc, endpoint, key):
        iface, addr = endpoint
        raw = await _call(rpc, "Interface.getValue",
                          {"interface": iface, "address": addr + ":1",
                           "valueKey": key})
        if isinstance(raw, dict):
            raw = raw.get("value")
        return float(raw)


class TestWeatherSource(unittest.TestCase):
    """The production Ccu3SensorSource end-to-end (poll + value sanity)."""

    timeout_s = 120

    async def setUp(self):
        if not config.get("ccu3_url"):
            self.skipTest("no ccu3_url configured")
        if not await _wait_wifi():
            self.skipTest("Wi-Fi did not come up")

    async def test_source_read_returns_sane_weather(self):
        source = await _source()
        t_out, lux, demand = await source.read()
        if t_out is None:
            # A transient ENOMEM/EHOSTUNREACH on the source's very first
            # poll (shared heap + the app's own poll racing at boot) is
            # expected; the defined recovery is "next interval retries".
            await asyncio.sleep(2.0)
            t_out, lux, demand = await source.read()
        self.assertIsNotNone(t_out, "weather poll produced no t_out")
        self.assertTrue(-40.0 < t_out < 60.0)
        self.assertTrue(lux is None or 0.0 <= lux <= 200000.0)
        self.assertIsNone(demand)  # the weather source never carries demand

    async def test_second_read_hits_the_poll_cache(self):
        # ccu3_poll_s gates real I/O: an immediate second read must return
        # the SAME real values (proof the periodic control loop reads are
        # bounded, not one RPC storm per tick). The sanity precondition makes
        # a (None, None) == (None, None) equality impossible to pass
        # vacuously; the two short waits absorb a boot-race ENOMEM on the
        # very first poll (defined behavior: next interval retries).
        source = await _source()
        first = await source.read()
        for _ in range(2):
            if first[0] is not None:
                break
            await asyncio.sleep(2.0)
            first = await source.read()
        self.assertIsNotNone(first[0], "no live t_out to compare")
        second = await source.read()
        self.assertEqual(first, second)
