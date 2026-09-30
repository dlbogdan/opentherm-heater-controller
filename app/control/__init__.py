"""Pure control core (Step 2).

Every module here is framework-free (stdlib + sibling modules only) so the heart
of the firmware is identical on-device and unit-testable on a host. I/O (sensors,
transport, state persistence) lives OUTSIDE this package.
"""
