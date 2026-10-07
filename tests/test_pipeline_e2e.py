"""End-to-end pipeline vs the HA blueprint (host, production calibration).

The question this suite answers: when the SAME sensor picture (outdoor temp,
sun, room thermostats) is fed to the firmware and to the production HA
blueprint (boiler_weather_compensation.yaml), do the intermediate
calculations and the boiler output agree?

Layout:
* ``BlueprintRef``  -- an INDEPENDENT transcription of the blueprint's Jinja
  math (rounding kept as-is: round(1)/round(3)/round(0)), stateful like the
  HA helpers (solar accumulator, mode, current setpoint). Deliberately NOT
  the firmware re-derived: drift between the two is the point.
* ``ProductionCcu3`` -- one fake CCU3 endpoint serving an HmIP-SWO weather
  station AND a 7-room HEATING+WTH house, mutable mid-scenario.
* Full-stack scenarios: the REAL Ccu3SensorSource + HeatingGroups +
  Controller + run_control_tick + LogTransport(DummyOTGW), driven by the
  house's production calibration (the persisted app-config.json values,
  NOT the shipped DEFAULTS).

Known, documented divergences (asserted, not hidden):
* Solar accumulator STORAGE: the blueprint writes the CLAMPED offset back to
  its helper; the firmware keeps the raw ODE accumulator and clamps only the
  offset -> after sustained sun the firmware's offset stays pinned longer
  (test_solar_accumulator_storage_policy_divergence_documents_it).
* Rate limiting while OFF: the blueprint re-writes the off setpoint every
  tick (target_flow != its legacy 20); the firmware does the reset ONCE
  (last_sent_flow == off_sentinel -> skip). End state identical.
* Off value: the blueprint's legacy number-mode 20 is superseded by the
  firmware's off_sentinel 0.0 (arch §8.1); the reference uses the sentinel.
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
from heating_groups import HeatingGroups  # noqa: E402
from control.controller import Controller  # noqa: E402
from control.failsafe import frost_clamp  # noqa: E402
from control.solar_accum import solar_step  # noqa: E402
from control_loop import run_control_tick  # noqa: E402
from transport.dummy_otgw import DummyOTGW  # noqa: E402
from transport.log import LogTransport  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# The house's PRODUCTION calibration (the board's persisted app-config.json;
# AGENTS.md: these mirror the tuned blueprint automation, not the shipped
# defaults). Keep in sync with the live deployment -- this table is what the
# blueprint comparison means.
PRODUCTION = {
    "t_design": -30, "flow_design": 77, "curve_base": 25, "b": 0.78,
    "flow_min": 25, "flow_min_on": 36, "flow_max": 67,
    "t_off": 23.0, "t_on": 19.0,
    "lux_low": 10000, "lux_high": 40000, "lux_max_offset": 5.0,
    "solar_charge": 0.15, "solar_halflife": 25, "lux_mult": 1.4,
    "demand_neutral": 0, "demand_rate": 3.8, "demand_exponent": 0.35,
    "demand_max_p_offset": 10,
    "min_change": 1, "off_sentinel": 0.0,
}


class BlueprintRef:
    """Faithful transcription of boiler_weather_compensation.yaml.

    Jinja rounding kept on sight (lux round(0), base_flow round(1),
    lux_fraction round(3), P-offset round(1), combine round(1), target
    round(0)/int). State mirrors the HA helpers: the solar accumulator
    helper stores the CLAMPED offset (the blueprint writes solar_offset
    back), the mode/setpoint mirror the boiler's current state.
    """

    LN2 = 0.693147

    def __init__(self, p):
        self.p = p
        self.accum = 0.0        # input_number: solar_accumulator_helper
        self.mode = "off"
        self.setpoint = 0.0     # number mode current_setpoint (float(0))

    def step(self, t_out, lux_raw, demand_raw, dt_min):
        p = self.p
        out = {}
        lux = round(float(lux_raw) * float(p["lux_mult"]))
        out["lux"] = lux

        # Heat curve
        if t_out >= p["t_off"]:
            bf = float(p["curve_base"])
        elif t_out <= p["t_design"]:
            bf = float(p["flow_design"])
        else:
            frac = (p["t_off"] - t_out) / (p["t_off"] - p["t_design"])
            bf = round(p["curve_base"] + (p["flow_design"] - p["curve_base"])
                       * (frac ** p["b"]), 1)
        out["base_flow"] = bf

        # Demand P-term (power law, clamped)
        dev = demand_raw - p["demand_neutral"]
        if dev == 0:
            dpo = 0.0
        else:
            sgn = 1 if dev > 0 else -1
            raw_off = sgn * p["demand_rate"] * (abs(dev) ** p["demand_exponent"])
            dpo = round(min(max(-p["demand_max_p_offset"], raw_off),
                            p["demand_max_p_offset"]), 1)
        out["demand_p_offset"] = dpo

        # Solar ODE (exact solution, temperature-scaled decay)
        if lux <= p["lux_low"]:
            lf = 0.0
        elif lux >= p["lux_high"]:
            lf = 1.0
        else:
            lf = round((lux - p["lux_low"])
                       / (p["lux_high"] - p["lux_low"]), 3)
        out["lux_fraction"] = lf
        temp_factor = max(0.5, (20.0 - t_out) / (20.0 - 5.0))
        k = self.LN2 / p["solar_halflife"] * temp_factor
        a_eq = p["solar_charge"] * lf / k
        decay = 2.0 ** (-k * dt_min / self.LN2)
        new_val = a_eq + (self.accum - a_eq) * decay
        solar = round(max(0.0, min(new_val, p["lux_max_offset"])), 1)
        self.accum = solar          # the helper stores the CLAMPED value
        out["solar_offset"] = solar

        # Combine + clamp
        adj = round(bf + dpo, 1)
        target_raw = round(adj - solar, 1)
        target = int(round(min(max(target_raw, p["flow_min"]), p["flow_max"])))
        out["target_raw"] = target_raw
        out["target"] = target

        # Hysteresis (dual dead zone + demand gate)
        should_off = t_out >= p["t_off"] or target_raw <= p["flow_min"]
        should_on = (t_out < p["t_on"] and target_raw > p["flow_min_on"]
                     and demand_raw > p["demand_neutral"])
        mode = "off" if should_off else ("heat" if should_on else self.mode)

        # Rate limiting (number mode; off value = the firmware sentinel)
        mode_change = mode != self.mode
        change_big = abs(target - int(self.setpoint)) >= int(p["min_change"])
        in_dead_zone = not should_off and not should_on
        dead_zone_hold = in_dead_zone and self.mode == "off"
        needs_off = (dead_zone_hold
                     and int(self.setpoint) != int(p["off_sentinel"]))
        update = needs_off or (not dead_zone_hold
                               and (mode_change or change_big))
        if not update:
            action = "skip"
        elif mode == "off":
            action = "off"
            self.setpoint = p["off_sentinel"]
        else:
            action = "heat"
            self.setpoint = target
        self.mode = mode
        out["action"] = action
        out["mode"] = mode
        return out

    def debug_line(self, d):
        """Human-readable line mirroring the blueprint's debug notification."""
        return ("Lux=%s BaseFlow=%s P=%s Solar=%s(lux_frac=%s) "
                "Raw=%s Target=%s Mode=%s Action=%s"
                % (d["lux"], d["base_flow"], d["demand_p_offset"],
                   d["solar_offset"], d["lux_fraction"], d["target_raw"],
                   d["target"], d["mode"], d["action"]))


