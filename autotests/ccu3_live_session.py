"""Shared live-CCU3 plumbing for the on-device suites (imported, not run).

Named without the test_ prefix so the runner does not collect it; /autotests
is on sys.path, so test modules import it normally. The runner re-imports
test modules per run but NOT plain helpers, so this module's pooled session
survives across consecutive 'test run' invocations within one app boot --
that is deliberate (see the session discipline below).

Session discipline (AGENTS.md, verified live twice): the CCU3 caps
concurrent JSON-RPC sessions, and idle sessions linger for MINUTES. One
login per test -- or per suite module, or per run -- exhausts the pool and
every later login fails with 'invalid credentials or too many sessions'
until the leftovers expire. So ALL suites share this ONE session: at most
2 (app weather) + 1 (app rooms) + 1 (suites) = 4 concurrent sessions ever.
A session killed by CCU idle-expiry is re-created transparently by
Ccu3Rpc.call's re-login path.

Read-only contract: suites share this rpc for READ methods only. NEVER add
setValue or config RPCs to any suite -- the CCU3 is production.
"""

import uasyncio as asyncio

from config import config

_state = {"rpc": None}


async def get_rpc():
    """The one shared Ccu3Rpc for all suites (lazy login with backoff)."""
    if _state["rpc"] is None:
        from ccu3 import Ccu3Rpc, Ccu3Error
        rpc = Ccu3Rpc(config.get("ccu3_url"), config.get("ccu3_user"),
                      config.get("ccu3_pass"))
        last = None
        for attempt in range(5):
            try:
                await rpc.login()
                break
            except (Ccu3Error, OSError) as exc:  # full pool: back off
                last = exc
                if attempt < 4:
                    await asyncio.sleep(3.0 * (attempt + 1))
        else:
            raise Ccu3Error("CCU3 login failed (5 tries): %s" % last)
        _state["rpc"] = rpc
    return _state["rpc"]


async def call(method, params):
    """One RPC through the shared session, retrying transient socket errors
    once (the app's own polls share the tight heap; a concurrent socket
    open can transiently ENOMEM -- a protocol failure fails twice)."""
    rpc = await get_rpc()
    try:
        return await rpc.call(method, params)
    except OSError:
        await asyncio.sleep(1.0)
        return await rpc.call(method, params)


async def wait_wifi(timeout_s=90):
    """Bounded wait for the app's Wi-Fi (sync tests can't be preempted)."""
    from ccu3 import _now_ms
    import network
    sta = network.WLAN(network.STA_IF)
    deadline = _now_ms() + timeout_s * 1000
    while _now_ms() < deadline:
        try:
            if sta.active() and sta.isconnected():
                return True
        except OSError:
            pass
        await asyncio.sleep(1.0)
    return False
