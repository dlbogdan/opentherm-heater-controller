"""Dummy OTGW driver: mirrors the gateway's commands and 1-minute rule.

Emits the same OTGW ``CS`` (control setpoint) / ``CH`` (CH enable) commands
plus acknowledgements a real ``OTGWTransportDrv`` would, through the audit
trace sink -- so the ring contents are identical once the real driver lands.
Off is the documented OTGW sequence (AGENTS.md, OTGW section): ``CS=0`` then
``CH=0``.

It also mirrors the gateway's **re-assert rule**: a control setpoint of
>= 8 degC expires after about a minute unless re-asserted (standalone mode
reverts it to 0). While an active setpoint is held, ``tick()`` re-asserts it
on the configured cadence (``reassert_s``; app-config ``cs_reassert_s``).
The opt-in expiry simulation (``simulate_expiry``) lets host tests prove the
re-assert keeps the setpoint held -- the exact hazard a real gateway would
impose. A real driver's re-assert is blind (no read-back needed on the
cadence); ``read_setpoint()`` is the read-back reconciliation hook.
"""

from transport.audit import OTGW_CH, OTGW_CS, ticks_ms
from transport.dummy_base import DummyTransportBase


# >= 8 degC the OTGW treats the CS as an *active* setpoint: it must be
# re-asserted at least every minute (AGENTS.md, OTGW section).
ACTIVE_CS_MIN = 8.0


def _cs_value(temp_c):
    try:
        return int(round(float(temp_c) * 10.0)) & 0xFFFF
    except (TypeError, ValueError):
        return 0


def elapsed_ms(started_ms, now_ms):
    """Milliseconds between two timestamps (MicroPython ticks or plain ints).

    ``None`` when there is no starting point (nothing was ever asserted).
    """
    if started_ms is None:
        return None
    try:
        import time
        if hasattr(time, "ticks_diff"):
            return max(0, time.ticks_diff(int(now_ms), int(started_ms)))
    except (ImportError, OSError):
        pass
    return max(0, int(now_ms) - int(started_ms))


class DummyOTGW(DummyTransportBase):
    """Simulated OTGW: CH/CS + ack trace events, sub-minute CS re-assert."""

    def __init__(self, reassert_s=30, fail_next=False,
                 simulate_expiry=False, expiry_s=60, clock=None):
        super(DummyOTGW, self).__init__(fail_next=fail_next)
        self._reassert_ms = int(reassert_s) * 1000
        self._simulate_expiry = bool(simulate_expiry)
        self._expiry_ms = int(expiry_s) * 1000
        self._clock = clock if clock is not None else ticks_ms
        # Re-assert state machine: the last successful assert's timestamp and
        # whether the (simulated) gateway reverted a stale setpoint.
        self._last_assert_ms = None
        self._gateway_reverted = False

    @property
    def reassert_interval_s(self):
        """Cadence the driver maintains between CS re-asserts (0 = none)."""
        return int(self._reassert_ms // 1000)

    def _command(self, cmd, value, then_ack, error=None):
        """Emit one gateway command (plus ack) or one error to the sink."""
        if self._sink is None:
            return
        self._sink.record_otgw_command(cmd, value)
        if then_ack:
            self._sink.record_otgw_ack(cmd)
        else:
            self._sink.record_error(error)

    # -- writes (OTGW CH / CS commands) --------------------------------------
    def set_heating(self, on):
        self._heating = bool(on)
        if self._fail_next:
            self._fail_next = False
            self._command(OTGW_CH, 1 if on else 0, then_ack=False,
                          error=self._last_error)
            return False
        self._last_error = 0
        self._command(OTGW_CH, 1 if on else 0, then_ack=True)
        return True

    def set_flow_target(self, temp_c):
        self._setpoint = float(temp_c)
        if self._fail_next:
            self._fail_next = False
            self._command(OTGW_CS, _cs_value(temp_c), then_ack=False,
                          error=self._last_error)
            return False
        self._last_error = 0
        self._command(OTGW_CS, _cs_value(temp_c), then_ack=True)
        # A successful assert starts the gateway's 1-minute window (a failed
        # one does not: nothing reached the gateway to hold).
        self._last_assert_ms = self._clock()
        self._gateway_reverted = False
        return True

    def release_override(self):
        """Off per the OTGW contract: ``CS=0`` then ``CH=0``."""
        self._heating = False
        self._setpoint = None
        self._last_assert_ms = None
        self._gateway_reverted = False
        if self._fail_next:
            self._fail_next = False
            self._command(OTGW_CS, 0, then_ack=False, error=self._last_error)
            return False
        self._last_error = 0
        self._command(OTGW_CS, 0, then_ack=True)
        self._command(OTGW_CH, 0, then_ack=True)
        return True

    # -- read-back ------------------------------------------------------------
    def read_setpoint(self):
        """The control setpoint the gateway currently holds (degC), or None.

        Mirrors reading back the CS (OpenTherm data point 1) -- the correct
        "is the setpoint still held?" check. With the expiry simulation on,
        it reports 0.0 once the gateway would have reverted a stale active
        setpoint (standalone mode reverts to 0).
        """
        if self._setpoint is None:
            return None
        if self._simulate_expiry and self._setpoint >= ACTIVE_CS_MIN:
            elapsed = elapsed_ms(self._last_assert_ms, self._clock())
            if elapsed is not None and elapsed >= self._expiry_ms:
                self._gateway_reverted = True
        if self._gateway_reverted:
            return 0.0
        return self._setpoint

    # -- driver maintenance: the sub-minute CS re-assert ----------------------
    def tick(self, now_ms):
        """Re-assert a held active (>= 8 degC) setpoint when the cadence is due.

        The (simulated) gateway side is evaluated first: a setpoint whose
        1-minute window has lapsed reverts BEFORE this driver gets a chance
        to refresh it, which is exactly the standalone OTGW behaviour.
        """
        if self._setpoint is None or self._setpoint < ACTIVE_CS_MIN:
            return None
        if self._simulate_expiry:
            elapsed = elapsed_ms(self._last_assert_ms, now_ms)
            if elapsed is not None and elapsed >= self._expiry_ms:
                self._gateway_reverted = True
        elapsed = elapsed_ms(self._last_assert_ms, now_ms)
        if elapsed is not None and elapsed >= self._reassert_ms:
            self._reassert(now_ms)
        return None

    def _reassert(self, now_ms):
        """Re-send the held CS. A failed re-assert does NOT refresh the window."""
        if self._fail_next:
            self._fail_next = False
            self._command(OTGW_CS, _cs_value(self._setpoint), then_ack=False,
                          error=self._last_error)
            return
        self._last_error = 0
        self._command(OTGW_CS, _cs_value(self._setpoint), then_ack=True)
        self._last_assert_ms = now_ms
        self._gateway_reverted = False
