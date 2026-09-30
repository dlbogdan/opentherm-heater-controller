"""Heating-mode persistence (Step 1).

Holds the one piece of control state the algorithm *cannot* recompute on its own:
``heating_on`` (arch §10.2). It is the dead-zone latch -- booting without it could
re-engage the boiler against a dead-zone hold. Everything else the pipeline needs
(solar accumulator, last-sent flow, timestamps) is derived or self-correcting, so
it deliberately does NOT survive a power cycle.

Persisted to a small ``state.json`` and written ONLY on an OFF<->HEATING
transition, using the framework's atomic temp-file + rename pattern. Device-only
(depends on the framework logger).
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
