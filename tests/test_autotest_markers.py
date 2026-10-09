"""Host tests for the autotest log markers (app/shell_commands.py).

The proof suites deliberately inject WARN-level faults, so /log.txt can
look like a failing board while tests run. AutotestMarkerShell brackets
``test run`` with start/end markers (and only that -- ``test list`` and
the usage line inject nothing), so test evidence is distinguishable from
field events by reading the log alone.
"""

import asyncio
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from shell_commands import AutotestMarkerShell  # noqa: E402


class FakeShell:
    def __init__(self):
        self.registered = {}

    def add(self, command, handler, description="", **kwargs):
        self.registered[command] = (handler, description, kwargs)


class FakeRunner:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def handle(self, args="", emit=None):
        self.calls.append(args)
        if self.fail:
            raise RuntimeError("suite blew up")
        return "RESULT: 1 passed"


class AutotestMarkerTests(unittest.TestCase):
    def setUp(self):
        self.shell = FakeShell()
        self.warns = []
        self.marker = AutotestMarkerShell(self.shell, self.warns.append)

    def register(self, runner, **kwargs):
        self.marker.add("test", runner.handle, "run suites", **kwargs)
        return self.shell.registered["test"][0]

    def test_run_is_bracketed_and_result_passes_through(self):
        handler = self.register(FakeRunner())
        out = asyncio.run(handler("run config", None))
        self.assertEqual(out, "RESULT: 1 passed")
        self.assertEqual(len(self.warns), 2)
        self.assertIn("TESTING IN PROGRESS", self.warns[0])
        self.assertEqual(self.warns[1], "Autotests: testing ended")

    def test_streaming_declaration_passes_through(self):
        self.register(FakeRunner(), streaming=True)
        self.assertEqual(self.shell.registered["test"][2], {"streaming": True})

    def test_list_and_usage_are_not_bracketed(self):
        handler = self.register(FakeRunner())
        asyncio.run(handler("list"))
        asyncio.run(handler(""))
        self.assertEqual(self.warns, [])

    def test_end_marker_lands_even_when_the_runner_raises(self):
        handler = self.register(FakeRunner(fail=True))
        with self.assertRaises(RuntimeError):
            asyncio.run(handler("run everything", None))
        self.assertEqual(len(self.warns), 2)
        self.assertEqual(self.warns[1], "Autotests: testing ended")

    def test_other_commands_pass_through_untouched(self):
        def rooms_handler(args):
            return "x"
        self.marker.add("rooms", rooms_handler, "rooms help")
        self.assertIs(self.shell.registered["rooms"][0], rooms_handler)


if __name__ == "__main__":
    unittest.main()
