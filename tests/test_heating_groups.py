"""Host tests for the async room model (app/heating_groups.py).

A fake CCU3 stands in, modelling devices (HmIP-HEATING groups, a WTH, eTRVs),
rooms (Room.listAll/Room.get with numeric channelIds), and value reads. It
exercises: the WTH filter (default mode), per-room eTRV averaging (etrv mode),
the demand signal, and the cache.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from heating_groups import HeatingGroups  # noqa: E402


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

    def make(self, fake, room_source="heating_groups", cap=3.0):
        return HeatingGroups(FakeConfig(room_source, cap),
                             cache_path=self.cache, http_post=fake)

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


if __name__ == "__main__":
    unittest.main()
