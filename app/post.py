"""Power-on self-test (POST) -- basic system health, run on every boot.

Contract (A/B safety, see ``state.py``):
  * candidate boot -- ``main()`` runs the POST BEFORE slot confirmation. A
    candidate that fails POST never confirms, so the watchdog rolls it back
    and quarantines the version. A passing candidate confirms as before.
  * active boot -- a POST failure puts the app in degraded mode: actuation is
    held (``state.post_failed``), the shell stays up for diagnosis, and the
    app never reboots on its own (rebooting would repeat the same boot).

Every check is local and fast (no network I/O, no blocking waits) so the
whole POST stays far inside the 12 s candidate window. Keep each check
under ~10 ms; anything slower belongs in the shell 'selftest' command, not
here.
"""

import gc
import json
import sys

try:
    import uos
except ImportError:  # CPython host (testing)
    import os as uos

import lib.coresys.logger as logger

# Below this free heap the slot is considered bloated/corrupt.
MIN_HEAP_FREE = 128 * 1024


class PostResult(object):
    """Collects (name, ok, detail) checks and a final verdict."""

    def __init__(self):
        self.checks = []

    def add(self, name, ok, detail=""):
        self.checks.append((name, bool(ok), detail))
        return bool(ok)

    @property
    def ok(self):
        return all(ok for (_name, ok, _detail) in self.checks)

    @property
    def count(self):
        return len(self.checks)

    def summary(self):
        parts = []
        for name, ok, detail in self.checks:
            if ok:
                parts.append(name)
            else:
                parts.append(name + " (FAIL" + (" " + detail + ")" if detail
                                              else ")"))
        return "; ".join(parts)


def _check_config(result, config, config_problems):
    """``config.validate()`` resets violating keys to defaults (see config.py),
    so callers may pass in the problems list from a single validate() call;
    when None (e.g. from the shell) we validate here."""
    if config_problems is None:
        config_problems = config.validate()
    return result.add(
        "config", not config_problems,
        "; ".join(config_problems[:2]) if config_problems else "")


def _check_state(result, state):
    """The persisted state file must be readable JSON with a bool heating_on.
    A missing file is fine (fresh boot); a corrupt one is not, because the
    dead-zone latch would silently drop."""
    try:
        uos.stat(state.filename)
    except OSError:
        return result.add("state", True)  # no file yet -- fresh boot
    try:
        with open(state.filename, "r") as f:
            data = json.load(f)
        if not isinstance(data.get("heating_on"), bool):
            return result.add("state", False, "bad heating_on type")
    except (OSError, ValueError) as e:
        return result.add("state", False, str(e))
    return result.add("state", True)


def _check_heap(result):
    free = gc.mem_free()
    return result.add(
        "heap", free >= MIN_HEAP_FREE,
        "" if free >= MIN_HEAP_FREE
        else "%d B free (< %d)" % (free, MIN_HEAP_FREE))


def _check_control_core(result):
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
    return result.add("control-core", ok, detail)


def run_post(config, state, config_problems=None, transport=None):
    """Run all POST checks and return a PostResult (see module docstring)."""
    result = PostResult()
    _check_config(result, config, config_problems)
    _check_state(result, state)
    _check_heap(result)
    _check_control_core(result)
    # Future transports (e.g. OTGW) can implement post_check() for a bounded
    # device health probe; nothing to check yet.
    if transport is not None and hasattr(transport, "post_check"):
        try:
            result.add("transport", bool(transport.post_check()))
        except Exception as e:
            result.add("transport", False, str(e))
    logger.info(
        "POST: %s (%d checks) -- %s" % (
            "PASS" if result.ok else "FAIL", result.count, result.summary()),
        log_to_file=True)
    return result
