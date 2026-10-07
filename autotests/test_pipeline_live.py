"""Live pipeline suite: sensors -> calculation -> boiler output (on-device).

The end-to-end counterpart of the host suite tests/test_pipeline_e2e.py:
it validates the SAME production chain against the REAL CCU3 and the LIVE
app, and prints a blueprint-comparable debug snapshot -- the numbers to
line up against the production HA blueprint's debug notification
(boiler_weather_compensation.yaml) while both run side by side.

Read-only by contract: no setValue, no actuation. The pipeline debug runs
a FRESH Controller.tick (a pure function -- zero I/O, zero writes) over
the LIVE cached sensor data; the boiler-output test only READS the live
transport's held state.

Session discipline: all CCU3 I/O goes through ccu3_live_session.get_rpc()
(one pooled login per board boot; the app's own two sessions count against
the CCU3 pool -- AGENTS.md).
"""

import json
import unittest

import uasyncio as asyncio

from ccu3 import _now_ms
from config import config
from state import state

import ccu3_live_session as session


def _live():
    """The app's LIVE wiring handle (read-only BY CONVENTION)."""
    import app_entry
    return getattr(app_entry, "LIVE", None) or {}


def _age_ms(ts):
    try:
        import time
        if hasattr(time, "ticks_diff"):
            return time.ticks_diff(_now_ms(), ts)
    except (ImportError, OSError):
        pass
    return _now_ms() - ts


def _cached_endpoint():
    """The app's discovered weather endpoint (identity from its cache)."""
    try:
        with open("/ccu3_cache.json", "r") as handle:
            data = json.load(handle)
        if data.get("interface") and data.get("address"):
            return data["interface"], data["address"]
    except (OSError, ValueError, AttributeError):
        pass
    return None


async def _raw_value(rpc, key):
    endpoint = _cached_endpoint()
    if endpoint is None:
        return None
    iface, addr = endpoint
    result = await rpc.call("Interface.getValue",
                            {"interface": iface, "address": addr + ":1",
                             "valueKey": key})
    if isinstance(result, dict):
        result = result.get("value")
    try:
        return float(result)
    except (TypeError, ValueError):
        return None


async def _wait_weather(source, timeout_s=120):
    """Bounded wait for the app's first weather poll (boot race, not a fault)."""
    deadline = _now_ms() + timeout_s * 1000
    while _now_ms() < deadline:
        if source is not None and source.last() is not None:
            return True
        await asyncio.sleep(2.0)
    return False


class TestLuxCalibrationLive(unittest.TestCase):
    """PLAN.md P1 on the REAL sensor: the controller must consume
    effective lux = round(raw * lux_mult), not the raw reading."""

    timeout_s = 150

    async def setUp(self):
        if not config.get("ccu3_url"):
            self.skipTest("no ccu3_url configured")
        if not await session.wait_wifi():
            self.skipTest("Wi-Fi did not come up")

    async def test_effective_lux_is_raw_times_mult(self):
        src = _live().get("sensor_source")
        if src is None:
            self.skipTest("no LIVE sensor_source (firmware too old?)")
        if not await _wait_weather(src):
            self.skipTest("no weather reading yet")
        t_out, lux_eff, ts = src.last()
        if lux_eff is None:
            self.skipTest("station reports no ILLUMINATION right now")
        mult = float(config.get("lux_mult"))
        # Independent RAW read of the same channel through the pooled
        # session: the app's cached lux must equal round(raw * mult). The
        # two reads are minutes apart (the app polls on ccu3_poll_s), so
        # the tolerance absorbs real sun/cloud movement, while a dead or
        # double-applied multiplier cannot hide inside it.
        rpc = await session.get_rpc()
        raw = await _raw_value(rpc, "ILLUMINATION")
        if raw is None:
            self.skipTest("ILLUMINATION unreadable")
        expected = round(raw * mult)
        tol = max(2000.0, 0.25 * expected)
        self.assertLess(abs(lux_eff - expected), tol,
                        "cached lux %s != raw %s x %s (=%s)"
                        % (lux_eff, raw, mult, expected))
        if t_out is not None:
            self.assertTrue(-40.0 < t_out < 60.0, "t_out sane: %r" % t_out)


