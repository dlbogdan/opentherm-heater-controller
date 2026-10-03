"""Rooms/demand source boundary (arch §3.4) -- mirrors the weather source.

The control loop reads the cached heating demand (0..1) from this source. The
source is the only place that knows where the per-room setpoint/actual data
comes from (today: the Homematic CCU3 heating groups). ``last()`` is ``None``
before the first pass and for the null source, which is the defined "no room
data" input: the control loop then runs on the weather source alone.

This module is framework-free and **CCU3-free** (the CCU3 collector is imported
lazily by ``make_rooms_source``), so it is host-testable and ``main.py`` can
select the concrete source from app config exactly as it does for the weather
source (``sensors.make_sensor_source``) instead of hardwiring a specific
backend. The CCU3 collector itself lives in ``app/heating_groups.py``.
"""


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
