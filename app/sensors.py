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

    Current wiring (control loop step): only the null source exists, so every
    selection reports "no live readings yet" and the controller runs its
    defined failsafe. When the CCU3 source lands, ``ccu3``/``auto`` select it
    here -- the control loop itself does not change.
    """
    requested = config.get("t_out_source")
    if log:
        log("Sensors: t_out_source=%s -> using %s (no live readings yet)"
            % (requested, NullSensorSource.name))
    return NullSensorSource()
