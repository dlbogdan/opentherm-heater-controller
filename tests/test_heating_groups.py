"""Host tests for the memory-efficient heating-group collector
(app/heating_groups.py).

A fake CCU3 stands in: it exposes HmIP-HEATING groups (the target) plus an
eTRV and a weather station (which must be excluded). The setpoint key is
``SET_POINT_TEMPERATURE`` (NOT ``SETPOINT``); one room is at OFF (setpoint 5)
to exercise the demand "extreme setpoint" skip.
"""

import json
import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from heating_groups import HeatingGroups  # noqa: E402


class FakeCcu3:
    def __init__(self):
        self.devices = [
            {"id": "12", "type": "HmIP-CCU3", "interface": "HmIP-RF",
             "address": "CCU"},          # internal -> must be skipped
            {"id": "100", "type": "HmIP-eTRV-2", "interface": "HmIP-RF",
             "address": "E1"},           # eTRV -> must be excluded
            {"id": "101", "type": "HmIP-SWO-PL", "interface": "HmIP-RF",
             "address": "WX"},           # weather -> must be excluded
            {"id": "200", "type": "HmIP-HEATING", "interface": "VirtualDevices",
             "address": "INT0000010", "name": "Room A"},
            {"id": "201", "type": "HmIP-HEATING", "interface": "VirtualDevices",
             "address": "INT0000009", "name": "Room B"},
            {"id": "202", "type": "HmIP-HEATING", "interface": "VirtualDevices",
             "address": "INT0000008", "name": "Room C"},
        ]
        self.session = "sid-1"
        # setpoint key is SET_POINT_TEMPERATURE (Room C is OFF at 5)
        self.values = {
            ("VirtualDevices", "INT0000010", "SET_POINT_TEMPERATURE"): "22.0",
            ("VirtualDevices", "INT0000010", "ACTUAL_TEMPERATURE"): "20.0",
            ("VirtualDevices", "INT0000009", "SET_POINT_TEMPERATURE"): "23.0",
            ("VirtualDevices", "INT0000009", "ACTUAL_TEMPERATURE"): "21.0",
            ("VirtualDevices", "INT0000008", "SET_POINT_TEMPERATURE"): "5.0",
            ("VirtualDevices", "INT0000008", "ACTUAL_TEMPERATURE"): "21.0",
        }
        self.calls = []

    def __call__(self, url, body):
        req = json.loads(body.decode())
        method, params, rid = req["method"], req["params"], req["id"]
        self.calls.append(method)
        if method == "Session.login":
            return ("HTTP/1.0 200 OK", {}, self._ok(rid, self.session))
        if params.get("_session_id_") != self.session:
            return ("HTTP/1.0 200 OK", {},
                    self._err(rid, -1, "not logged in"))
        if method == "Device.listAll":
            return ("HTTP/1.0 200 OK", {},
                    self._ok(rid, [d["id"] for d in self.devices]))
        if method == "Device.get":
            for d in self.devices:
                if d["id"] == params["id"]:
                    return ("HTTP/1.0 200 OK", {}, self._ok(rid, d))
            return ("HTTP/1.0 200 OK", {}, self._err(rid, 1, "no device"))
        if method == "Interface.getValue":
            key = (params["interface"], params["address"][:-2],
                   params["valueKey"])
            if key in self.values:
                return ("HTTP/1.0 200 OK", {},
                        self._ok(rid, self.values[key]))
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
    def __init__(self, delta_cap=3.0):
        self._cap = delta_cap

    def get(self, k):
        base = {"ccu3_url": "http://127.0.0.1/api/homematic.cgi",
                "ccu3_user": "u", "ccu3_pass": "p",
                "heating_group_type": "HmIP-HEATING"}
        if k == "demand_delta_cap":
            return self._cap
        return base.get(k)


class HeatingGroupsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = str(Path(self.tmp.name) / "groups.json")

    def make(self, fake, cap=3.0):
        return HeatingGroups(FakeConfig(cap), cache_path=self.cache,
                             http_post=fake)

    def test_discovers_only_heating_groups(self):
        fake = FakeCcu3()
        hg = self.make(fake)
        hg.read()  # discovery happens on first read
        addrs = [g["addr"] for g in hg._groups]
        self.assertEqual(sorted(addrs),
                         ["INT0000008", "INT0000009", "INT0000010"])
        self.assertNotIn("E1", addrs)     # eTRV excluded
        self.assertNotIn("WX", addrs)     # weather excluded
        self.assertNotIn("CCU", addrs)    # CCU internal excluded

    def test_reads_setpoint_and_actual_per_room(self):
        fake = FakeCcu3()
        r = self.make(fake).read()
        self.assertEqual(r["total"], 3)
        self.assertEqual(r["thermostats"], 3)
        # setpoints: 22, 23, 5 -> avg (22+23+5)/3 = 16.667, max 23
        self.assertAlmostEqual(r["avg_setpoint"], (22 + 23 + 5) / 3)
        self.assertAlmostEqual(r["max_setpoint"], 23.0)
        # actuals: 20, 21, 21 -> avg 20.667, max 21
        self.assertAlmostEqual(r["avg_actual"], (20 + 21 + 21) / 3)
        self.assertAlmostEqual(r["max_actual"], 21.0)

    def test_demand_skips_off_and_counts_all_thermostats(self):
        # Room A: 22-20 = 2.0 (demanding). Room B: 23-21 = 2.0 (demanding).
        # Room C: setpoint 5 (OFF) -> skipped, but still a thermostat.
        fake = FakeCcu3()
        r = self.make(fake).read()
        self.assertEqual(r["demanding_rooms"], 2)
        self.assertEqual(r["thermostats"], 3)
        # global avg delta = (2.0 + 2.0) / 3 = 1.333
        self.assertAlmostEqual(r["avg_delta"], 4.0 / 3)
        # demand = (1.333 / 3.0) * 100 = 44.44
        self.assertAlmostEqual(r["demand_pct"], (4.0 / 3) / 3.0 * 100.0)
        self.assertAlmostEqual(r["max_delta"], 2.0)

    def test_demand_saturates_at_cap(self):
        # delta_cap 1.0 with avg_delta 1.333 -> 133% -> clamped to 100
        fake = FakeCcu3()
        r = self.make(fake, cap=1.0).read()
        self.assertAlmostEqual(r["demand_pct"], 100.0)

    def test_cache_hit_avoids_rediscovery(self):
        self.make(FakeCcu3()).read()            # populates the cache
        fake2 = FakeCcu3()
        self.make(fake2).read()                 # fresh instance, cache present
        self.assertNotIn("Device.listAll", fake2.calls)
        self.assertNotIn("Device.get", fake2.calls)


if __name__ == "__main__":
    unittest.main()
