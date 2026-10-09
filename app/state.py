"""Heating-mode persistence (Step 1).

Holds the one piece of control state the algorithm *cannot* recompute on its own:
``heating_on`` (arch §10.2). It is the dead-zone latch -- booting without it could
re-engage the boiler against a dead-zone hold. Everything else the pipeline needs
(solar accumulator, last-sent flow, timestamps) is derived or self-correcting, so
it deliberately does NOT survive a power cycle.

Persisted to a small ``state.json`` and written ONLY on an OFF<->HEATING
transition, using the framework's atomic temp-file + rename pattern. Device-only
(depends on the framework logger).

Boot contracts (A/B safety) -- owned by the FRAMEWORK, mirrored here:
The framework launcher (``slot_main.py``) runs the power-on self-test
(``lib/coresys/post.py``) and makes the confirm/rollback/degrade decision
BEFORE ``main()`` is called. ``main()`` then mirrors that boot context into
these runtime-only flags (never persisted) so the device layer can hold
actuation:
* **Candidate boot** (``State.candidate_boot``) -- the boot that follows an OTA
  install is supervised; ``main()`` runs briefly and the board reboots ~1-2 s
  after the framework confirms the slot. The device layer must NOT issue
  non-idempotent physical actuation (boiler ON/OFF, setpoint release) while it
  is set -- the boiler's last state persists across the confirmation reboot and
  the ACTIVE boot applies the first real decision.
* **Degraded/POST-fail** (``State.post_failed``) -- set when the framework POST
  failed on the active slot. Actuation is held, services stay up for diagnosis,
  and the app never reboots (a reboot would just repeat the same boot).
The source of truth is ``lib/coresys/post`` (``is_candidate_boot`` /
``is_degraded``); these flags are only convenient mirrors for the app.
"""

import json
import uos

import lib.coresys.logger as logger

STATE_FILE = "/state.json"


class State:
    """Tiny load/save store for the persisted heating flag."""

    def __init__(self, filename=STATE_FILE):
        self.filename = filename
        self.heating_on = False
        # Runtime-only (never persisted): True on the supervised candidate boot
        # after an OTA apply. The device layer holds physical actuation while it
        # is set; see the module docstring.
        self.candidate_boot = False
        # Runtime-only (never persisted): True when the boot-time POST failed
        # on the active slot (degraded mode). Actuation is held; the app keeps
        # its services up for diagnosis and never reboots on its own.
        self.post_failed = False
        # Runtime-only (never persisted, recomputed every control tick, P4):
        # what the loop ACTUALLY holds per sensor source --
        # {"weather": tier, "weather_age_s": s, "demand": tier,
        #  "demand_age_s": s, "cache_s": s}. This is the future UI warning
        # flag; the `sensors` shell command reports the same tiers but
        # recomputes age live at query time. RAM-only by design: a
        # per-tick flash write would violate the logging policy.
        self.data_state = None
        self._load()

    def _load(self):
        try:
            with open(self.filename, "r") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("heating_on"), bool):
                self.heating_on = data["heating_on"]
            else:
                logger.warning("State: invalid state file shape; heating_on=False.")
                self.heating_on = False
            logger.info("State: loaded heating_on=%s" % self.heating_on)
        except (OSError, ValueError) as e:
            self.heating_on = False
            logger.info("State: no/invalid state file (%s); heating_on=False." % e)

    def set_heating_on(self, value):
        """Persist a heating_on change (no-op if the value is unchanged)."""
        value = bool(value)
        if value == self.heating_on:
            return
        self.heating_on = value
        self._save()

    def _save(self):
        tmp = self.filename + ".new"
        try:
            with open(tmp, "w") as f:
                json.dump({"heating_on": self.heating_on}, f)
                f.flush()
            try:
                uos.remove(self.filename)
            except OSError:
                pass
            uos.rename(tmp, self.filename)
            logger.info("State: saved heating_on=%s" % self.heating_on)
        except Exception as e:
            logger.error("State: failed to save state: %s" % e)


# Module-level singleton shared by every part of the app.
state = State()
