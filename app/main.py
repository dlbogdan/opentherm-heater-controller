"""App entry point for the OpenTherm heater controller (Pico 2W / micropy-system).

This is **Step 0** of the build plan: the boot skeleton + framework harness. It
establishes the three things every later step builds on:

* **A/B slot confirmation** — owned by the **framework launcher** (see
  ``micropy-system/src/slot_main.py`` + ``lib/coresys/post.py``): it runs the
  power-on self-test (the framework's general checks) BEFORE a candidate may
  confirm; a passing candidate is confirmed inside the 12 s watchdog window, a
  failing one is rolled back and quarantined. This app supplies no domain
  checks of its own -- it just *reads* the boot context
  (``is_candidate_boot`` / ``is_degraded``) to hold actuation (the control
  loop issues no physical writes on a candidate/degraded boot). The
  self-test (``selftest.py``, shell ``selftest``) stays as the on-demand
  reference and also covers the control-loop wiring.
* **The control loop** — a periodic task (``control_loop.py``) running
  ``sensors -> Controller -> audited transport`` on a configurable interval
  (``control_tick_s``). Until the sensor source and the real OTGW driver land,
  it runs null readings (defined failsafe heat) against the dummy driver,
  which is fully observable through the ``transport`` shell commands.
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
import os
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

# Live wiring handle (read-only BY CONVENTION): populated once in main() so
# diagnostics and the device test suites can observe the EXACT objects the
# control loop uses (an autotest does ``import app_entry`` -> the already
# imported live module). Suites may read these; they must NEVER actuate
# through them -- behavioral tests build fresh dummies instead.
LIVE = {}


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
    # Put the ACTIVE slot first. The launcher's activate_slot_path already
    # guarantees this for the live app, but the old code inserted apps/b last
    # regardless, so an older inactive slot could shadow the running
    # firmware's selftest module (2026-10-05: the shell ran slot b's 34-check
    # suite while slot a ran 1.1.70). The explicit ordering also covers the
    # raw-REPL path where no slot is on sys.path yet.
    try:
        from lib.coresys.ota_state import load_state
        active = load_state()["active"]
    except Exception:
        active = "a"
    if active not in ("a", "b"):
        active = "a"
    for p in ("apps/" + ("b" if active == "a" else "a"), "apps/" + active):
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


def _isdir(path):
    """Directory probe that works on this MicroPython build.

    Verified on-device 2026-10-07: this 1.29.0 rp2 build has NO os.path
    attribute at all (``os.path.isdir`` -> AttributeError, which killed the
    1.1.77 candidate). os.stat is always present; S_IFDIR is 0o40000.
    """
    try:
        return bool(os.stat(path)[0] & 0o40000)
    except OSError:
        return False


def _autotests_dir():
    """Where the device test suites live (registration-time resolution).

    The slot-packaged ``autotests/`` (a --debug OTA build: integrity-checked
    and versioned with the RUNNING firmware) outranks the USB push directory
    ``/autotests``, so stale push debris can never shadow the suites that
    shipped with this firmware. The launcher put the slot on sys.path; a
    release build has no such directory and the framework default applies.
    (Explicit here because the runner in /lib/coresys is only refreshed by
    the USB push/provisioning -- the app must carry its own contract.)
    """
    for entry in sys.path:
        if not entry:
            continue
        candidate = entry.rstrip("/") + "/autotests"
        if _isdir(candidate):
            return candidate
    return None


async def main():
    logger.info("App: main entered.")

    # 1. Boot context (set by the framework launcher BEFORE main() is called,
    #    after it ran the POST and made the confirm/rollback decision): on a
    #    supervised candidate boot or a degraded active boot the device layer
    #    holds non-idempotent physical actuation. See lib/coresys/post.py.
    from lib.coresys import post as framework_post
    state.candidate_boot = framework_post.is_candidate_boot()
    state.post_failed = framework_post.is_degraded()
    if state.candidate_boot:
        # Safety state (actuation held) -- a warning, so it IS written to flash.
        logger.warning("App: [candidate boot - holding actuation].",
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

    # Shared audited transport: LogTransport (bounded in-memory ring) wraps
    # the driver; the shell observes this exact instance and the control loop
    # applies its decisions through the same object. The driver is selected
    # by config (transport: "otgw_dummy" | "otgw_uart" | "direct_ot_dummy"):
    # the dummies mirror the real commands and stay the default until the
    # gateway is wired; "otgw_uart" is the real OTGWTransportDrv.
    from transport.log import LogTransport
    from transport.factory import make_transport
    transport = LogTransport(make_transport(config))

    # 2b. Control loop: sensors -> controller -> audited transport. The
    #     interval is configurable (control_tick_s, seconds; applied at boot)
    #     and the first tick fires immediately after the task starts. All
    #     actuation is held while the framework marks this boot as candidate
    #     or degraded (state.candidate_boot / state.post_failed).
    from control.controller import Controller
    from control_loop import run_control_tick
    from sensors import make_sensor_source
    # Logging policy (microcontroller, flash-wear constrained -- see AGENTS.md):
    # INFO goes to the serial console only; only WARN/ERROR are written to
    # /log.txt (the framework's OTA process also writes to the file).
    log = lambda m: logger.info(m)
    warn = lambda m: logger.warning(m, log_to_file=True)
    controller = Controller(initial_heating_on=state.heating_on)
    sensor_source = make_sensor_source(config, log=log, warn=warn)

    def _net_up():
        try:
            return bool(wifi.is_up())
        except Exception:
            return False

    async def _net_ready(timeout_ms):
        """Bounded async wait for Wi-Fi; True when up, False when it expires.

        The framework fires each periodic tick's FIRST call immediately at
        boot -- before Wi-Fi has connected. The tick therefore WAITS for the
        connect (bounded) instead of skipping: no I/O fires while the network
        is down (no EHOSTUNREACH noise), and the first successful pass lands
        right after connect instead of at the next interval. The timeout
        keeps a board without Wi-Fi running (the tick degrades, the next
        interval retries). Polling is 1 s -- the 500 ms Wi-Fi keepalive task
        drives the connect in parallel.
        """
        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            if _net_up():
                return True
            await asyncio.sleep(1.0)
        return _net_up()

    # Room model (arch §3.4): the (slow) setpoint/actual pass runs in its OWN
    # periodic task and caches the aggregate (demand + per-room). The fast
    # control loop reads the cached demand + the weather (t_out/lux), so it
    # never does a slow pass. The concrete source (CCU3 heating groups, or
    # none) is chosen by the factory -- main.py stays backend-agnostic, exactly
    # as with the weather source above. All I/O is async (non-blocking).
    # The first tick fires before Wi-Fi is connected: it waits (bounded, 120 s)
    # for the connect instead of skipping, so the first pass lands right after
    # connect -- no failing I/O, no full-interval dead wait.
    from rooms import make_rooms_source, pick_demand
    from sensors import TIER_EXPIRED, weather_tier
    rooms = make_rooms_source(config, log=log, warn=warn)
    LIVE.update(transport=transport, rooms=rooms, controller=controller,
                sensor_source=sensor_source)
    # Demand P-term + gate (controller step 4): enabled from config when a
    # room source exists to feed it. A missing reading is still the defined
    # "no sensor" input inside the controller, so this can never strand the
    # heating on a cold day if the CCU3 is down.
    controller.has_demand_sensor = (
        bool(config.get("demand_enabled")) and rooms.enabled)
    if rooms.enabled:
        async def rooms_tick():
            if not await _net_ready(120 * 1000):
                return  # no network within the window -- next interval retries
            try:
                await rooms.read()
            except Exception as exc:  # contain; the next poll retries
                warn("Rooms: poll failed: %s" % exc)  # error -> to flash

        tasks.create_periodic_task(
            rooms_tick, interval_ms=rooms.poll_s * 1000, task_id="rooms",
            description="room pass (setpoints/actuals -> demand)",
            is_coroutine=True)
        logger.info("App: rooms poll started (every %ds)." % rooms.poll_s)

    # P4 demand-expiry WARN edge tracker (RAM-only): warn once on the
    # transition INTO expired, cleared on recovery, never per tick.
    demand_edge = {"was": None}

    async def control_tick():
        # Last-resort net (PLAN.md P3): make control-tick failures visible.
        # The framework catches per-tick exceptions, but its default
        # listener's line is generic ("SystemManager: Task control failed")
        # and /lib/coresys is only as fresh as the last provisioning -- the
        # app carries its OWN domain-level containment, mirroring rooms_tick
        # and reassert_tick. run_control_tick's granular handlers (apply_
        # decision / transport.tick) keep their better messages and fire
        # first; this net catches everything else that escapes the tick
        # (_net_ready, the sensor read outside its own containment, rooms.
        # last(), config.all(), the pure controller.tick). On a raise the
        # tick aborts: no decision, no latch persist; the independent re-
        # assert task keeps the last CS alive (documented standalone
        # behavior) and the next tick retries. At most the solar
        # accumulator advanced mid-tick (self-correcting, dt-capped) --
        # the heating_on/last_sent_flow assignments never complete, so no
        # rate-limiter lie is introduced.
        try:
            # Fetch the (possibly I/O-bound) weather reading first -- the
            # only async part of this tick; the pure tick below stays sync
            # + testable. The tick waits (bounded, 30 s) for Wi-Fi instead
            # of skipping: no failing read before connect, and the first
            # real reading lands right after connect; still down when the
            # window expires -> failsafe input (t_out is None) exactly as
            # before.
            if await _net_ready(30 * 1000):
                t_out, lux, _ = await sensor_source.read()
            else:
                t_out, lux = None, None
            # Heating demand + sensor-freshness tiers (P4). Everything is
            # derived from the sources' OWN timestamps (single source of
            # truth -- no drifting booleans): pick_demand applies the
            # whole fresh/cached/expired policy (host-pinned in rooms.py,
            # one shared sensor_cache_s expiry), and the weather tier is
            # derived from the source's cached stamp. The tier feeds
            # state.data_state (RAM-only; `sources` shell command; the
            # future UI warning flag). Expired -> the controller's
            # defined failsafe inputs.
            cache_s = int(config.get("sensor_cache_s"))
            now_ms = time.ticks_ms()
            values = (sensor_source.last()
                      if hasattr(sensor_source, "last") else None)
            weather, weather_age = weather_tier(
                values, now_ms, config.get("ccu3_poll_s"), cache_s)
            demand, demand_tier, demand_age = pick_demand(
                rooms.last(), now_ms, rooms.poll_s, cache_s)
            if (demand_tier == TIER_EXPIRED
                    and demand_edge["was"] != TIER_EXPIRED):
                # Edge-triggered (one flash line per transition, never
                # one per tick -- logging policy).
                warn("Rooms: demand expired (age %ss > %ss); no-sensor "
                     "input" % (demand_age, cache_s))
            demand_edge["was"] = demand_tier
            state.data_state = {
                "weather": weather, "weather_age_s": weather_age,
                "demand": demand_tier, "demand_age_s": demand_age,
                "cache_s": cache_s,
            }

            def read_sensors():
                return (t_out, lux, demand)

            run_control_tick(
                controller, transport, state, config.all(), read_sensors,
                now_ms, log=log, warn=warn)
        except Exception as exc:
            # WARN -> flash (logging policy); consumes the exception, so
            # the framework's generic TASK_FAILED line stops firing for
            # this task: flash writes stay 1/tick on a persistent failure.
            warn("Control: tick failed: %s" % exc)

    tick_s = int(config.get("control_tick_s"))
    tasks.create_periodic_task(
        control_tick, interval_ms=tick_s * 1000, task_id="control",
        description="control loop (sensors -> controller -> audited transport)",
        is_coroutine=True)
    logger.info(
        "App: control loop started (tick %ds, first tick immediate; actuation "
        "held on candidate/degraded boot)." % tick_s)

    # OTGW vigilance rule (AGENTS.md, OTGW section): a control setpoint of
    # >= 8 degC EXPIRES at the gateway after about a minute unless re-asserted
    # (standalone mode reverts it to 0). The control tick (default 60 s) has
    # ZERO margin against that limit, so the re-assert runs on its own faster
    # cadence (cs_reassert_s, default 30 s) that drives transport.tick() --
    # which is idempotent, so it coexists with the control tick's tick call.
    # Only backends with the obligation expose a non-zero reassert_interval_s
    # (DummyOTGW / the future OTGWTransportDrv; direct OT returns 0).
    reassert_s = int(transport.reassert_interval_s or 0)
    if reassert_s > 0:
        def reassert_tick():
            try:
                transport.tick(time.ticks_ms())
            except Exception as exc:
                warn("Transport: CS re-assert failed: %s" % exc)

        tasks.create_periodic_task(
            reassert_tick, interval_ms=reassert_s * 1000,
            task_id="otgw_reassert",
            description="OTGW CS re-assert (sub-minute vigilance)",
            is_coroutine=False)
        logger.info("App: CS re-assert task started (every %ds)." % reassert_s)

    if has_ssid:
        _apply_static_ip()  # no-op unless a static IP is configured
        wifi.up()  # non-blocking connect kickoff
        tasks.create_periodic_task(
            wifi.refresh, interval_ms=500, task_id="wifi_keepalive",
            description="Wi-Fi keepalive", is_coroutine=True)
        logger.info(
            "App: Wi-Fi connect kicked off; keepalive task running.")
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
        from shell_commands import (register_config_commands,
                                    register_rooms_commands,
                                    register_sources_commands,
                                    register_transport_commands)
        register_transport_commands(shell, transport)
        register_config_commands(shell, config)
        register_rooms_commands(shell, rooms)
        register_sources_commands(shell, state)
        # Device test suites (framework runner): unittest-style suites are
        # pushed to /autotests by the host tool (tools/target/autotest.py)
        # or OTA'd INSIDE the slot by a --debug build (deploy.py --debug).
        # They run INSIDE this loop -- live singletons, real async I/O, heap
        # and timeout guarded. The runner module itself is pushed to
        # /lib/coresys by the same tool: a board that never received it
        # (production firmware) simply has no 'test' command.
        try:
            from lib.coresys.autotest import register as register_autotests
            # A test harness must never take down the heating app: any
            # registration failure is contained and file-logged (only
            # ImportError -- no runner on release boards -- stays silent).
            try:
                tests_dir = _autotests_dir()
                if tests_dir:
                    try:
                        register_autotests(shell, warn=warn,
                                           directory=tests_dir)
                    except TypeError:
                        # Runner copy predates the directory kwarg: keep
                        # the test command on its default directory.
                        register_autotests(shell, warn=warn)
                        warn("Shell: runner predates directory kwarg; "
                             "slot suites (%s) not registered" % tests_dir)
                else:
                    register_autotests(shell, warn=warn)
            except Exception as exc:
                warn("Shell: autotest registration failed: %s" % exc)
        except ImportError:
            pass
        tasks.create_task(
            _run_optional_service(shell.start(), "remote shell"),
            task_id="net_service",
            description="remote shell (status/log/reboot + selftest)")
        logger.info("App: remote shell service task started.")

    # 3. App domain config + state (Step 1): log the validated boot snapshot.
    #    Config is loaded from /app-config.json and repaired by the pre-flight
    #    validation above before any future control task can consume it.
    logger.info(
        "App: t_off=%s t_on=%s flow[%s..%s] min_on=%s design=%s/%s transport=%s"
        " | heating_on=%s demand=%s(%s)"
        % (config.get("t_off"), config.get("t_on"), config.get("flow_min"),
           config.get("flow_max"), config.get("flow_min_on"),
           config.get("flow_design"), config.get("t_design"),
           config.get("transport"), state.heating_on,
           "on" if controller.has_demand_sensor else "off",
           "rooms" if rooms.enabled else "no-source"))

    # 4. Heartbeat -- the serial-console test surface for Step 0.
    #    INFO-level: serial console only, never written to flash (see the
    #    logging policy above). Boot evidence is the OTA process (framework,
    #    file-logged) plus `status`/the serial console -- not the heartbeat.
    start_ms = time.ticks_ms()

    def heartbeat():
        gc.collect()
        uptime_s = time.ticks_diff(time.ticks_ms(), start_ms) // 1000
        ip = wifi.get_ip() or "-"
        rssi = wifi.get_signal_strength()
        rssi_str = " %s dBm" % rssi if rssi is not None else ""
        logger.info(
            "Heartbeat: up %ss | heap %s B | wifi %s %s%s"
            % (uptime_s, gc.mem_free(), wifi.get_state(), ip, rssi_str))

    tasks.create_periodic_task(
        heartbeat, interval_ms=10000, task_id="heartbeat",
        description="Step 0 heartbeat", is_coroutine=False)

    # The provisioning boot-marker ("entering main loop") is on the serial
    # console; it is not written to flash (INFO).
    logger.info(
        "App: Step 0 + 1 harness ready; entering main loop.")

    # 5. Keep the uasyncio loop alive; periodic tasks do the recurring work.
    #    (uasyncio has no sleep_ms -- sleep() takes fractional seconds.)
    while True:
        await asyncio.sleep(1.0)


if __name__ == "__main__":
    asyncio.run(main())