class ProductionCcu3:
    """One fake CCU3: HmIP-SWO weather + a 7-room HEATING+WTH house.

    Values are mutable mid-scenario (set_weather / set_room) so a whole day
    can run through one discovery.
    """

    def __init__(self, rooms, weather):
        self.session = "sid"
        self.devices, self.rooms, self.values = {}, {}, {}
        self._room_group = {}
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
            self.rooms[room_id] = {"id": room_id, "name": name,
                                   "channelIds": [str(1000 + i),
                                                  str(2000 + i)]}
            self._room_group[i] = group
            if sp is not None:
                self.values[("VirtualDevices", group,
                             "SET_POINT_TEMPERATURE")] = "%.2f" % sp
            if act is not None:
                self.values[("VirtualDevices", group,
                             "ACTUAL_TEMPERATURE")] = "%.2f" % act
        self.devices["300"] = {"id": "300", "type": "HmIP-SWO-PL",
                               "interface": "HmIP-RF", "address": "WS1",
                               "name": "Weather", "channels": [{"id": "3000"}]}
        self.set_weather(weather)
        self.methods = []

    # scenario mutation ------------------------------------------------------
    def set_weather(self, values):
        for key, value in values.items():
            self.values[("HmIP-RF", "WS1", key)] = value

    def set_room(self, index, setpoint=None, actual=None):
        group = self._room_group[index]
        for key, value in (("SET_POINT_TEMPERATURE", setpoint),
                           ("ACTUAL_TEMPERATURE", actual)):
            if value is None:
                self.values.pop(("VirtualDevices", group, key), None)
            else:
                self.values[("VirtualDevices", group, key)] = "%.2f" % value

    # JSON-RPC surface --------------------------------------------------------
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


