"""Live suite: the control-tick failure net (PLAN.md P3).

Proves the app-level containment around the WHOLE control tick fires -- the
net that turns a persistently failing tick from an invisible boiler-pinning
hazard into a greppable flash-logged WARN. The suite patches the LIVE
weather source's ``read`` (the exact singleton the control loop uses, via
``app_entry.LIVE``) to raise, waits for the next control tick to hit it,
and asserts the wrapper's WARN landed in /log.txt with OUR exception text
-- proof the caught raise is the injected one, not an unrelated failure.

Costs (deliberate, documented): ONE intentional WARN line in /log.txt per
run (flash write -- this IS the evidence), and one or two control ticks
aborted during the patch window. Safe by the net's own contract: the tick
aborts BEFORE any actuation, the independent re-assert task keeps the held
CS alive throughout (dummy transport: no real hardware behind it), and
tearDown ALWAYS restores ``read`` (the runner runs it even on timeout).

timeout_s is raised above the 60 s control_tick interval so the wait
covers the worst-case phase (patch fired just after a tick ran).
"""

import unittest

import app_entry

import uasyncio as asyncio

LOG_FILE = "/log.txt"
MARK = "control-net live demo"


class TestControlTickNet(unittest.TestCase):
    """Patch the live sensor source; prove the wrapper's warn lands."""

    timeout_s = 75  # control_tick_s is 60: cover the worst tick phase

    def setUp(self):
        self.src = app_entry.LIVE.get("sensor_source")
        if self.src is None:
            raise unittest.SkipTest("no live sensor source wired")
        self.hit = {"seen": False}

    def _boom(self):
        """The injected fault: record the hit, then raise into the net."""
        self.hit["seen"] = True
        raise RuntimeError(MARK)

    async def test_control_tick_net_fires(self):
        # Instance attr shadows the class method: the next control tick
        # calls this instead of the real async read; the raise escapes the
        # tick body and the wrapper must catch + warn (flash).
        self.src.read = self._boom
        try:
            waited_ms = 0
            while waited_ms < 70000 and not self.hit["seen"]:
                await asyncio.sleep(1.0)
                waited_ms += 1000
            self.assertTrue(
                self.hit["seen"],
                "control tick never hit the patched read within 70 s "
                "(interval is control_tick_s=%s -- task dead?)"
                % self.src._config.get("control_tick_s"))
            await asyncio.sleep(0.2)  # let the WARN land in the file
            tail = self._log_tail(keep=1500)
            self.assertIn(b"Control: tick failed", tail,
                          "wrapper WARN missing from the flash log tail")
            self.assertIn(MARK.encode(), tail,
                          "caught fault is not our injected raise")
        finally:
            self._restore()

    def _log_tail(self, keep=1500):
        """Last ``keep`` bytes of the flash log -- O(1) RAM, never the file."""
        tail = b""
        try:
            with open(LOG_FILE, "rb") as handle:
                while True:
                    chunk = handle.read(512)
                    if not chunk:
                        break
                    tail = (tail + chunk)[-keep:]
        except OSError:
            pass
        return tail

    def _restore(self):
        # Instance attr back to the class method: the live loop resumes
        # real polls on the next tick. Idempotent (tearDown may re-run it).
        self.src.read = type(self.src).read

    def tearDown(self):
        # The runner runs tearDown even when the test times out, so the
        # live app can never be left holding the patched read.
        if self.src is not None:
            self._restore()


class TestLiveSourceRestored(unittest.TestCase):
    """The patch class ran first (sorted order): prove it left no debris.

    ``read`` must resolve to the class method again -- a leftover boom in
    the live singleton would be the worst device-only regression possible.
    """

    def test_live_source_restored(self):
        src = app_entry.LIVE.get("sensor_source")
        if src is None:
            raise unittest.SkipTest("no live sensor source wired")
        self.assertIs(src.read, type(src).read,
                      "live sensor source still patched -- net suite leaked")
