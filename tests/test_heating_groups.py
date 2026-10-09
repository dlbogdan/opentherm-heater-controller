"""Host tests for the async room model (app/heating_groups.py).

A fake CCU3 stands in, modelling devices (HmIP-HEATING groups, a WTH, eTRVs),
rooms (Room.listAll/Room.get with numeric channelIds), and value reads. It
exercises: the WTH filter (default mode), per-room eTRV averaging (etrv mode),
the demand signal, and the cache.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from heating_groups import HeatingGroups  # noqa: E402
import heating_groups  # noqa: E402  (module-level patch targets below)


def run(coro):
    return asyncio.run(coro)


class FakeCcu3:
    def __init__(self):
        self.session = "sid-1"
        # channel ids: groups A/B, one WTH, two eTRVs
        self.devices = {
            "100": {"id": "100", "type": "HmIP-HEATING",
                    "interface": "VirtualDevices", "address": "INT0001",
                    "name": "Group A", "channels": [{"id": "1001"}]},
            "101": {"id": "101", "type": "HmIP-HEATING",
                    "interface": "VirtualDevices", "address": "INT0002",
                    "name": "Group B", "channels": [{"id": "1011"}]},
            "200": {"id": "200", "type": "HmIP-WTH-1", "interface": "HmIP-RF",
                    "address": "W1", "name": "WTH", "channels": [{"id": "2001"}]},
            "300": {"id": "300", "type": "HmIP-eTRV-2", "interface": "HmIP-RF",
                    "address": "E1", "name": "E1", "channels": [{"id": "3001"}]},
            "301": {"id": "301", "type": "HmIP-eTRV-2", "interface": "HmIP-RF",
                    "address": "E2", "name": "E2", "channels": [{"id": "3011"}]},
        }
        # Room A has group A + the WTH + two eTRVs; Room B has only group B.
        self.rooms = {
            "1": {"id": "1", "name": "Room A",
                  "channelIds": ["1001", "2001", "3001", "3011"]},
            "2": {"id": "2", "name": "Room B", "channelIds": ["1011"]},
        }
        self.values = {
            ("VirtualDevices", "INT0001", "SET_POINT_TEMPERATURE"): "22.0",
            ("VirtualDevices", "INT0001", "ACTUAL_TEMPERATURE"): "20.0",
            ("HmIP-RF", "E1", "SET_POINT_TEMPERATURE"): "20.0",
            ("HmIP-RF", "E1", "ACTUAL_TEMPERATURE"): "19.0",
            ("HmIP-RF", "E2", "SET_POINT_TEMPERATURE"): "24.0",
            ("HmIP-RF", "E2", "ACTUAL_TEMPERATURE"): "22.0",
        }
        self.methods = []

    async def __call__(self, url, body):
        req = json.loads(body.decode())
        method, params, rid = req["method"], req["params"], req["id"]
        self.methods.append(method)
        if method == "Session.login":
            return ("HTTP/1.0 200 OK", {}, self._ok(rid, self.session))
        if params.get("_session_id_") != self.session:
            return ("HTTP/1.0 200 OK", {}, self._err(rid, -1, "not logged in"))
        if method == "Device.listAll":
            return ("HTTP/1.0 200 OK", {}, self._ok(rid, list(self.devices)))
        if method == "Device.get":
            d = self.devices.get(params["id"])
            if d:
                return ("HTTP/1.0 200 OK", {}, self._ok(rid, d))
            return ("HTTP/1.0 200 OK", {}, self._err(rid, 1, "no device"))
        if method == "Room.listAll":
            return ("HTTP/1.0 200 OK", {}, self._ok(rid, list(self.rooms)))
        if method == "Room.get":
            r = self.rooms.get(params["id"])
            if r:
                return ("HTTP/1.0 200 OK", {}, self._ok(rid, r))
            return ("HTTP/1.0 200 OK", {}, self._err(rid, 1, "no room"))
        if method == "Interface.getValue":
            key = (params["interface"], params["address"][:-2],
                   params["valueKey"])
            if key in self.values:
                return ("HTTP/1.0 200 OK", {}, self._ok(rid, self.values[key]))
            return ("HTTP/1.0 200 OK", {}, self._err(rid, 1, "no key"))
        raise AssertionError("unexpected " + method)

    @staticmethod
    def _ok(rid, result):
        return json.dumps({"result": result, "error": None, "id": rid}).encode()

    @staticmethod
    def _err(rid, code, message):
        return json.dumps({"result": None,
                           "error": {"code": code, "message": message},
                           "id": rid}).encode()


class FakeConfig:
    def __init__(self, room_source="heating_groups", delta_cap=3.0):
        self._rs = room_source
        self._cap = delta_cap

    def get(self, k):
        base = {"ccu3_url": "http://127.0.0.1/api/homematic.cgi",
                "ccu3_user": "u", "ccu3_pass": "p"}
        if k == "room_source":
            return self._rs
        if k == "demand_delta_cap":
            return self._cap
        return base.get(k)


class RoomModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = str(Path(self.tmp.name) / "rooms.json")
        self.registry = str(Path(self.tmp.name) / "weights.json")

    def make(self, fake, room_source="heating_groups", cap=3.0):
        return HeatingGroups(FakeConfig(room_source, cap),
                             cache_path=self.cache, registry_path=self.registry,
                             http_post=fake)

    def test_heating_groups_mode_applies_wth_filter(self):
        # group A is in Room A (has a WTH) -> active; group B is in Room B
        # (no WTH) -> excluded.
        fake = FakeCcu3()
        r = run(self.make(fake).read())
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["thermostats"], 1)
        self.assertAlmostEqual(r["avg_setpoint"], 22.0)
        self.assertAlmostEqual(r["avg_actual"], 20.0)
        # delta 2.0 over 1 thermostat, cap 3 -> 66.7%
        self.assertAlmostEqual(r["demand_pct"], (2.0 / 3.0) * 100.0)

    def test_heating_groups_mode_room_name_is_canonical(self):
        fake = FakeCcu3()
        hg = self.make(fake)
        run(hg.read())
        names = [room["room_name"] for room in hg._rooms]
        self.assertEqual(names, ["Room A"])  # from Room API, not device name

    def test_etrv_mode_averages_per_room(self):
        fake = FakeCcu3()
        r = run(self.make(fake, room_source="etrv").read())
        self.assertEqual(r["total"], 1)          # only Room A has eTRVs
        # E1 sp20/act19 + E2 sp24/act22 -> avg sp 22, avg act 20.5
        self.assertAlmostEqual(r["avg_setpoint"], 22.0)
        self.assertAlmostEqual(r["avg_actual"], 20.5)
        # delta 1.5 over 1 thermostat, cap 3 -> 50%
        self.assertAlmostEqual(r["demand_pct"], (1.5 / 3.0) * 100.0)

    def test_cache_hit_avoids_rediscovery(self):
        run(self.make(FakeCcu3()).read())  # populate the cache
        fake2 = FakeCcu3()
        run(self.make(fake2).read())
        self.assertNotIn("Device.listAll", fake2.methods)
        self.assertNotIn("Room.listAll", fake2.methods)

    def test_old_schema_cache_without_room_id_forces_rediscovery(self):
        run(self.make(FakeCcu3()).read())  # populate a new-schema cache
        with open(self.cache) as handle:
            data = json.load(handle)
        for room in data["rooms"]:  # downgrade to the pre-P6a schema
            room.pop("room_id", None)
            room.pop("kind", None)
        with open(self.cache, "w") as handle:
            json.dump(data, handle)
        fake2 = FakeCcu3()
        hg = self.make(fake2)
        run(hg.read())
        self.assertIn("Device.listAll", fake2.methods)  # old cache rejected
        self.assertTrue(hg.weights())  # one-time re-discovery seeded the registry


class FakeClock:
    """Controllable ``_now_ms`` substitute (module-patched in the tests below)."""

    def __init__(self, start_ms=1000000):
        self.now = start_ms

    def __call__(self):
        return self.now

    def advance_s(self, seconds):
        self.now += int(seconds * 1000)


class _NoopSleep:
    """Stands in for the module's asyncio (only its .sleep is used here)."""

    @staticmethod
    async def sleep(_seconds):
        return None


