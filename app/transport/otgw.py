"""Real OTGW driver: standalone-mode control of the PIC gateway over UART.

The first real ``BoilerTransport`` (arch §2.3). The Pico talks to an OpenTherm
Gateway (PIC firmware, otgw.tclcode.com) on two GPIOs with NOTHING on the
gateway's X1 thermostat terminals (standalone mode -- how this house is
wired; AGENTS.md, OTGW section). Verified protocol facts (firmware.html +
standalone.html):

* **Commands:** two uppercase letters + ``=`` + value, ``\\r``-terminated,
  8N1 at ``otgw_baud`` (PIC gateway default 9600). ``CS=<temp>`` overrides the
  control setpoint the gateway sends in MsgID 1; ``CH=0/1`` drives the
  CH-enable bit while external control is active; ``CS=0`` ends external
  control (the gateway clears CHenable and falls back to its internal
  default). Off is therefore ``CS=0`` then ``CH=0`` -- the pinned contract.
* **Ack:** the gateway answers every command with ``<CMD>: <value>`` (e.g.
  ``CS: 45.5``) on its own line. A command counts as SENT only after its ack
  -- the driver never assumes acceptance without an ack (arch §9.1).
  Rejected commands answer ``NG``/``SE``/``BV``/``OR``/``NS``/``NF``/``OE``.
* **Vigilance:** a CS >= 8 degC EXPIRES at the gateway after ~1 minute unless
  re-asserted (standalone reverts it to 0). ``tick()`` re-asserts on the
  ``cs_reassert_s`` cadence AND immediately when the MsgID 1 read-back has
  drifted below the held value -- the read-back is the only "held?" check
  (``read_setpoint()``, never ``read_flow_temp()``).
* **Telemetry:** the gateway reports every OpenTherm message as ``T/B/R/A`` +
  8 hex digits (the full frame, same layout as ``opentherm.build_frame``).
  The driver folds IDs 25/28/17/0/5 into the ``read_*`` surface instead of
  polling. ``R`` lines carrying MsgID 1 (write_data) are the setpoint the
  gateway is ACTUALLY sending -- the read-back that drives drift detection.
  Gateway event lines ("Thermostat disconnected" ...) are expected in
  standalone mode: logged INFO (console only), never a fault.

I/O model: non-blocking except the bounded per-command ack wait
(``ack_timeout_ms``; a healthy gateway echoes in tens of ms). After
``max_retries`` consecutive ack failures the driver enters FAULT with a
backoff window: writes then FAST-FAIL (no multi-second stall per command --
the retry-storm hazard AGENTS.md warns about) and the re-assert pauses. That
pause IS the safe fallback in standalone mode: the gateway self-expires any
held setpoint within a minute of silence. The held *intent* survives, so the
override resumes automatically when the link recovers (a failed write already
rolled the controller's rate-limiter state back, so the control loop retries
in step with the driver).

Status lines are deliberately NOT pushed into the audit ring (the gateway
emits ~2 lines/second; the ring stays a command-level audit). PM=<id>
priority messages (fault-detail pull) are a follow-up, not part of the
pinned contract (tests/test_otgw_contract.py).
"""

from transport.audit import OTGW_CH, OTGW_CS, elapsed_ms, ticks_ms
from transport.base import (BoilerTransport, ERR_DISCONNECTED, ERR_NONE,
                           ERR_PROTOCOL, ERR_TIMEOUT, TransportHealth)
from transport.opentherm import decode_frame

# >= 8 degC the OTGW treats the CS as an *active* setpoint: it must be
# re-asserted at least every minute (AGENTS.md, OTGW section).
ACTIVE_CS_MIN = 8.0

# Gateway command-error tokens (firmware.html, "Responses").
GATEWAY_ERRORS = ("NG", "SE", "BV", "OR", "NS", "NF", "OE")

# RX line bounds: a status line is 11 bytes, an ack shorter; anything longer
# is garbage. The buffer is fixed-size -- it never grows (heap budget <1 KB).
MAX_LINE = 32
MAX_RXBUF = 128

_HEX = b"0123456789abcdefABCDEF"


