"""Rooms/demand source boundary (arch §3.4) -- mirrors the weather source.

The control loop reads the cached heating demand (0..100 percent) from this
source. The source is the only place that knows where the per-room setpoint/
actual data comes from (today: the Homematic CCU3 heating groups). ``last()``
is ``None`` before the first pass and for the null source, which is the
defined "no room data" input: the control loop then runs on the weather
source alone.

This module is framework-free and **CCU3-free** (the CCU3 collector is imported
lazily by ``make_rooms_source``), so it is host-testable and ``main.py`` can
select the concrete source from app config exactly as it does for the weather
source (``sensors.make_sensor_source``) instead of hardwiring a specific
backend. The CCU3 collector itself lives in ``app/heating_groups.py``.

P4 adds ``pick_demand``: the WHOLE demand selection (fresh / cached /
expired against the shared ``sensor_cache_s`` expiry), host-pinned here so
the device glue in ``main.py`` stays a one-liner.
"""

from control.util import elapsed_ms
from sensors import TIER_CACHED, TIER_EXPIRED, TIER_FRESH, TIER_NONE


class RoomsSource(object):
    """Protocol: ``await read()`` (refresh the cached aggregate);
    ``last() -> aggregate | None`` (sync, for the control loop + shell).

    ``enabled`` is ``False`` and ``poll_s`` is ``0`` for the null source, so
    the device layer schedules no periodic pass for it.
    """

    enabled = False
    poll_s = 0

    async def read(self):
        raise NotImplementedError

    def last(self):
        raise NotImplementedError


class NullRoomsSource(RoomsSource):
    """No room-data source configured: ``last()`` is always None (demand off)."""

    enabled = False
    poll_s = 0

    async def read(self):
        return None

    def last(self):
        return None


def make_rooms_source(config, log=None, warn=None):
    """Select the rooms/demand source from app config (arch §3.4).

    When ``ccu3_url`` is set, use the CCU3 heating-group collector (its
    ``room_source`` key chooses heating_groups vs eTRV). Without one, return
    the null source so demand is simply disabled -- the weather source still
    drives the control loop, so a missing room source degrades safely.
    """
    if config.get("ccu3_url"):
        from heating_groups import HeatingGroups
        if log:
            log("Rooms: source -> CCU3 heating groups at %s"
                % config.get("ccu3_url"))
        return HeatingGroups(config, log=log, warn=warn)
    if log:
        log("Rooms: source -> none (no ccu3_url; demand disabled)")
    return NullRoomsSource()


def pick_demand(agg, now_ms, poll_s, cache_s):
    """``(demand_pct, tier, age_s)`` -- the whole demand selection (P4).

    * fresh (age <= 2 x ``poll_s``): the value steers.
    * cached (age <= ``sensor_cache_s``): the last-known value STILL
      steers -- room setpoints/actuals move slowly -- and the tier flags
      it for the UI (owner decision 2026-10-09).
    * expired / missing / no source: value is ``None`` -- the controller's
      defined "no sensor" input (zero offset, permissive gate), so stale
      data can never suppress or fake-engage heat past the cache window.

    Wrap-safe via ``control.util.elapsed_ms``; a negative elapsed (clock
    anomaly) counts as fresh, mirroring the weather staleness guard. A
    fresh/cached aggregate whose ``demand_pct`` is None (empty pass)
    yields value None with the tier kept -- the controller treats it as
    no-sensor either way.
    """
    if not agg:
        return (None, TIER_NONE, None)
    ts = agg.get("ts")
    if ts is None:
        return (None, TIER_EXPIRED, None)
    age = elapsed_ms(ts, now_ms)
    if age < 0:
        age = 0
    age_s = age // 1000
    value = agg.get("demand_pct")
    if age <= 2 * int(poll_s) * 1000:
        return (value, TIER_FRESH, age_s)
    if age <= int(cache_s) * 1000:
        return (value, TIER_CACHED, age_s)
    return (None, TIER_EXPIRED, age_s)
