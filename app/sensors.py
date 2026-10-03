"""Sensor source boundary (arch §3.4 / §3.5).

The control loop asks the source for ``(t_out, lux, demand_raw)`` on every
tick. The source is the only place that knows where the values come from
(Homematic CCU3 weather station, a local probe, ...). ``t_out is None`` is
the defined "no fresh outdoor temp" input that drives the controller into
its failsafe heat (arch §9) -- so a missing source degrades safely instead
of silently freezing the output.

This module is framework-free so it is host-testable; ``main.py`` selects
the concrete source from app config.
"""


class SensorSource(object):
    """Protocol: ``read() -> (t_out, lux, demand_raw)`` (None = unavailable)."""

    name = "sensor"

    def read(self):
        raise NotImplementedError


class NullSensorSource(SensorSource):
    """No sensor wiring yet: report no readings (controller runs failsafe)."""

    name = "null"

    def read(self):
        return (None, None, None)


def make_sensor_source(config, log=None):
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
        return Ccu3SensorSource(config, log=log)
    if log:
        log("Sensors: t_out_source=%s -> using %s (no live readings)"
            % (requested, NullSensorSource.name))
    return NullSensorSource()