class E2EConfig:
    """App-config stand-in: production calibration + source wiring."""

    def __init__(self, **overrides):
        self.values = dict(PRODUCTION)
        self.values.update({
            "ccu3_url": "http://ccu/api/homematic.cgi",
            "ccu3_user": "u", "ccu3_pass": "p",
            "ccu3_weather_type": "HmIP-SWO",
            "ccu3_poll_s": 0,          # re-poll every read (scenario control)
            "room_source": "heating_groups",
            "demand_delta_cap": 3.0, "rooms_poll_s": 300,
            "demand_enabled": True,
        })
        self.values.update(overrides)

    def get(self, key):
        return self.values[key]


class _FakeState:
    def __init__(self):
        self.heating_on = False
        self.candidate_boot = False
        self.post_failed = False
        self.saved = []

    def set_heating_on(self, value):
        self.saved.append(bool(value))
        self.heating_on = bool(value)


DEMANDING_HOUSE = [   # the production mix: total delta 2.2 over 7 rooms
    ("Kitchen", 22.0, 20.0),      # delta 2.0
    ("Kids", 20.5, 21.6),         # no
    ("KidsS", 21.5, 21.4),        # delta 0.1
    ("Bedroom", 21.5, 21.4),      # delta 0.1
    ("Garage", 5.0, 20.8),        # OFF mode: skipped
    ("Attic", 30.0, 18.5),        # ON mode: skipped
    ("Living", 22.0, 22.3),       # no
]
SATISFIED_HOUSE = [(name, 21.0, 21.0) for name, _sp, _a in DEMANDING_HOUSE]


class Stack:
    """The real pipeline: fake CCU3 -> sources -> controller -> transport."""

    def __init__(self, rooms, weather, **cfg_overrides):
        self.tmp = tempfile.TemporaryDirectory()  # kept: no implicit cleanup
        self.fake = ProductionCcu3(rooms, weather)
        cfg = E2EConfig(**cfg_overrides)
        self.weather = Ccu3SensorSource(
            cfg, cache_path=str(Path(self.tmp.name) / "w.json"),
            http_post=self.fake, sleep=lambda _s: None)
        self.rooms = HeatingGroups(
            cfg, cache_path=str(Path(self.tmp.name) / "r.json"),
            http_post=self.fake)
        self.controller = Controller(initial_heating_on=False)
        self.controller.has_demand_sensor = True
        self.transport = LogTransport(DummyOTGW())
        self.state = _FakeState()
        self.params = dict(PRODUCTION)

    async def refresh_rooms(self):
        await self.rooms.read()

    async def tick(self, now_ms):
        """One control tick exactly as main.py wires it (weather read +
        cached rooms demand + run_control_tick)."""
        t_out, lux, _ = await self.weather.read()
        agg = self.rooms.last()
        demand = agg["demand_pct"] if agg else None

        def read_sensors():
            return (t_out, lux, demand)

        return run_control_tick(self.controller, self.transport, self.state,
                                self.params, read_sensors, now_ms)