def _cs_tenths(temp_c):
    """CS audit payload: tenths of a degree (decode side divides by 10)."""
    try:
        return int(round(float(temp_c) * 10.0)) & 0xFFFF
    except (TypeError, ValueError):
        return 0


def _cs_wire(temp_c):
    """CS wire value: one decimal is the gateway's own resolution."""
    try:
        return "%.1f" % float(temp_c)
    except (TypeError, ValueError):
        return "0"


class OTGWTransportDrv(BoilerTransport):
    """OTGWTransportDrv: ack-gated CS/CH writes, sub-minute re-assert, UART."""

    # demo_safe stays False (transport.base contract): a shell diagnostic may
    # never actuate a real boiler (test_otgw_contract DemoSafetyGateContract).

    def __init__(self, link, reassert_s=30, ack_timeout_ms=2000,
                 boiler_stale_ms=60000, max_retries=3, backoff_s=60,
                 clock=None, log=None, warn=None):
        """``link`` is the UART surface: ``write(bytes)``, ``readall()``.

        On the device the factory passes a ``machine.UART``; host tests pass
        a gateway simulator. Everything else is injectable so the exact
        contract is testable without hardware.
        """
        self._link = link
        self._reassert_ms = int(reassert_s) * 1000
        self._ack_timeout_ms = int(ack_timeout_ms)
        self._boiler_stale_ms = int(boiler_stale_ms)
        self._max_retries = int(max_retries)
        self._backoff_ms = int(backoff_s) * 1000
        self._clock = clock if clock is not None else ticks_ms
        self._log = log
        self._warn = warn

        # Held INTENT (what we want the gateway to hold) vs wire TRUTH.
        self._intended_cs = None      # None = released (nothing held)
        self._intended_ch = False
        self._last_assert_ms = None   # timestamp of the last ACKED CS assert
        self._wire_cs = None          # MsgID 1 value observed on the wire
        self._wire_seen_cs = False

        self._pending = None          # (wire_cmd, audit_id, sent_ms)
        self._rxbuf = bytearray()
        self._sink = None

        # Telemetry folded from status lines (no active polling).
        self._flow_temp = None
        self._return_temp = None
        self._modulation = None
        self._master_status = None    # (hb, lb) of the master status byte pair
        self._slave_status = None
        self._fault_flags = None
        self._slave_fault = False
        self._gateway_version = None

        self._last_boiler_ms = None   # last B line: is the boiler answering?

        self._last_error = ERR_NONE
        self._fail_streak = 0
        self._fault = False
        self._backoff_until_ms = 0
        self._health = TransportHealth.OK

    # -- trace sink (set by the LogTransport decorator) -----------------------
    def set_trace_sink(self, sink):
        self._sink = sink

    @property
    def reassert_interval_s(self):
        return int(self._reassert_ms // 1000)

    def last_error_code(self):
        return self._last_error

    def health(self):
        return self._health

    # -- writes (ack-gated OTGW CS / CH commands) -----------------------------
    def set_heating(self, on):
        on = bool(on)
        self._intended_ch = on
        return self._command("CH", "1" if on else "0", OTGW_CH, 1 if on else 0)

    def set_flow_target(self, temp_c):
        temp_c = float(temp_c)
        self._intended_cs = temp_c  # retry intent even if this write fails
        return self._command("CS", _cs_wire(temp_c), OTGW_CS, _cs_tenths(temp_c))

    def release_override(self):
        """Off per the pinned contract: ``CS=0`` then ``CH=0``, nothing held."""
        self._intended_cs = None
        self._intended_ch = False
        self._last_assert_ms = None
        self._wire_cs = None
        self._wire_seen_cs = False
        if not self._command("CS", "0", OTGW_CS, 0):
            return False
        return self._command("CH", "0", OTGW_CH, 0)

    def _command(self, wire_cmd, wire_value, audit_id, audit_value):
        """Send one command and wait (bounded) for its ack. True = acked."""
        now = self._clock()
        if self._fault and elapsed_ms(now, self._backoff_until_ms) > 0:
            # FAULT backoff: fast-fail. Nothing hits the wire, no blocking
            # wait -- this is what keeps a dead gateway from stalling the
            # event loop every cadence tick.
            self._last_error = ERR_DISCONNECTED
            return False
        try:
            self._link.write(("%s=%s\r" % (wire_cmd, wire_value)).encode())
        except Exception:
            self._pending = None
            self._note_failure(ERR_DISCONNECTED)
            return False
        if self._sink:
            self._sink.record_otgw_command(audit_id, audit_value)
        self._pending = (wire_cmd, audit_id, now)
        return self._await_ack(wire_cmd, now)

    def _await_ack(self, wire_cmd, sent_ms):
        """Pump the link until the matching ack (or error/timeout) lands."""
        while True:
            self._pump()
            if self._pending is None:
                return self._last_error == ERR_NONE
            if elapsed_ms(sent_ms, self._clock()) >= self._ack_timeout_ms:
                self._pending = None
                self._note_failure(ERR_TIMEOUT)
                return False

    # -- driver maintenance: pump, re-assert, reconcile, health ---------------
    def tick(self, now_ms):
        self._pump()
        if self._fault and elapsed_ms(now_ms, self._backoff_until_ms) <= 0:
            self._fault = False
            self._fail_streak = 0  # one real attempt; health proves recovery
        cs = self._intended_cs
        if cs is not None and cs >= ACTIVE_CS_MIN:
            elapsed = elapsed_ms(self._last_assert_ms, now_ms)
            due = elapsed is None or elapsed >= self._reassert_ms
            if (not due and self._wire_seen_cs and self._wire_cs is not None
                    and self._wire_cs < ACTIVE_CS_MIN):
                due = True  # read-back drift: the gateway reverted -> NOW
            if due:
                self._command("CS", _cs_wire(cs), OTGW_CS, _cs_tenths(cs))
        self._update_health(now_ms)
        return None

    def _update_health(self, now_ms):
        if self._fault:
            health = TransportHealth.FAULT
        elif (self._last_boiler_ms is None
              or elapsed_ms(self._last_boiler_ms, now_ms)
              >= self._boiler_stale_ms):
            health = TransportHealth.DEGRADED  # boiler not answering on the wire
        elif self._fail_streak:
            health = TransportHealth.DEGRADED
        else:
            health = TransportHealth.OK
        if health != self._health:
            self._health = health
            if health == TransportHealth.OK and self._log:
                self._log("OTGW: link healthy.")

    def _note_failure(self, code):
        self._last_error = code
        self._fail_streak += 1
        if self._sink:
            self._sink.record_error(code)
        if self._fail_streak >= self._max_retries and not self._fault:
            self._fault = True
            self._backoff_until_ms = (self._clock() + self._backoff_ms) & 0xFFFFFFFF
            if self._warn:
                self._warn("OTGW: %d commands unacknowledged -> FAULT; "
                           "re-assert paused (the gateway self-expires any "
                           "held setpoint)" % self._max_retries)

    # -- read-back / telemetry --------------------------------------------------
    def read_setpoint(self):
        """The control setpoint held at the gateway (degC), or None.

        Active hold (>= 8): reports the WIRE truth (ack echo / MsgID 1
        read-back) -- 0.0 once the gateway has reverted a stale setpoint.
        Sub-8 hold (incl. the off sentinel 0.0): reports the held value.
        Released: None. Never the measured flow temperature.
        """
        if self._intended_cs is None:
            return None
        if self._intended_cs >= ACTIVE_CS_MIN and self._wire_seen_cs:
            return self._wire_cs
        return self._intended_cs

    def read_flow_temp(self):
        return self._flow_temp

    def read_return_temp(self):
        return self._return_temp

    def read_modulation(self):
        return self._modulation

    # -- UART line pump + protocol parsing --------------------------------------
    def _pump(self):
        try:
            data = self._link.readall()
        except Exception:
            self._pending = None
            self._note_failure(ERR_DISCONNECTED)
            return
        if not data:
            return
        self._rxbuf.extend(data)
        buf = self._rxbuf
        start = 0
        i = 0
        n = len(buf)
        while i < n:
            byte = buf[i]
            if byte == 13 or byte == 10:  # \r or \n (responses are CRLF)
                if i - start > 0:
                    line = bytes(buf[start:i])
                    if len(line) <= MAX_LINE:
                        self._handle_line(line)
                    # over-long line: garbage, dropped whole
                if byte == 13 and i + 1 < n and buf[i + 1] == 10:
                    i += 1
                start = i + 1
            i += 1
        if start:
            # MicroPython bytearray has NO item deletion (del buf[:n] raises
            # TypeError -- verified on-device 2026-10-05; CPython allows it,
            # which is why the host suite passed). Rebind the remainder.
            self._rxbuf = buf = buf[start:]
        if len(buf) > MAX_RXBUF:
            # MicroPython bytearray has NO .clear() either (verified
            # on-device 2026-10-05): rebind instead. A stuck >128-byte
            # fragment is garbage; never grows.
            self._rxbuf = bytearray()

    def _handle_line(self, line):
        text = line.decode()  # no kwargs: this MicroPython build rejects them
        if not text:
            return
        first = text[0]
        if first in "TBRAE" and len(text) == 9:
            hex_part = text[1:]
            for char in hex_part:
                if ord(char) not in _HEX:
                    return
            if first == "B":
                self._last_boiler_ms = self._clock()
            if first != "E":  # E = parity-error report: traffic, not a frame
                self._handle_frame(first, int(hex_part, 16))
            return
        if len(text) > 3 and text[2] == ":" and text[:2] in ("CS", "CH"):
            self._handle_ack(text[:2], text[3:].strip())
            return
        if text in GATEWAY_ERRORS:
            if self._pending is not None:
                self._pending = None
                self._note_failure(ERR_PROTOCOL)
            return
        if "Thermostat" in text or "power" in text:
            # Gateway event lines; "Thermostat disconnected" is EXPECTED in
            # standalone mode. INFO (console only) -- never a fault (AGENTS.md
            # logging policy).
            if self._log:
                self._log("OTGW: %s" % text)
            return
        if "OpenTherm Gateway" in text:
            self._gateway_version = text.strip()
            if self._log:
                self._log("OTGW: %s" % text)
            return
        # Unknown line: forward-compatible gateway output, ignored.

    def _handle_ack(self, wire_cmd, value_text):
        try:
            value = float(value_text)
        except ValueError:
            return
        if wire_cmd == "CS":
            # The gateway's interpreted override -- authoritative at ack time.
            self._wire_cs = value
            self._wire_seen_cs = True
        if self._pending is not None and self._pending[0] == wire_cmd:
            audit_id = self._pending[1]
            if self._sink:
                self._sink.record_otgw_ack(audit_id)
            self._pending = None
            self._last_error = ERR_NONE
            self._fail_streak = 0
            if wire_cmd == "CS":
                # Only a SUCCESSFUL assert starts the gateway's 1-minute
                # window (a failed one never reached the gateway to hold).
                self._last_assert_ms = self._clock()

    def _handle_frame(self, direction, frame):
        decoded = decode_frame(frame)
        if not decoded["parity_ok"]:
            return
        data_id = decoded["data_id"]
        message_type = decoded["message_type"]
        if data_id == 1 and message_type == "write_data" and direction in ("T", "R"):
            # The control setpoint the gateway is actually sending to the
            # boiler -- the "is it still held?" read-back.
            self._wire_cs = decoded["value"]
            self._wire_seen_cs = True
        elif data_id == 25 and message_type == "read_ack":
            self._flow_temp = decoded["value"]
        elif data_id == 28 and message_type == "read_ack":
            self._return_temp = decoded["value"]
        elif data_id == 17 and message_type == "read_ack":
            self._modulation = decoded["value"]
        elif data_id == 0:
            if message_type == "read_ack":  # slave status (boiler -> gateway)
                self._slave_status = (decoded["hb"], decoded["lb"])
                fault = bool(decoded["lb"] & 0x1)
                if fault and not self._slave_fault and self._warn:
                    self._warn("OTGW: boiler fault flag set (MsgID 0 slave LB)")
                self._slave_fault = fault
            elif message_type == "read_data":  # master status we are sending
                self._master_status = (decoded["hb"], decoded["lb"])
        elif data_id == 5 and message_type == "read_ack":
            self._fault_flags = (decoded["hb"], decoded["lb"])
