"""Sensor source boundary (arch §3.4 / §3.5).

The control loop asks the source for ``(t_out, lux, demand_raw)`` on every
tick. The source is the only place that knows where the values come from
(Homematic CCU3 weather station, a local probe, ...). ``t_out is None`` is
the defined "no fresh outdoor temp" input that drives the controller into
its failsafe heat (arch §9) -- so a missing source degrades safely instead
of silently freezing the output.

This module is framework-free so it is host-testable; ``main.py`` selects
the concrete source from app config.

P4 adds the shared freshness tiers (``sensor_cache_s`` is ONE expiry for
ALL sensor caches -- owner decision 2026-10-09) and ``weather_tier``,
which derives what the control loop ACTUALLY holds from the source's own
timestamp stamp. The tier feeds ``state.data_state`` (the future UI
warning flag) -- never a separately maintained boolean.
"""

from control.util import elapsed_ms

# Freshness tiers (shared by every sensor source; strings so they ride
# straight into the `sources` JSON / UI).
TIER_FRESH = "fresh"      # polled within its own cadence
TIER_CACHED = "cached"    # last-known value, still steering + UI flag
TIER_EXPIRED = "expired"  # beyond sensor_cache_s: controller failsafes
TIER_NONE = "none"        # no reading ever taken / no source configured

FRESH_FACTOR = 2  # fresh window = 2 x the source's poll interval


def weather_tier(values, now_ms, poll_s, cache_s):
    """``(tier, age_s)`` for a cached ``(t_out, lux, ts)`` reading.

    Wrap-safe (``control.util.elapsed_ms``); a negative elapsed (clock
    anomaly) counts as fresh, mirroring the weather staleness guard.
    """
    if not values or values[2] is None:
        return (TIER_NONE, None)
    age = elapsed_ms(values[2], now_ms)
    if age < 0:
        age = 0
    age_s = age // 1000
    if age <= FRESH_FACTOR * int(poll_s) * 1000:
        return (TIER_FRESH, age_s)
    if age <= int(cache_s) * 1000:
        return (TIER_CACHED, age_s)
    return (TIER_EXPIRED, age_s)


class SensorSource(object):
    """Protocol: ``await read() -> (t_out, lux, demand_raw)`` (None = unavailable).

    ``read`` is a coroutine so a source that does I/O (the CCU3) never blocks
    the board's event loop; the null source returns immediately.
    """

    name = "sensor"

    async def read(self):
        raise NotImplementedError


class NullSensorSource(SensorSource):
    """No sensor wiring yet: report no readings (controller runs failsafe)."""

    name = "null"

    async def read(self):
        return (None, None, None)


def make_sensor_source(config, log=None, warn=None):
    """Select the sensor source from app config (arch §3.5).

    ``ccu3``/``auto`` use the CCU3 weather station when ``ccu3_url`` is set
    (``auto`` falls back to the null source without one); ``local`` is the
    future wired-probe source and the null source is the explicit safe
    default. Whatever is selected, ``t_out=None`` is the defined failsafe
    input, so a dead source degrades the controller safely.
    """
    requested = config.get("t_out_source")
    if requested in ("ccu3", "auto") and config.get("ccu3_url"):
        from ccu3 import Ccu3SensorSource
        if log:
            log("Sensors: t_out_source=%s -> CCU3 at %s"
                % (requested, config.get("ccu3_url")))
        return Ccu3SensorSource(config, log=log, warn=warn)
    if log:
        log("Sensors: t_out_source=%s -> using %s (no live readings)"
            % (requested, NullSensorSource.name))
    return NullSensorSource()
