# Bug 5 — Secret masking is courtesy, not a boundary (unauthenticated shell + `repl`)

> Source: full-project review 2026-10-10. Severity: **informational**
> (accepted trusted-LAN design — but the acceptance is nowhere explicit).
> Status: PLANNED — decision record + one optional cheap switch.

## Problem

The remote shell (framework `telnet_service`, port 23) is **unauthenticated
on the LAN** and fully privileged:

- `config set` can rewrite any app config (e.g. point `ccu3_url` at an
  attacker host, flip `transport`, change `manual_setpoint`).
- The framework `repl` command gives a raw MicroPython prompt → the whole
  filesystem, including `/app-config.json` with the **CCU3 credentials in
  cleartext** (and tomorrow's `mqtt_pass`).
- The `config` display masking (`app/shell_commands.py:233-250`,
  `_SECRET_SUFFIXES`) hides `*_pass` from listings — good hygiene, but it
  only hides values from the `config` command. Anyone with `repl` (or a
  USB connection) reads them directly.

This is the documented trusted-LAN posture (AGENTS.md: "One HTTP/1.0 request
per connection, no TLS (trusted LAN)"), and it is reasonable for a heating
controller on a home LAN. What is MISSING is an explicit statement that the
shell itself is part of the trust boundary — so nobody later mistakes the
masking for a security control, and the owner decides (rather than inherits)
the posture.

## Fix

1. **Decision record (the actual fix).** AGENTS.md, in the shell/resource
   grammar area: a short "Trust model" note —
   - the shell (incl. `repl`) is the LAN trust boundary: anyone who can
     reach port 23 owns the device;
   - `config` secret masking is a **display courtesy** (keeps credentials
     out of casual output/logs/screenshots), NOT a security control;
   - accepted risks: plaintext telnet, cleartext credentials on flash,
     unauthenticated `config set` / `reboot` / `repl`;
   - mitigations that are DELIBERATELY rejected: fake shell auth over
     plaintext telnet (a token on the wire is no auth — it would only buy
     false confidence).
   README's shell section gets a one-line pointer to it.
2. **Optional switch (owner gate — implement only if the owner wants it).**
   A `repl_enabled` bool in the framework's `system-config.json`, checked by
   `TelnetService` before spawning the REPL. `repl` is FRAMEWORK-owned, so
   this is a `micropy-system` change (framework ownership rules, AGENTS.md):
   file it in `micropy-system/PLAN.md`, do NOT half-do it from the app.
   Until it exists, the operational mitigation is network placement (the
   board lives on a trusted LAN/VLAN; no port-23 forwarding — worth one
   confirming line in the README).

## Verify

- Host: none needed for the doc step. If the framework switch is approved
  and lands: the framework host suite pins `repl_enabled=false` refuses
  `repl`; the app's `config_schema` stays untouched (it is a SYSTEM
  setting, framework-owned).
- Device: `telnet.py <ip> config get SHELL.net_port` unchanged (no app
  config churn from this bug); if the framework switch lands, `repl`
  refusal + everything else still works.

## Workflow / commit

Docs-only from the project side: AGENTS.md + README edit, no deploy, no
version bump. The optional framework switch is a separate milestone in the
submodule with its own deploy/verify cycle.

## Out of scope

- TLS/auth on the shell (rejected above — plaintext-LAN posture).
- Encrypting `/app-config.json` (flash is not a confidentiality boundary
  either; USB reads it regardless).
