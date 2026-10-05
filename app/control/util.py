"""Small math helpers shared by the control core (Step 2)."""


def clamp(value, lo, hi):
    """Clamp ``value`` into ``[lo, hi]`` (order-insensitive on the bounds)."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def sign(x):
    """Return -1, 0, or 1 for the sign of ``x``."""
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def elapsed_ms(started_ms, now_ms):
    """Wrap-safe elapsed milliseconds between two ``ticks_ms``-style stamps.

    ``ticks_ms`` wraps at 2**32 ms (~49.7 days of uptime), so raw subtraction
    goes negative across the wrap and silently breaks every interval
    comparison. This applies the same uint32 signed-diff semantics as
    MicroPython's ``time.ticks_diff``, so it is identical on host and device
    (and needs no ``time`` import here). The result is signed: correct for
    intervals under 2**31 ms (~24.8 days), far beyond anything the control
    core measures.
    """
    diff = (int(now_ms) - int(started_ms)) & 0xFFFFFFFF
    if diff > 0x7FFFFFFF:
        diff -= 0x100000000
    return diff
