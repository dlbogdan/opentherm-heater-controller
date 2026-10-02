"""App-specific POST checks for the OpenTherm heater controller.

The framework runs the POST harness on every boot (``micropy-system``
``lib/coresys/post.py``) and calls the guest entry module's ``post_checks()``
hook. This module provides the *domain* checks; the free-heap check is the
framework's default and is not repeated here.

Each check returns a ``(name, ok, detail)`` tuple. Keep them local and fast --
the framework requires the whole POST to stay inside the A/B candidate
confirmation window. Runs on host (CPython) and device (MicroPython).
"""

import json
import sys

try:
    import uos
except ImportError:  # CPython host (testing)
    import os as uos


def _config_check(config, config_problems=None):
    """``config.validate()`` resets violating keys to defaults (see config.py)
    and returns a problems list; a clean config yields an empty list."""
    if config_problems is None:
        config_problems = config.validate()
    ok = not config_problems
    detail = "; ".join(config_problems[:2]) if config_problems else ""
    return ("config", ok, detail)


def _state_check(state):
    """The persisted state file must be readable JSON with a bool heating_on.
    A missing file is fine (fresh boot); a corrupt one is not, because the
    dead-zone latch would silently drop."""
    try:
        uos.stat(state.filename)
    except OSError:
        return ("state", True, "")  # no file yet -- fresh boot
    try:
        with open(state.filename, "r") as f:
            data = json.load(f)
        if not isinstance(data.get("heating_on"), bool):
            return ("state", False, "bad heating_on type")
    except (OSError, ValueError) as e:
        return ("state", False, str(e))
    return ("state", True, "")


def _control_core_check():
    """Reuse the 23-check pure control-core self-test (~50 ms on-device)."""
    import builtins
    for p in ("apps/a", "apps/b"):
        try:
            sys.path.insert(0, p)
        except Exception:
            pass
    captured = []
    real_print = builtins.print
    builtins.print = lambda *values, **_kw: captured.append(
        " ".join(str(value) for value in values))
    try:
        import selftest
        ok = bool(selftest.run())
    except Exception as e:
        ok = False
        captured.append("error: %s" % e)
    finally:
        builtins.print = real_print
    detail = captured[-1] if (not ok and captured) else ""
    return ("control-core", ok, detail)


def checks(config, state, config_problems=None):
    """Return the list of ``(name, ok, detail)`` domain POST checks."""
    return [
        _config_check(config, config_problems),
        _state_check(state),
        _control_core_check(),
    ]
