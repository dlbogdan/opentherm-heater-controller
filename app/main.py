"""App entry point for the OpenTherm heater controller (Pico 2W / micropy-system).

This is **Step 0** of the build plan: the boot skeleton + framework harness. It
establishes the three things every later step builds on:

* **A/B slot confirmation** — owned by the **framework launcher** (see
  ``micropy-system/src/slot_main.py`` + ``lib/coresys/post.py``): it runs the
  power-on self-test (the framework's general checks) BEFORE a candidate may
  confirm; a passing candidate is confirmed inside the 12 s watchdog window, a
  failing one is rolled back and quarantined. This app supplies no domain
  checks of its own -- it just *reads* the boot context
  (``is_candidate_boot`` / ``is_degraded``) to hold actuation. The control-core
  self-test (``selftest.py``) stays as the on-demand reference (shell
  ``selftest``) until the transport is live.
* **Wi-Fi** (optional, non-fatal) — kicked off non-blocking and driven to
  completion by a 500 ms keepalive task; the control core runs fine with it off.
* **The scheduler** — a ``TaskManager`` whose periodic tasks are the single place
  all recurring work lives. The framework owns the event loop and the WDT; the app
  never feeds the watchdog itself.

The **serial console** is the test surface for this step: a heartbeat task reports
uptime, free heap, and Wi-Fi state every 10 s.

Contract (micropy-system): this module is imported as ``app_entry`` and its
``main()`` coroutine is awaited inside the framework's own ``uasyncio`` loop, so
``main()`` must NOT call ``asyncio.run`` and must NOT return (returning ends the
loop and halts the device).
"""

import sys
import time
import gc

import uasyncio as asyncio

import lib.coresys.logger as logger
from lib.coresys.manager_config import ConfigManager
from lib.coresys.manager_wifi import WiFiManager
from lib.coresys.manager_tasks import TaskManager

# NOTE: `config`/`state` are module-singleton INSTANCES, not the modules --
# `from x import x` binds the instance so `config.validate()` / `state.heating_on`
# work (the modules themselves only expose the class + instance).
from config import config
from state import state


def _make_wifi(sys_config):
    """Build a WiFiManager from framework config. Returns (wifi, has_ssid)."""
    device_name = sys_config.get("DEVICE", "NAME", "otc")
    ssid = sys_config.get("WIFI", "SSID", "")
    password = sys_config.get("WIFI", "PASS", "")
    wifi = WiFiManager(ssid=ssid, password=password, hostname=device_name)
    return wifi, bool(ssid)


def _apply_static_ip():
    """If a static IP is configured, apply it to the STA interface (no-op if DHCP).

    The Pico W's ``network.WLAN(STA_IF)`` is a singleton, so this configures the
    same interface the framework WiFiManager will connect. Must be called before
    ``wifi.up()``.
    """
    ip = config.get("net_ip")
    if not ip:
        return False
    mask = config.get("net_mask") or "255.255.255.0"
    gw = config.get("net_gw") or ip
    dns = config.get("net_dns") or gw
    try:
        import network
        network.WLAN(network.STA_IF).ifconfig((ip, mask, gw, dns))
        logger.info("Net: static IP %s/%s gw %s" % (ip, mask, gw))
        return True
    except Exception as e:
        logger.error("Net: failed to set static IP: %s" % e)
        return False


async def _run_optional_service(coro, name):
    """Contain an optional long-running service failure to that service."""
    try:
        await coro
    except Exception as e:
        logger.error("App: optional service %s failed: %s" % (name, e),
                     log_to_file=True)


def _run_selftest(_args=""):
    """Run the control-core self-test; return 'PASS/FAIL: ...' + check lines."""
    import builtins
    for p in ("apps/a", "apps/b"):
        try:
            sys.path.insert(0, p)
        except Exception:
            pass
    import selftest
    captured = []
    real_print = builtins.print
    builtins.print = lambda *values, **_kw: captured.append(
        " ".join(str(value) for value in values))
    try:
        ok = selftest.run()
    except Exception as e:
        ok = False
        captured.append("error: %s" % e)
    finally:
        builtins.print = real_print
    detail = "\n".join(captured[-30:])
    verdict = "PASS" if ok else "FAIL"
    return "%s: control core self-test%s" % (
        verdict, ("\n" + detail) if detail else "")


