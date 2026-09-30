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
