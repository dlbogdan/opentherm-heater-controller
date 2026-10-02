"""App entry point for the OpenTherm heater controller (Pico 2W / micropy-system).

This is **Step 0** of the build plan: the boot skeleton + framework harness. It
establishes the three things every later step builds on:

* **A/B slot confirmation** — gated on the **power-on self-test** (``post.py``):
  a candidate that passes POST confirms inside the 12 s candidate window
  enforced by the hardware watchdog (see ``watchdog.py``); one that fails never
  confirms, so the watchdog rolls it back and quarantines the version.
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
from lib.coresys.ota_state import load_state
from lib.coresys.slot_manager import confirm_running_slot
from lib.coresys.manager_config import ConfigManager
from lib.coresys.manager_wifi import WiFiManager
from lib.coresys.manager_tasks import TaskManager

# NOTE: `config`/`state` are module-singleton INSTANCES, not the modules --
# `from x import x` binds the instance so `config.validate()` / `state.heating_on`
# work (the modules themselves only expose the class + instance).
from config import config
from state import state
import post


def _sample_boot_slot():
    """Read the A/B state and flag a candidate boot; returns the running slot.

    The candidate flag must be sampled BEFORE confirming (confirmation clears
    ``pending``): while ``state.candidate_boot`` is set the device layer holds
    non-idempotent physical actuation -- this boot is supervised and reboots
    ~1-2 s after confirmation. Slot confirmation itself is deferred to AFTER
    the power-on self-test (see ``main()``) and still lands inside the 12 s
    candidate window.
    """
    pre = load_state()
    running_slot = pre["pending"] or pre["active"]
    state.candidate_boot = pre["pending"] is not None
    logger.info(
        "App: running as slot '%s'%s."
        % (running_slot,
           " [candidate boot - holding actuation]"
           if state.candidate_boot else ""),
        log_to_file=True)
    return running_slot


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


def _run_post(_args=""):
    """Run the power-on self-test on demand; return the verdict line."""
    result = post.run_post(config, state)
    return "%s: %s" % ("PASS" if result.ok else "FAIL", result.summary())


async def main():
    logger.info("App: main entered.", log_to_file=True)

    # 1. Sample the A/B state FIRST: on a candidate boot the actuation hold
    #    must be in force before anything else (this boot is supervised).
    running_slot = _sample_boot_slot()

    # 2. Framework harness: config + Wi-Fi + scheduler.
    sys_config = ConfigManager("/system-config.json")
    wifi, has_ssid = _make_wifi(sys_config)
    tasks = TaskManager()

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
    # log, heap, reboot, repl built-ins + the app self-test. Started as a
    # one-shot task; a failure here is isolated to that task and cannot take
    # down the control loop.
    if config.get("net_enabled") and has_ssid:
        from lib.coresys.telnet_service import TelnetService
        shell = TelnetService(wifi=wifi, port=int(config.get("net_port")),
                              name="otc")
        shell.add("selftest", _run_selftest, "run the control-core self-test")
        shell.add("post", _run_post, "run the power-on self-test")
        tasks.create_task(
            shell.start(), task_id="net_service",
            description="remote shell (status/log/reboot/repl + selftest)")
        logger.info("App: remote shell service task started.")

    # App domain config + state (Step 1): validate and log the boot snapshot.
    config_problems = config.validate()
    logger.info(
        "App: t_off=%s t_on=%s flow[%s..%s] min_on=%s design=%s/%s transport=%s"
        " | heating_on=%s"
        % (config.get("t_off"), config.get("t_on"), config.get("flow_min"),
           config.get("flow_max"), config.get("flow_min_on"),
           config.get("flow_design"), config.get("t_design"),
           config.get("transport"), state.heating_on))

    # 4. POWER-ON SELF-TEST (the A/B gate) -- runs BEFORE slot confirmation
    #    so a failing candidate never confirms. All checks are local and fast,
    #    keeping the confirm (below) inside the 12 s candidate window.
    post_result = post.run_post(config, state,
                                config_problems=config_problems)
    if post_result.ok:
        confirm_running_slot(running_slot)
        logger.info(
            "App: slot '%s' confirmed after POST."
            % (running_slot,), log_to_file=True)
    elif state.candidate_boot:
        # Candidate + POST failure: do NOT confirm -- the watchdog's candidate
        # window rolls this slot back and quarantines the version. The shell
        # stays up for a quick look, then idle so the watchdog does its job.
        logger.error(
            "POST failed on candidate boot -- not confirming; watchdog will "
            "roll back and quarantine this version.", log_to_file=True)
        while True:
            await asyncio.sleep(1.0)
    else:
        # Active slot + POST failure: degraded mode -- services stay up for
        # diagnosis, actuation is held, and the app never reboots (a reboot
        # would just repeat the same failing boot).
        state.post_failed = True
        logger.error(
            "POST failed on active boot %s -- degraded mode: actuation held, "
            "services up for diagnosis." % (running_slot,), log_to_file=True)

    # 5. Heartbeat -- the serial-console test surface for Step 0.
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

    # 6. Keep the uasyncio loop alive; periodic tasks do the recurring work.
    #    (uasyncio has no sleep_ms -- sleep() takes fractional seconds.)
    while True:
        await asyncio.sleep(1.0)


if __name__ == "__main__":
    asyncio.run(main())