class TestPipelineDebugLive(unittest.TestCase):
    """Calculation debug over LIVE data: a fresh pure Controller.tick on the
    app's cached weather + rooms aggregate, printed as a blueprint-style
    line and pinned self-consistent. Zero actuation (pure function)."""

    timeout_s = 250

    async def setUp(self):
        if not config.get("ccu3_url"):
            self.skipTest("no ccu3_url configured")
        if not await session.wait_wifi():
            self.skipTest("Wi-Fi did not come up")
        live = _live()
        self.src = live.get("sensor_source")
        self.rooms = live.get("rooms")
        if self.src is None or self.rooms is None:
            self.skipTest("no LIVE handles (firmware too old?)")
        if not await _wait_weather(self.src):
            self.skipTest("no weather reading yet")
        deadline = _now_ms() + 140 * 1000
        while _now_ms() < deadline and self.rooms.last() is None:
            await asyncio.sleep(2.0)

    def test_pipeline_snapshot_is_self_consistent(self):
        from control.controller import Controller
        from control.failsafe import frost_clamp
        from control.util import clamp

        vals = self.src.last()
        agg = self.rooms.last()
        if vals is None or vals[0] is None:
            self.skipTest("no fresh t_out (failsafe path, nothing to compare)")
        if agg is None or agg.get("demand_pct") is None:
            self.skipTest("no rooms demand aggregate yet")
        t_out, lux, _ts = vals
        demand = agg["demand_pct"]
        p = config.all()

        ctl = Controller(initial_heating_on=state.heating_on)
        ctl.has_demand_sensor = bool(p["demand_enabled"]) and self.rooms.enabled
        d = ctl.tick(_now_ms(), p, t_out, lux=lux, demand_raw=demand)
        if d.base_flow is None:
            self.skipTest("failsafe decision (no t_out) -- not comparable")

        # Blueprint-comparable debug line (mirror of the HA notification):
        # compare Lux/BaseFlow/P/Solar/Raw/Target against the blueprint's
        # debug notification while both systems watch the same sensors.
        print("PIPELINE t_out=%s lux=%s(x%s) base=%.2f solar=%.2f "
              "demand=%.2f%% p_off=%.2f raw=%.2f target=%s on=%s action=%s "
              "reason=%s"
              % (t_out, lux, p["lux_mult"], d.base_flow, d.solar_offset,
                 demand, d.demand_offset,
                 d.base_flow + d.demand_offset - d.solar_offset,
                 d.target, d.heating_on, d.action, d.reason))

        # Self-consistency: the emitted target MUST be the clamp of the
        # printed intermediates (plus the frost floor when very cold).
        raw = d.base_flow + d.demand_offset - d.solar_offset
        expected = int(round(clamp(raw, p["flow_min"], p["flow_max"])))
        expected = frost_clamp(expected, t_out)
        if d.action == "heat":
            self.assertEqual(d.target, expected)
        # Demand offset sign: more demand than neutral can never LOWER
        # the target, and vice versa.
        if demand > p["demand_neutral"]:
            self.assertGreaterEqual(d.demand_offset, 0.0)
        elif demand < p["demand_neutral"]:
            self.assertLessEqual(d.demand_offset, 0.0)
        else:
            self.assertEqual(d.demand_offset, 0.0)
        # Solar credit never exceeds its cap.
        self.assertTrue(0.0 <= d.solar_offset <= p["lux_max_offset"])


class TestBoilerOutputLive(unittest.TestCase):
    """The held boiler state must agree with the heating latch (no I/O)."""

    timeout_s = 30

    def test_held_setpoint_matches_heating_latch(self):
        if state.candidate_boot or state.post_failed:
            self.skipTest("actuation held (candidate/degraded boot)")
        tr = _live().get("transport")
        if tr is None:
            self.skipTest("no LIVE transport handle")
        sp = tr.read_setpoint()
        if state.heating_on:
            self.assertIsNotNone(sp, "latch ON but nothing held at the boiler")
            self.assertGreaterEqual(sp, 8.0,
                                    "active hold must be >= 8 (OTGW rule)")
        else:
            self.assertTrue(sp is None or sp < 8.0,
                            "latch OFF must hold nothing (got %r)" % sp)