class FailingCcu3:
    """Transport-level failure on every call (counts the attempts)."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, url, body):
        self.calls += 1
        raise OSError("connection refused")


class DiscoveryBackoffTests(unittest.TestCase):
    """P2: a failed discovery must back off instead of re-running the full
    scan on every poll (the same ~50-device storm as the weather source;
    rooms is isolated from the control tick but NOT bounded without this)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = str(Path(self.tmp.name) / "ccu3_rooms_cache.json")
        self.registry = str(Path(self.tmp.name) / "weights.json")
        self.clock = FakeClock()
        saved_clock = heating_groups._now_ms
        saved_asyncio = heating_groups.asyncio
        heating_groups._now_ms = self.clock
        heating_groups.asyncio = _NoopSleep()
        self.addCleanup(lambda: setattr(heating_groups, "_now_ms",
                                        saved_clock))
        self.addCleanup(lambda: setattr(heating_groups, "asyncio",
                                        saved_asyncio))

    def make(self, post):
        return HeatingGroups(FakeConfig(), cache_path=self.cache,
                             registry_path=self.registry, http_post=post)

    def test_failed_discovery_backs_off_instead_of_re_scanning(self):
        post = FailingCcu3()
        rooms = self.make(post)
        run(rooms.read())  # first read: 3-attempt scan, then fails
        self.assertGreaterEqual(post.calls, 3)
        self.assertIsNone(rooms._rooms)
        calls_after_first = post.calls
        # Inside the backoff window: zero new RPC calls, empty pass.
        agg = run(rooms.read())
        self.assertEqual(post.calls, calls_after_first)
        self.assertIsNone(agg["demand_pct"])
        # After the window elapses: one more attempt cycle.
        self.clock.advance_s(heating_groups.DISCOVERY_BACKOFF_S + 1)
        run(rooms.read())
        self.assertGreater(post.calls, calls_after_first)

    def test_successful_discovery_clears_the_marker(self):
        post = FailingCcu3()
        rooms = self.make(post)
        run(rooms.read())  # fails -> backoff marker set
        self.assertIsNotNone(rooms._discovery_failed_ms)
        self.clock.advance_s(heating_groups.DISCOVERY_BACKOFF_S + 1)
        rooms._rpc._post = FakeCcu3()  # the CCU3 recovers
        agg = run(rooms.read())
        self.assertIsNotNone(agg["demand_pct"])
        self.assertIsNone(rooms._discovery_failed_ms)  # success clears it