class FullStackDayTests(unittest.TestCase):
    """A scripted day through the FULL stack, compared to BlueprintRef."""

    def setUp(self):
        self.stack = Stack(DEMANDING_HOUSE,
                           {"ACTUAL_TEMPERATURE": "2.0",
                            "ILLUMINATION": "0"})
        self.bp = BlueprintRef(PRODUCTION)

    def compare(self, decision, bp, msg):
        """Firmware Decision vs blueprint debug dict, tick by tick."""
        self.assertEqual(bp["action"], decision.action, msg)
        self.assertAlmostEqual(bp["base_flow"], decision.base_flow, delta=0.05,
                               msg=msg + " base_flow: " + self.bp.debug_line(bp))
        self.assertAlmostEqual(bp["demand_p_offset"], decision.demand_offset,
                               delta=0.05, msg=msg + " demand_p")
        self.assertAlmostEqual(bp["solar_offset"], decision.solar_offset,
                               delta=0.05, msg=msg + " solar")
        if decision.action == "heat":
            self.assertEqual(bp["target"], decision.target,
                             msg + " target: " + self.bp.debug_line(bp))

    def test_day_sequence_matches_blueprint_and_boiler(self):
        run(self._day_sequence())

    async def _day_sequence(self):
        st = self.stack
        await st.refresh_rooms()

        # T0 06:00 cold + dark, rooms demanding -> ON at the golden 59 degC.
        # Hand-computed (production calibration): base 50.258 + P 8.647
        # (demand 10.476%) - solar 0 = 58.905 -> 59.
        d = await st.tick(0)
        self.assertEqual(d.action, "heat")
        self.assertEqual(d.target, 59)
        self.assertAlmostEqual(d.base_flow, 50.258, places=3)
        self.assertAlmostEqual(d.demand_offset, 8.647, places=3)
        self.assertEqual(d.solar_offset, 0.0)
        self.compare(d, self.bp.step(2.0, 0.0, 10.476190476, 0.0), "T0")

        # T1 +10 min unchanged -> rate-limited skip on both sides.
        d = await st.tick(600_000)
        self.assertEqual(d.action, "skip")
        self.compare(d, self.bp.step(2.0, 0.0, 10.476190476, 10.0), "T1")

        # T2 +20 min sun arrives: raw 30000 -> effective 42000 (x1.4) ->
        # lux_fraction 1.0 -> solar 1.276 after 10 min -> target 58.
        st.fake.set_weather({"ACTUAL_TEMPERATURE": "2.0",
                             "ILLUMINATION": "30000"})
        d = await st.tick(1_200_000)
        self.assertEqual(d.action, "heat")
        self.assertEqual(d.target, 58)
        self.assertAlmostEqual(d.solar_offset, 1.276, places=2)
        self.compare(d, self.bp.step(2.0, 30000.0, 10.476190476, 10.0), "T2")

        # T3 +30 min warm evening (24 >= t_off 23) + satisfied rooms ->
        # OFF with override release.
        st.fake.set_weather({"ACTUAL_TEMPERATURE": "24.0",
                             "ILLUMINATION": "0"})
        for i in range(len(DEMANDING_HOUSE)):
            st.fake.set_room(i, 21.0, 21.0)
        await st.refresh_rooms()
        d = await st.tick(1_800_000)
        self.assertEqual(d.action, "off")
        self.assertTrue(d.release_override)
        bp = self.bp.step(24.0, 0.0, 0.0, 10.0)
        self.assertEqual(bp["action"], "off")
        self.assertAlmostEqual(bp["solar_offset"], d.solar_offset, delta=0.05)

        # T4 still off: firmware skips (one-time reset already done); the
        # blueprint re-writes its legacy off setpoint every tick -- same end
        # state, documented divergence.
        d = await st.tick(2_400_000)
        self.assertEqual(d.action, "skip")
        bp = self.bp.step(24.0, 0.0, 0.0, 10.0)
        self.assertIn(bp["action"], ("skip", "off"))

        # The boiler wire truth: the exact OTGW-level API sequence.
        self.assertEqual(st.transport.commands, [
            ("set_heating", True),
            ("set_flow_target", 59),
            ("set_heating", True),      # heat decisions re-assert CH
            ("set_flow_target", 58),
            ("set_heating", False),
            ("release_override",),
        ])
        self.assertIsNone(st.transport.read_setpoint())  # nothing held
        self.assertEqual(st.state.saved[-1], False)      # latch persisted off

    def test_lux_mult_changes_the_boiler_setpoint_end_to_end(self):
        """P1 regression net: with the same sky, lux_mult 1.4 vs 1.0 must
        produce DIFFERENT boiler setpoints (56 vs 57 at this operating
        point) -- a dead lux_mult silently collapses that difference."""
        run(self._lux_mult_differential())

    async def _lux_mult_differential(self):
        for mult, want_lux, want_cs in ((1.4, 42000, 56), (1.0, 30000, 57)):
            st = Stack(DEMANDING_HOUSE,
                       {"ACTUAL_TEMPERATURE": "2.0",
                        "ILLUMINATION": "30000"}, lux_mult=mult)
            bp = BlueprintRef(dict(PRODUCTION, lux_mult=mult))
            await st.refresh_rooms()
            d = await st.tick(0)                    # ON (solar starts at 0)
            self.assertEqual((d.action, d.target), ("heat", 59))
            bp.step(2.0, 30000.0, 10.476190476, 0.0)
            d = await st.tick(1_800_000)            # 30 min of sun
            self.assertEqual(d.action, "heat")
            self.assertEqual(d.target, want_cs,
                             "lux_mult %s: CS must be %s" % (mult, want_cs))
            self.assertEqual(st.weather.last()[1], want_lux)
            self.compare(d, bp.step(2.0, 30000.0, 10.476190476, 30.0),
                         "mult=%s" % mult)