async def main():
    logger.info("App: main entered.", log_to_file=True)

    # 1. Boot context (set by the framework launcher BEFORE main() is called,
    #    after it ran the POST and made the confirm/rollback decision): on a
    #    supervised candidate boot or a degraded active boot the device layer
    #    holds non-idempotent physical actuation. See lib/coresys/post.py.
    from lib.coresys import post as framework_post
    state.candidate_boot = framework_post.is_candidate_boot()
    state.post_failed = framework_post.is_degraded()
    if state.candidate_boot:
        logger.info("App: [candidate boot - holding actuation].",
                    log_to_file=True)
    elif state.post_failed:
        logger.error("App: [degraded - POST failed, holding actuation].",
                     log_to_file=True)

    # 1b. Config pre-flight: the boot POST is heap-only, so enforce the app's
    #     own cross-key constraints here -- reset any out-of-range value to its
    #     default and log problems -- before the control loop can read them.
    config.validate()

    # 2. Framework harness: config + Wi-Fi + scheduler.
    sys_config = ConfigManager("/system-config.json")
    wifi, has_ssid = _make_wifi(sys_config)
    tasks = TaskManager()

    # Shared recording transport: the shell observes this exact instance and
    # the future control task will apply its decisions to the same object.
    # Until physical OTGW wiring lands, it provides a safe integration seam.
    from transport.log import LogTransport
    transport = LogTransport()

    if has_ssid:
        _apply_static_ip()  # no-op unless a static IP is configured
        wifi.up()  # non-blocking connect kickoff
        tasks.create_periodic_task(
            wifi.refresh, interval_ms=500, task_id="wifi_keepalive",
            description="Wi-Fi keepalive", is_coroutine=True)
        logger.info(
            "App: Wi-Fi connect kicked off; keepalive task running.",
            log_to_file=True)
    else:
        logger.info("App: no Wi-Fi SSID configured; running offline.")

    # Remote shell (framework service on the standard telnet port): status,
    # log, heap, reboot (+ optional repl) and the app self-test. Started as a
    # one-shot task. Wrap it so an unexpected service exception is logged and
    # contained instead of being re-raised by TaskManager into the event loop.
    if config.get("net_enabled") and has_ssid:
        from lib.coresys.telnet_service import TelnetService
        shell = TelnetService(wifi=wifi, port=int(config.get("net_port")),
                              name="otc")
        shell.add("selftest", _run_selftest, "run the control-core self-test")
        from shell_commands import register_transport_commands
        register_transport_commands(shell, transport)
        tasks.create_task(
            _run_optional_service(shell.start(), "remote shell"),
            task_id="net_service",
            description="remote shell (status/log/reboot + selftest)")
        logger.info("App: remote shell service task started.")

    # 3. App domain config + state (Step 1): log the validated boot snapshot.
    #    Config is loaded from /config.json and repaired by the pre-flight
    #    validation above before any future control task can consume it.
    logger.info(
        "App: t_off=%s t_on=%s flow[%s..%s] min_on=%s design=%s/%s transport=%s"
        " | heating_on=%s"
        % (config.get("t_off"), config.get("t_on"), config.get("flow_min"),
           config.get("flow_max"), config.get("flow_min_on"),
           config.get("flow_design"), config.get("t_design"),
           config.get("transport"), state.heating_on))

    # 4. Heartbeat -- the serial-console test surface for Step 0.
    #    The FIRST heartbeat is also file-logged so a live boot leaves evidence
    #    in /log.txt even when we cannot watch the console.
    start_ms = time.ticks_ms()
    first_beat = [True]

    def heartbeat():
        gc.collect()
        uptime_s = time.ticks_diff(time.ticks_ms(), start_ms) // 1000
        ip = wifi.get_ip() or "-"
        rssi = wifi.get_signal_strength()
        rssi_str = " %s dBm" % rssi if rssi is not None else ""
        logger.info(
            "Heartbeat: up %ss | heap %s B | wifi %s %s%s"
            % (uptime_s, gc.mem_free(), wifi.get_state(), ip, rssi_str),
            log_to_file=first_beat[0])
        first_beat[0] = False

    tasks.create_periodic_task(
        heartbeat, interval_ms=10000, task_id="heartbeat",
        description="Step 0 heartbeat", is_coroutine=False)

    logger.info(
        "App: Step 0 + 1 harness ready; entering main loop.", log_to_file=True)

    # 5. Keep the uasyncio loop alive; periodic tasks do the recurring work.
    #    (uasyncio has no sleep_ms -- sleep() takes fractional seconds.)
    while True:
        await asyncio.sleep(1.0)


if __name__ == "__main__":
    asyncio.run(main())
