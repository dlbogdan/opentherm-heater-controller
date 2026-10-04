"""Room-model correctness against the production demand recipe (host).

tests/test_heating_groups.py covers the discovery modes with a minimal fake.
This suite models the REAL house shape (7 WTH-filtered rooms, mixed states:
comfort, OFF=5, ON=30, tiny deltas, missing values) and pins the aggregate
the control loop consumes -- the ReGaHd recipe from AGENTS.md -- plus the
aggregate's internal self-consistency, freshness (ts), and the
two-heating-groups-in-one-room dedupe (a room double in the denominator
dilutes demand for every OTHER room, not just that one).
"""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from heating_groups import HeatingGroups  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _device(dev_id, dtype, address, channel):
    return dev_id, {"id": dev_id, "type": dtype,
                    "interface": "VirtualDevices" if "HEATING" in dtype
                    else "HmIP-RF",
                    "address": address, "name": "n-" + address,
                    "channels": [{"id": channel}]}


class HouseCcu3:
    """Seven HEATING rooms each with a WTH; optional extras per scenario.

    rooms: list of (name, setpoint_or_None, actual_or_None)
    extras: {"second_group_in": room_index, "extra_wth_room": bool, ...}
    """

    def __init__(self, rooms, demand_cap_rooms=None, second_group_in=None):
        self.session = "sid"
        self.devices, self.rooms, self.values = {}, {}, {}
        self.values_by = {}
        for i, (name, sp, act) in enumerate(rooms):
            room_id = str(i + 1)
            group = "INT%04d" % i
            self.devices[str(100 + i)] = {
                "id": str(100 + i), "type": "HmIP-HEATING",
                "interface": "VirtualDevices", "address": group,
                "name": name, "channels": [{"id": str(1000 + i)}]}
            self.devices[str(200 + i)] = {
                "id": str(200 + i), "type": "HmIP-WTH-2",
                "interface": "HmIP-RF", "address": "W%d" % i,
                "name": name + " WTH", "channels": [{"id": str(2000 + i)}]}
            chans = [str(1000 + i), str(2000 + i)]
            if second_group_in == i:  # a second group in the same room
                dup = "INTDUP%d" % i
                self.devices[str(150 + i)] = {
                    "id": str(150 + i), "type": "HmIP-HEATING",
                    "interface": "VirtualDevices", "address": dup,
                    "name": name + " dup", "channels": [{"id": str(2500 + i)}]}
                chans.append(str(2500 + i))
                self._dup = (group, dup)
            self.rooms[room_id] = {"id": room_id, "name": name,
                                   "channelIds": chans}
            if sp is not None:
                self.values[("VirtualDevices", group,
                             "SET_POINT_TEMPERATURE")] = "%.2f" % sp
            if act is not None:
                self.values[("VirtualDevices", group,
                             "ACTUAL_TEMPERATURE")] = "%.2f" % act
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
    def __init__(self, cap=3.0):
        self._cap = cap

    def get(self, k):
        return {"ccu3_url": "http://ccu/api/homematic.cgi", "ccu3_user": "u",
                "ccu3_pass": "p", "room_source": "heating_groups",
                "demand_delta_cap": self._cap, "rooms_poll_s": 300}.get(k)


class DemandRecipeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = str(Path(self.tmp.name) / "rooms.json")

    def read(self, rooms, second_group_in=None, cap=3.0):
        fake = HouseCcu3(rooms, second_group_in=second_group_in)
        hg = HeatingGroups(FakeConfig(cap), cache_path=self.cache,
                           http_post=fake)
        return hg, run(hg.read())

    def test_recipe_over_a_house_shaped_set(self):
        # name, setpoint, actual -- production mix, verified by hand:
        rooms = [("Kitchen", 22.0, 20.0),      # delta 2.0  (demands)
                 ("Kids", 20.5, 21.6),         # -1.1      (no)
                 ("KidsS", 21.5, 21.4),        # +0.1      (demands)
                 ("Bedroom", 21.5, 21.4),      # +0.1      (demands)
                 ("Garage", 5.0, 20.8),        # OFF mode: SKIPPED
                 ("Attic", 30.0, 18.5),        # ON mode:  SKIPPED
                 ("Living", 22.0, 22.3)]       # -0.3      (no)
        _hg, r = self.read(rooms)
        self.assertEqual(r["total"], 7)
        self.assertEqual(r["thermostats"], 7)          # values present count
        self.assertEqual(r["demanding_rooms"], 3)      # 2.0 + 0.1 + 0.1
        # recipe: sum(deltas)/ALL thermostats / cap * 100
        self.assertAlmostEqual(r["avg_delta"], 2.2 / 7)
        self.assertAlmostEqual(r["demand_pct"], (2.2 / 7) / 3.0 * 100.0)
        self.assertAlmostEqual(r["max_delta"], 2.0)
        self.assertEqual(r["max_delta_room"], "Kitchen")

    def test_two_groups_same_room_do_not_double_count(self):
        rooms = [("A", 22.0, 20.0), ("B", 21.0, 20.0)]  # deltas 2.0 and 1.0
        hg, r = self.read(rooms, second_group_in=0)
        self.assertEqual(r["total"], 2)  # NOT 3: dup group folded away
        self.assertEqual(len(hg._rooms), 2)
        self.assertAlmostEqual(r["demand_pct"], (3.0 / 2) / 3.0 * 100.0)

    def test_off_and_on_modes_never_demand(self):
        rooms = [("Off", 4.5, 10.0),    # below 5: invalid setpoint
                 ("Edge", 5.0, 4.0),    # AT 5: excluded (exclusive bound)
                 ("On", 30.0, 20.0),    # AT 30: excluded
                 ("Hot", 29.9, 20.0)]   # just below: demands 9.9
        _hg, r = self.read(rooms)
        self.assertEqual(r["demanding_rooms"], 1)
        self.assertAlmostEqual(r["max_delta"], 9.9, places=3)
        self.assertLessEqual(r["demand_pct"], 100.0)

    def test_all_saturated_house_demands_zero(self):
        rooms = [(str(i), 21.0, 22.0) for i in range(7)]  # all above setpoint
        _hg, r = self.read(rooms)
        self.assertEqual(r["demand_pct"], 0.0)
        self.assertEqual(r["demanding_rooms"], 0)
        self.assertEqual(r["thermostats"], 7)  # the denominator still counts

    def test_dead_ccu_values_degrade_to_no_demand_not_crash(self):
        rooms = [("A", None, None), ("B", None, None)]  # every getValue fails
        _hg, r = self.read(rooms)
        self.assertIsNone(r["demand_pct"])
        self.assertEqual(r["reading"], 0)

    def test_aggregate_is_self_consistent(self):
        # The device-side freshness/consistency test relies on these field
        # relations holding; pin them so the semantics can't silently drift.
        rooms = [("A", 22.0, 20.4), ("B", 20.0, 20.0), ("C", 21.0, 19.0)]
        _hg, r = self.read(rooms)
        self.assertAlmostEqual(r["avg_delta"] * r["thermostats"],
                               (1.6 + 0.0 + 2.0), places=6)
        self.assertAlmostEqual(
            r["demand_pct"],
            min(100.0, max(0.0, r["avg_delta"] / 3.0 * 100.0)), places=6)

    def test_ts_marks_pass_time_within_clock_skew(self):
        before = int(time.monotonic() * 1000)
        _hg, r = self.read([("A", 21.0, 20.0)])
        after = int(time.monotonic() * 1000)
        self.assertIsInstance(r["ts"], int)
        # _now_ms() is monotonic_ms on the host: the pass timestamp must sit
        # between the walls around the read (5 s skew tolerance).
        self.assertGreaterEqual(r["ts"], before - 5000)
        self.assertLessEqual(r["ts"], after + 5000)

    def test_delta_cap_rescales_demand(self):
        rooms = [("A", 22.0, 20.0)]  # delta 2.0
        _hg, r3 = self.read(rooms, cap=3.0)
        _hg, r2 = self.read(rooms, cap=2.0)
        self.assertAlmostEqual(r3["demand_pct"], (2 / 3) * 100 / 1)  # /1 room
        self.assertAlmostEqual(r2["demand_pct"], 100.0)  # 2.0/2.0 = 100%


if __name__ == "__main__":
    unittest.main()