class FakeCcu3Union(FakeCcu3):
    """Room Big (group+WTH+eTRV), Room Small (eTRV only, no group),
    Room Both (group+WTH+eTRV). Proves the P6a union + group-wins rule."""

    def __init__(self, with_small_room=True):
        FakeCcu3.__init__(self)
        self.devices = {
            "100": {"id": "100", "type": "HmIP-HEATING",
                    "interface": "VirtualDevices", "address": "INT0001",
                    "name": "Big", "channels": [{"id": "1001"}]},
            "200": {"id": "200", "type": "HmIP-WTH-1", "interface": "HmIP-RF",
                    "address": "W1", "name": "W1", "channels": [{"id": "2001"}]},
            "300": {"id": "300", "type": "HmIP-eTRV-2", "interface": "HmIP-RF",
                    "address": "E1", "name": "E1", "channels": [{"id": "3001"}]},
            "130": {"id": "130", "type": "HmIP-HEATING",
                    "interface": "VirtualDevices", "address": "INT0003",
                    "name": "Both", "channels": [{"id": "1031"}]},
            "230": {"id": "230", "type": "HmIP-WTH-1", "interface": "HmIP-RF",
                    "address": "W3", "name": "W3", "channels": [{"id": "2031"}]},
            "330": {"id": "330", "type": "HmIP-eTRV-2", "interface": "HmIP-RF",
                    "address": "E3", "name": "E3", "channels": [{"id": "3031"}]},
        }
        self.rooms = {
            "1": {"id": "1", "name": "Room Big",
                  "channelIds": ["1001", "2001", "3001"]},
            "3": {"id": "3", "name": "Room Both",
                  "channelIds": ["1031", "2031", "3031"]},
        }
        self.values = {
            ("VirtualDevices", "INT0001", "SET_POINT_TEMPERATURE"): "22.0",
            ("VirtualDevices", "INT0001", "ACTUAL_TEMPERATURE"): "20.0",
            # Group setpoint differs from the eTRV so group-wins is provable.
            ("VirtualDevices", "INT0003", "SET_POINT_TEMPERATURE"): "21.0",
            ("VirtualDevices", "INT0003", "ACTUAL_TEMPERATURE"): "20.0",
            ("HmIP-RF", "E3", "SET_POINT_TEMPERATURE"): "25.0",
            ("HmIP-RF", "E3", "ACTUAL_TEMPERATURE"): "24.0",
        }
        if with_small_room:
            self.devices["310"] = {"id": "310", "type": "HmIP-eTRV-2",
                                   "interface": "HmIP-RF", "address": "E2",
                                   "name": "E2", "channels": [{"id": "3011"}]}
            self.rooms["2"] = {"id": "2", "name": "Room Small",
                               "channelIds": ["3011"]}
            self.values[("HmIP-RF", "E2", "SET_POINT_TEMPERATURE")] = "23.0"
            self.values[("HmIP-RF", "E2", "ACTUAL_TEMPERATURE")] = "20.0"


class UnionModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = str(Path(self.tmp.name) / "rooms.json")
        self.registry = str(Path(self.tmp.name) / "weights.json")

    def make(self, fake, room_source="heating_groups+etrvs"):
        return HeatingGroups(FakeConfig(room_source), cache_path=self.cache,
                             registry_path=self.registry, http_post=fake)

    def test_union_membership_and_kinds(self):
        hg = self.make(FakeCcu3Union())
        run(hg.read())
        self.assertEqual(sorted(r["room_id"] for r in hg._rooms),
                         ["1", "2", "3"])
        self.assertEqual({r["room_id"]: r["kind"] for r in hg._rooms},
                         {"1": "group", "2": "etrv", "3": "group"})

    def test_union_group_wins_when_room_has_both(self):
        r = run(self.make(FakeCcu3Union()).read())
        self.assertEqual(r["total"], 3)
        # Room Both uses the GROUP setpoint (21.0), not its eTRV (25.0):
        # avg sp = (22 + 23 + 21) / 3 = 22.0.
        self.assertAlmostEqual(r["avg_setpoint"], 22.0)

    def test_union_demand_is_weighted(self):
        hg = self.make(FakeCcu3Union())
        r = run(hg.read())
        # all weights 1.0 -> unweighted mean delta (2+3+1)/3 = 2.0 -> 66.7%
        self.assertAlmostEqual(r["demand_pct"], (2.0 / 3.0) * 100.0)
        hg.set_weight("2", 0.4)  # down-weight the small group-less room
        r2 = run(hg.read())      # cache hit; weight applied at aggregate
        self.assertAlmostEqual(r2["weighted_thermostats"], 2.4)
        # (2 + 0.4*3 + 1) / 2.4 = 1.75 -> /3 -> 58.33%
        self.assertAlmostEqual(r2["demand_pct"], (1.75 / 3.0) * 100.0)

    def test_reconcile_flags_vanished_but_keeps_weight(self):
        hg = self.make(FakeCcu3Union())
        run(hg.read())
        hg.set_weight("2", 0.4)
        os.remove(self.cache)  # force a rediscovery
        hg2 = self.make(FakeCcu3Union(with_small_room=False))
        run(hg2.read())
        byid = {e["room_id"]: e for e in hg2.weights()}
        self.assertIn("2", byid)                       # never auto-deleted
        self.assertFalse(byid["2"]["sensors_alive"])   # flagged gone
        self.assertAlmostEqual(byid["2"]["weight"], 0.4)  # tuning preserved

    def test_forget_prunes_a_room(self):
        hg = self.make(FakeCcu3Union())
        run(hg.read())
        ok, _ = hg.forget("Room Small")
        self.assertTrue(ok)
        self.assertNotIn("2", [e["room_id"] for e in hg.weights()])

    def test_weight_out_of_domain_rejected(self):
        hg = self.make(FakeCcu3Union())
        run(hg.read())
        ok, _ = hg.set_weight("1", 1.5)
        self.assertFalse(ok)
        self.assertEqual(hg._weight_for("1"), 1.0)


class PeriodicRediscoveryTests(unittest.TestCase):
    """P6a: a valid-but-stale cache must re-run discovery after
    rooms_rediscovery_s so a dead/added device surfaces."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = str(Path(self.tmp.name) / "rooms.json")
        self.registry = str(Path(self.tmp.name) / "weights.json")
        self.clock = FakeClock()
        saved_clock = heating_groups._now_ms
        saved_asyncio = heating_groups.asyncio
        heating_groups._now_ms = self.clock
        heating_groups.asyncio = _NoopSleep()
        self.addCleanup(lambda: setattr(heating_groups, "_now_ms", saved_clock))
        self.addCleanup(lambda: setattr(heating_groups, "asyncio",
                                        saved_asyncio))

    def make(self, fake):
        hg = HeatingGroups(FakeConfig("heating_groups+etrvs"),
                           cache_path=self.cache, registry_path=self.registry,
                           http_post=fake)
        hg._rediscovery_s = 100
        return hg

    def test_stale_cache_reruns_the_scan(self):
        hg = self.make(FakeCcu3Union())
        run(hg.read())  # first discovery
        fake2 = FakeCcu3Union()
        hg._rpc._post = fake2
        run(hg.read())  # within window: cache, no scan
        self.assertNotIn("Device.listAll", fake2.methods)
        self.clock.advance_s(hg._rediscovery_s + 1)
        run(hg.read())  # stale: re-discover
        self.assertIn("Device.listAll", fake2.methods)

    def test_failed_refresh_keeps_the_previous_room_list(self):
        hg = self.make(FakeCcu3Union())
        r1 = run(hg.read())
        self.clock.advance_s(hg._rediscovery_s + 1)
        hg._rpc._post = FailingCcu3()
        # The scan fails (backoff marker set) but the stale-but-good room
        # list survives for the next pass; the value reads then raise (the
        # pre-existing pass-abort contract -- the stale aggregate keeps
        # steering via the P4 cached tier).
        self.assertRaises(OSError, run, hg.read())
        self.assertIsNotNone(hg._rooms)
        self.assertEqual(len(hg._rooms), r1["total"])
        # Reconcile only runs on a SUCCESSFUL scan: no false vanish flags.
        for entry in hg.weights():
            self.assertTrue(entry["sensors_alive"])


if __name__ == "__main__":
    unittest.main()