class BlueprintMathTests(unittest.TestCase):
    """Piecewise: firmware math == blueprint math, given the SAME state."""

    def test_heat_curve_matches_blueprint_rounding(self):
        p = PRODUCTION
        ref = BlueprintRef(p)
        for t_out in (-35, -30, -12.5, 2.0, 15.0, 22.9, 23.0, 30):
            d = ref.step(t_out, 0.0, p["demand_neutral"], 0.0)
            from control.heat_curve import base_flow
            fw = base_flow(t_out, p["t_off"], p["t_design"],
                           p["flow_design"], p["curve_base"], p["b"])
            self.assertAlmostEqual(d["base_flow"], fw, delta=0.05,
                                   msg="t_out=%s" % t_out)

    def test_solar_ode_matches_blueprint_given_same_accum(self):
        p = PRODUCTION
        # Feed the reference EFFECTIVE lux: neutralize its lux_mult so both
        # sides start from the identical lux picture.
        ref_params = dict(PRODUCTION, lux_mult=1.0)
        for lux_eff, t_out, accum, dt in (
                (0, 2.0, 0.0, 10.0), (42000, 2.0, 0.0, 10.0),
                (28000, 10.0, 3.0, 30.0), (12000, -8.0, 5.0, 15.0),
                (40000, 24.0, 8.0, 30.0)):
            fw_accum, fw_off = solar_step(accum, t_out, lux_eff,
                                          int(dt * 60_000), 0, p)
            ref = BlueprintRef(ref_params)
            ref.accum = accum
            d = ref.step(t_out, lux_eff, p["demand_neutral"], dt)
            self.assertAlmostEqual(d["solar_offset"], fw_off, delta=0.05,
                                   msg="%s/%s/%s/%s" % (lux_eff, t_out,
                                                        accum, dt))

    def test_demand_p_term_matches_blueprint(self):
        p = PRODUCTION
        ref = BlueprintRef(p)
        from control.demand_p import demand_offset
        for demand in (0.0, 0.4, 5.0, 10.476190476, 50.0, 100.0):
            d = ref.step(10.0, 0.0, demand, 0.0)
            fw = demand_offset(demand, p, True)
            self.assertAlmostEqual(d["demand_p_offset"], fw, delta=0.05,
                                   msg="demand=%s" % demand)

    def test_solar_accumulator_storage_policy_divergence_documents_it(self):
        # KNOWN divergence (flagged for the owner, PLAN.md): the blueprint
        # stores the CLAMPED accumulator (helper gets solar_offset), the
        # firmware keeps the raw ODE value and clamps only the offset.
        # During sustained sun both sit at the 5.0 cap; after sunset the
        # firmware's offset stays pinned ~20 min longer, then decays from
        # higher -- up to ~0.7 degC more solar credit an hour into darkness.
        p = PRODUCTION
        ref = BlueprintRef(dict(PRODUCTION, lux_mult=1.0))  # effective-lux feed
        fw_accum = 0.0
        t = 0
        for _ in range(12):                       # 2 h of full sun at 10 C
            t += 600_000
            fw_accum, fw_off = solar_step(fw_accum, 10.0, 40000.0, t,
                                          t - 600_000, p)
            ref.step(10.0, 40000.0, p["demand_neutral"], 10.0)
            # delta 0.3: the blueprint re-seeds its ODE from the ROUNDED
            # helper value every step, so its rounding drift accumulates.
            self.assertAlmostEqual(fw_off, ref.accum, delta=0.3)
        self.assertAlmostEqual(fw_off, 5.0, places=1)           # both capped
        self.assertGreater(fw_accum, 7.0)         # firmware kept the raw peak
        for _ in range(6):                        # 1 h of darkness
            t += 600_000
            fw_accum, fw_off = solar_step(fw_accum, 10.0, 0.0, t,
                                          t - 600_000, p)
            ref.step(10.0, 0.0, p["demand_neutral"], 10.0)
        self.assertLessEqual(ref.accum, 5.0)      # blueprint clamped it
        self.assertGreater(fw_off, ref.accum + 0.3,
                           "divergence must stay visible: fw=%s bp=%s"
                           % (fw_off, ref.accum))


if __name__ == "__main__":
    unittest.main()
