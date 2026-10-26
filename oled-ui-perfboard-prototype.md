# OLED Display + Input Controls — Perfboard Prototype (DRAFT v3)

> Status: draft v3 after owner discussion (2026-10-09). Bench plan for the
> next major feature milestone (PLAN.md deferred: "OLED UI"). It de-risks the
> hardware and the driver stack on a throwaway perfboard build BEFORE any
> production firmware is touched. Contracts it feeds into:
> `pico-standalone-architecture.md` §3.2 (pin plan), §3.3 (panel selection),
> §5 (scheduler), §11 (256×64 layouts), §13 (memory budget).
>
> Owner direction locked in v2: **UI on the second core** (framework-owned
> low-level plumbing, app-owned panel driver — §7), **SSD1322 panels already
> on hand** (ESP32-proven, possibly SH1322 clones), **rotary encoder with
> push + 2 spare buttons** (draft input choice, revisit at P4), spare
> Pico 2 W available for the bench, and **≥30 fps minimum (stretch 60) on
> the 4bpp grayscale panel** — the fps target reshapes the bandwidth and
> GIL math, see §7.6.

---

## 1. Purpose — what this prototype must prove

The production board drives a real boiler. A display/button failure must never
degrade heating. The prototype answers, on a spare bench Pico, the questions
the host suite cannot:

1. **Electrical:** does the SSD1322 panel work reliably on jumper wires from
   the Pico 2W at a usable clock, powered from the Pico's 3V3 rail alongside
   the Wi-Fi radio, without brownouts or artifacts? Clone variance: the
   on-hand modules may be SH1322 — the init sequence differs; identify the
   real controller per module.
2. **Driver:** does a MicroPython SSD1322-class driver fit our heap (already
   ~290 KB and fragmenting — AGENTS.md) and sustain the owner's **≥30 fps
   minimum (stretch 60)** on the 4bpp grayscale panel (8 KB framebuffer,
   ~8.2 KB per full frame) without starving the uasyncio loop?
3. **Core-1 viability (the big one):** what does `_thread` actually buy under
   MicroPython's GIL on our pinned v1.29.0 build — measure scheduling
   isolation, whether `SPI.write` releases the GIL, and the thread stack
   cost (§7). Decide the UI shape (asyncio task vs core-1 thread) with data.
4. **Input:** can a rotary encoder be read without missing detents — 2–5 ms
   thread polling vs `Pin.irq` edge queues (§6 P2)? Plus 2 buttons with
   software debounce.
5. **UX feasibility:** is the §11 layout renderable with our fonts, and does
   encoder + push + 2 buttons navigate it well?

**Out of scope** (deliberately): enclosure, panel mounting hardware, BH1750 /
DS18B20 wired sensors, OTGW wiring, MQTT.

**Never prototype on the production board** (`10.9.30.76` — it drives the
boiler). Bench board (owner decision 2026-10-11): **start on the Pico W
(RP2040)** for bring-up + performance tuning — `display_bringup.py`
auto-detects the chip. This is the conservative order: the SPI/panel
ceiling transfers 1:1 (same PL022 IP, same panel), and the RP2040 CPU
side (125 MHz M0+, smaller heap) is strictly slower than the Pico 2 W,
so a 30/60 fps PASS on RP2040 is a floor for RP2350. Re-run P3's S1-vs-S2
verdict on the Pico 2 W before P4 (thread stack cost + heap budget are
chip-specific).

---

## 2. Fixed inputs (do not re-decide here)

From `pico-standalone-architecture.md` (pinned):

- Panel target: **256×64 SPI OLED (SSD1322 class)** on **SPI0** (§3.3).
  Arch's GPIO 7–11 assignment is corrected by the SPI pin-table note in §4
  (MicroPython rejects GP7-as-SCK). Do NOT wire the 256×64 panel on I2C.
- CYW43439 radio lines GP23/24/25/29 (WL_REG_ON / WL_DATA+IRQ / WL_CS /
  WL_CLOCK) — never touch. GP26–28 are usable header pins (ADC-capable).
- Display refresh target ~10 fps (§5) — **superseded by the owner target
  ≥30 fps minimum, stretch 60** (2026-10-09). Arch §13's "framebuffer
  2 KB" is a 1bpp assumption: SSD1322 is **4bpp grayscale → 8 KB**
  (256×64×4bpp); flag the arch correction.
- Layouts: §11 status + config screens (the config screen finally exposes
  `manual_mode` / `manual_setpoint`, P4).

Owner decisions 2026-10-09 (deviations from arch, to be reflected back into
§3.2/§3.3 after the prototype):

- **Input = rotary encoder with push switch + 2 buttons** (draft; "we'll
  deal with this later a bit"). Replaces the arch's 5-button plan on the
  SAME GPIO block GP14–18.
- **UI direction: second core, solo** — with the framework/app split of §7.

From the current codebase (verified, supersedes older arch text):

- The OTGW driver uses **UART0 on GP0/GP1** (`app/transport/factory.py`).
  Arch §3.2's "OpenTherm single wire on GPIO 6" is the *direct-OT* future
  plan. GP0/GP1/GP6 all stay free on the prototype so it validates the
  **final** pin map, not a throwaway one.
- The app runs inside the framework uasyncio loop; periodic work goes
  through `TaskManager.create_periodic_task` (AGENTS.md). A core-1 thread is
  a NEW pattern outside that loop — §7 defines its supervision.

---

## 3. Bill of materials

| Item | Qty | Notes / selection |
|---|---|---|
| Pico W (RP2040) | 1 | **First bench board** (owner 2026-10-11: tuning starts here). |
| Pico 2 W (RP2350) | 1 | Spare bench board (owner has one) — final pin-map + P3 verdict. |
| 2.8" 256×64 OLED, SSD1322 | as available | **Owner already has modules** (used on ESP32 — likely SH1322 clones). P1 must identify the real controller per module; keep one known-good ESP32-tested unit as reference. **16-level grayscale (4bpp)** — MicroPython `framebuf.GS4_HMSB` matches the GRAM packing. |
| Rotary encoder with push switch | 2–3 | Common through-hole 5-pin type (A, B, C common, SW pair). MC3600-class; detented preferred for UX feel. |
| Tactile switches 6×6 mm | 2 | The two spare buttons. |
| Perfboard / stripboard | 2 | Display header + power distribution; input board (encoder + buttons). |
| Female header strips | as needed | Socket the display module for swapping. |
| Male pin headers + DuPont jumpers | ~20 | SPI leads **≤ 10 cm**. |
| Decoupling: 100 nF ceramic ×4, 10 µF electrolytic ×2 | — | At the display module VDD and input-board corners. |
| Series resistors 330 Ω (optional) ×3 | — | On SCK/MOSI/CS if ringing shows at high clock. |
| Slide switch (optional) | 1 | Clean hard resets on the USB feed. |

Power budget: SSD1322 module ≈ 20–40 mA typical, worst ~100 mA full-white
(onboard charge pump). Pico 2W 3V3(OUT) ~300 mA (5 V at VSYS); Wi-Fi bursts
take their share. **Prototype rule: OLED on 3V3(OUT), watch for brownout
resets at full-white; if one appears, feed the panel from a separate 3.3 V
supply (common GND) and record it as a design constraint.** Never back-power
the Pico through its 3V3 pin.

---

## 4. Pin map and wiring

Arch §3.2 block, with the owner's draft input swap (GP14–18 unchanged as a
block):

| Signal | Pico pin | GPIO | Wire notes |
|---|---|---|---|
| OLED SCK (SPI0) | 4 | 2 | **not GP7 — see SPI pin-table note**; ≤ 10 cm, keep parallel with GND |
| OLED MOSI (SPI0) | 5 | 3 | SPI0 MOSI must be 3/7/19/23; MISO unused (write-only panel) |
| OLED CS | 12 | 9 | plain GPIO — any pin legal |
| OLED DC | 14 | 10 | |
| OLED RES (RST) | 15 | 11 | active-low; optional but keep it |
| OLED VCC | 36 | 3V3(OUT) | decouple 10 µF + 100 nF at the module |
| OLED GND | 38 | GND | |
| Encoder A | 19 | 14 | pull-up; quadrature — see §6 P2 |
| Encoder B | 20 | 15 | pull-up |
| Encoder push (SW) | 21 | 16 | one side of the switch pair, to GND |
| BTN1 | 22 | 17 | switch to GND, `Pin.IN + PULL_UP`, active-low |
| BTN2 | 24 | 18 | " |

**SPI pin-table note (verified 2026-10-09 against `ports/rp2/machine_spi.c`):**
MicroPython enforces the RP2040-style SPI function table **even on RP2350**
(despite the chip's flexible IO mapping): SPI0 SCK ∈ {2, 6, 18, 22}, MOSI ∈
{3, 7, 19, 23}. Arch §3.2's SCK=GP7/MOSI=GP8 raises `bad SCK pin` at init —
deviation: **SCK=GP2, MOSI=GP3** (CS/DC/RES are plain GPIO, unchanged).
GP6/GP7/GP8 stay free for the future direct-OT plan; GP18 stays free for
BTN2. Flag arch §3.2 for correction.

All inputs use the RP2350 internal pull-ups (~56 kΩ); the encoder's common
pin and the switch second pins go to the GND rail.

Deliberately untouched: GP0/GP1 (future OTGW UART0), GP4/GP5 (future I2C0 —
also the sanctioned SSD1306 fallback, §9; note GP4/5 carry **I2C0**, not
I2C1: I2C1 SDA ∈ {2,6,14,18,26}, so `I2C(1, sda=Pin(4), scl=Pin(5))` raises),
GP6–GP8 (freed by the SPI pin-table deviation above; future direct-OT),
GP12/GP13 (future 1-Wire), GP23/24/25/29 (CYW43 radio lines).

**Pin budget (Pico 2 W):** OTGW UART0 GP0/1 + OLED GP2/3/9/10/11 + I2C0
GP4/5 + 1-Wire GP12/13 + encoder GP14/15/16 + buttons GP17/18 = **16
allocated**. Free inside GP0–GP24: **7** (GP6–8, GP19–22) — GP23/GP24 are
NOT free, they are CYW43 lines (WL_REG_ON / WL_DATA+IRQ). GP26–28 are
additionally usable (ADC). Free-block reservations: GP6–8 direct-OT (GP8/9
can also carry UART1; CS moves to any free GPIO), GP19/22 backup SPI0 set
(the third MOSI candidate GP23 is a radio line — drop it), GP20/21 general.
All pair choices verified against MicroPython's function tables (UART0
default GP0/1; SPI0 SCK∈{2,6,18,22} MOSI∈{3,7,19,23}; I2C0 GP4/5 valid).

**15-pin module note (owner's stock, 2026-10-09):** the 2.8" SSD1322
modules with 15-pin headers are parallel+serial variants — wire ONLY the
7 SPI signals above (D0=SCLK, D1=MOSI, CS, DC/DS/A0, RES/RST, VCC, GND);
leave the parallel data lines and NC pins unconnected. Interface straps
(BS0/BS1 pads on the back) are already set to 4-wire SPI on modules that
worked out-of-box on ESP32. Proven reference driver (owner's ESP32 devices,
years in production): `sle118/squeezelite-esp32``components/display/SSD1322.c`
— 4-wire SPI, 4bpp, **shadow-buffer dirty-diff in production** (validates
§7.8's diff approach). Its byte-swap + column-end `Width/4-1`=63 come from
gds's 4-pixel-column model; the datasheet column unit is ONE BYTE (2 px), so
the correct default is `0x7F` with **no swap** (MicroPython `GS4_HMSB`
matches the native packing) — the `0x3F` variant only makes sense paired
with the byte-swap, never mixed. Contrast register is `0xC1` (`0x81` is the
SSD1306 register, undefined here). Init base (verified 2026-10-11 against
u8g2 + datasheet): `u8x8_d_ssd1322_256x64_init_seq` — it sends `FD 12`
command unlock, `B9` linear grayscale table and `AB 01` internal-VDD, which
the circulated tables omit. Squeezelite's own values differ from what an
earlier revision of this doc attributed to it: clock `B3 0x91`, phase
`B1 0xE2`, pre-charge `BB 0x1F`, contrast `C1` only, no `B4` at all.

---

## 5. Perfboard build

Two boards, wired in this order (each step is a test point):

1. **Power rail board:** 3V3 and GND rails from the Pico; 10 µF bulk; female
   header for the display module at the board edge; 100 nF directly across
   the module's VDD/GND pads.
2. **SPI fan-out:** short stubs from the display header to DuPont headers to
   the Pico. One GND wire back to the Pico, star-connected at the bulk cap.
3. **Input board:** encoder (A/B/SW) + 2 buttons; keep encoder leads short
   (its edges are the timing-critical signals); 100 nF across A/B to GND is
   an optional hardware-debounce variant to test against software handling.
4. Label every wire. Photograph the wiring before power-up.

---

## 6. Bring-up phases

Standalone scripts on the bench Pico (provisioned with
`micropy-system/tools/target/provision.py`, or Thonny for the first
scripts). None of this touches the production board.

### P0 — Power-up smoke (no code)

- Feed 5 V USB, measure 3V3(OUT) with the display seated. Expect ≥ 3.2 V.
- Any display glow / Pico reset = wiring fault before proceeding.

### P1 — Display bring-up (`proto/display_bringup.py`, bench-only)

The script now exists: **`proto/display_bringup.py`** (standalone, zero
project imports — patterns, clock sweep, scroll demo, timing + heap
report; auto-detects RP2040 vs RP2350 via `os.uname().machine` and picks
the right SPI0 TX DREQ, so it runs on either bench board; run with
`mpremote run proto/display_bringup.py`). Its shape:

```python
from machine import Pin, SPI
spi = SPI(0, baudrate=5_000_000, polarity=0, phase=0,
          sck=Pin(2), mosi=Pin(3))          # NOT GP7/GP8 — see §4 note
cs, dc, res = Pin(9, Pin.OUT), Pin(10, Pin.OUT), Pin(11, Pin.OUT)
```

Record in the prototype log:

- **Controller identification per module** (SSD1322 vs SH1322 init probe) —
  the on-hand stock is mixed until proven.
- SPI clock sweep: **5 → 10 MHz** (SSD1322 SPI is spec'd to ~10 MHz — do
  NOT chase 12.5; the panel, not the RP2350, is the ceiling). First clock
  with artifacts is the ceiling; pick ½–⅔ of it as the design clock.
  rp2 `machine.SPI` rounds the requested clock **down** (pico-sdk
  `spi_set_baudrate`: even prescale × postdiv, actual ≤ requested):
  5 MHz → 4.81 MHz (125/26), 10 MHz → 8.93 MHz (125/14). The sweep prints
  the actual clock from the SPI repr — judge all wire numbers against it.
- Full-frame transfer time (`ticks_diff` around `spi.write`) — **8192 B at
  10 MHz ≈ 6.6 ms** (at 5 MHz ≈ 13.1 ms; at the achievable 8.93 MHz
  ≈ 7.3 ms); the fps duty math is §7.6. The
  script reports µs-resolution **draw-vs-wire splits** ([4]) and a
  **dirty-row partial push** ([4b], 16 rows ≈ 1.3 ms @10 MHz) so the
  §7.6 production shape is measured, not assumed.
- `gc.mem_free()` before/after driver + framebuffer alloc.
- Full-white / full-black / checkerboard / **16-step grayscale ramp**, 1 h
  soak, contrast register sweep (record the chosen contrast — grayscale
  legibility depends on it).

**Pass:** stable image at the chosen clock for 1 h, no brownout at
full-white, full-frame write ≤ 7.5 ms at the highest artifact-free clock
(the requested 10 MHz runs at 8.93 MHz actual → 7.3 ms wire alone),
driver + 8 KB framebuffer ≤ ~12 KB heap.

### P2 — Input bring-up (`proto/input_bringup.py`)

**Encoder (the timing-critical one):** quadrature edges at fast twist
(~10 detents/s) change state every few ms — **20 Hz polling WILL miss
detents.** Test both shapes:

- **(a) Fast poll:** read A/B every 2–5 ms in a tight loop with
  `time.sleep_ms` (releases the GIL — free for core 0). This is the shape a
  core-1 UI thread does naturally; measure its fidelity + CPU cost.
- **(b) `Pin.irq` on A and B** → `micropython.RingIO` carrying edge stamps,
  folded into steps by the app — the docs explicitly bless RingIO as
  **IRQ/thread-safe when used single-direction** (write in the IRQ, read
  in the app). IRQ rules apply: no allocation in the handler; if
  `micropython.schedule` is used to escape the IRQ, a bound method must be
  captured in the constructor first (creating a bound-method reference
  in-IRQ allocates — same trap family as MICROPYTHON-GOTCHAS §1). The
  schedule queue is finite (`RuntimeError` when full) — a fast-twist storm
  must not overflow it; RingIO drain is the bounded alternative.

Record: missed/extra detents per shape at slow/normal/fast twists, push-
switch double-events over ~200 presses, press-and-hold repeat (config value
stepping — decide repeat rate, e.g. 300 ms after 500 ms hold).

**Pass:** chosen shape gives 0 missed / 0 phantom detents at fast twist;
0 double events, 0 missed presses on the push + 2 buttons.

### P3 — Combined + load + the core-1 experiment (`proto/ui_under_load.py`)

The decision engine for §7. Run the full app stack on the bench board
(`transport: "otgw_dummy"`) and compare UI shapes:

| Shape | What it is |
|---|---|
| S1 baseline | UI as a `TaskManager` periodic task in the uasyncio loop (30 fps) |
| S2 core-1 | UI as a `_thread` on core 1 (framebuffer + SPI pump + input poll in the thread) |

Measure for BOTH:

- Event-loop jitter: 1 s heartbeat task, max gap with UI on vs off.
- **GIL behavior probe:** does `SPI.write` on the thread block core 0's
  heartbeat for the frame duration? (If yes, the thread buys scheduling
  isolation, not SPI overlap — record it; gotchas-catalog candidate.)
- **Thread stack cost:** explicit `stack_size` (3rd arg of
  `start_new_thread`); find the floor with `micropython.stack_use()`
  sampled at the UI loop's deepest point; record the heap delta.
- Heap trend over a 24 h soak (`gc.mem_free()` hourly;
  `micropython.mem_info()` at start/end for the allocation-shape picture)
  — the long-uptime ENOMEM/fragmentation watch item must not get worse
  with the UI on.
- **fps sustain:** 30 fps full-frame in both shapes (the minimum bar);
  then the 60 fps stretch via dirty-row partial updates (§7.6) —
  heartbeat gap recorded at each point. The duty math says 60 fps
  full-frame is the one configuration to avoid; prove it rather than
  assume it.
- **Animation stress scene (§7.9):** scrolling list (memmove trick) +
  animated progress bar + AA text at target cadence — measure achieved
  fps and heartbeat gap in S1/S2, with and without the async shim +
  double buffering. This is the owner's smooth-UI acceptance scene; the
  verdict on whether the async patch leaves the shelf lives here.
- Input fidelity under load (encoder during a CCU3 poll burst).
- Resilience bonus: with S2, force a network retry storm (dead CCU3 IP) and
  confirm the UI keeps rendering last-known state.

**Pass:** 24 h, no crash/reboot, no heap decline trend, heartbeat gap
< 1.5 s in the chosen shape, encoder clean under load, and a recorded
S1-vs-S2 verdict.

### P4 — Production integration (after P1–P3 pass)

Prototype outputs feed it directly: chosen clock, contrast, debounce
constants, repeat rate, encoder read shape, S1-vs-S2 verdict, stack budget.
Shape per §7: framework plumbing + `app/display.py` (panel driver) +
`app/buttons.py` (nav state machine) + §11 layouts wired to the sectioned
config (`config_schema.py`) — writes go through the same validate +
atomic-persist path the shell uses (one flash write per human edit; UI logs
are INFO/console only, never per-tick flash writes). Host-testable logic
(nav, layout formatting) gets host tests; the driver gets a live autotest
suite (`autotests/test_display_live.py`) following the proof-suite pattern.

---

## 7. Core-1 UI architecture (owner direction, 2026-10-09)

### 7.1 The GIL facts first (correcting a common confusion)

The "GIL recently disabled, things got faster" news is **CPython 3.13/3.14
free-threading (PEP 703)** — it does not apply to MicroPython. MicroPython
still builds with `MP_THREAD_GIL=1` on the rp2 port, including the official
v1.29.0 UF2 we pin in `provision.py`. On that build:

- **Python bytecode runs on one core at a time.** `_thread` gives
  *scheduling isolation* (the thread's `sleep_ms` waits release the GIL and
  cost core 0 nothing) and overlap **only inside C-level calls that release
  the GIL**. Whether `SPI.write` releases it is unverified for our build —
  P3 measures it.
- A no-GIL build (`MP_THREAD_GIL=0`) is a **compile-time** option, so it
  means custom firmware: we'd build and pin our own UF2 (the official
  micropython.org v1.29.0 pin in `provision.py` goes away, every board
  re-provisioned; OTA itself is unaffected — it never touches the
  MicroPython base). The real hazard is not performance but **memory**:
  the GIL also protects the single shared heap and the GC. With it off,
  concurrent allocations from two threads corrupt the heap, and nothing
  in our stack (uasyncio, lwIP, coresys, logger, JSON) is written for
  that. The safe GIL=0 pattern requires an allocation-free core-1 loop
  (pre-rendered buffers, no formatting, locks around shared state) —
  C discipline in MicroPython. Note that PIO+DMA (§7.6) delivers the
  actual prize — refresh that never touches the VM — **with the GIL
  still protecting everything else**. Do not go no-GIL unless P3 data
  proves nothing else suffices.

So "core 1 handles the UI solo" is real for *timing* (the UI loop's cadence
and blocking waits stop being the event loop's problem) and for *resilience*
(UI keeps rendering during a network storm), but it is NOT two cores running
Python layout code in parallel. The prototype's job is to quantify all of
this, not assume it.

### 7.2 Framework / app split (owner proposal, adopted for the prototype)

- **Framework (`micropy-system/src/lib/coresys/`)** owns the generic,
  panel-agnostic plumbing on core 1:
  - the `_thread` lifecycle: start with explicit `stack_size`, crash
    containment, restart policy;
  - a **4bpp grayscale framebuffer** (configurable width/height — 8 KB for
    256×64, `framebuf.GS4_HMSB`, allocated once at boot) with **dirty-row
    tracking** (§7.6); double-buffering (16 KB) only if P3 shows visible
    tearing on partial updates;
  - the **SPI pump**: pins / SPI id / baud / fps from config; pushes the
    dirty buffer with one `spi.write` per frame;
  - a **heartbeat stamp** the core-0 side can check.
- **App (`app/display.py` + UI code)** owns everything panel-specific —
  deliberately, because panel variants (SSD1322 vs SH1322 clones, offsets,
  contrast) are too many to generalize:
  - init sequence, contrast, column/row offsets;
  - §11 layouts, nav state machine, input semantics.

### 7.3 The handoff contract (thread-safety under the GIL)

- App draws into the **back buffer**, then sets a single dirty/swap flag
  (one assignment — atomic under the GIL). The core-1 thread swaps buffers
  and pumps. Never share compound mutable state across the boundary.
- The core-1 thread **never touches uasyncio objects**, sockets, or the
  config store. The loop publishes a plain snapshot (dict of scalars /
  pre-formatted strings); the thread only reads it.
- The display driver instance is touched by exactly one thread (core 1).
- **`micropython.schedule` crosses the boundary (documented GIL behavior):**
  with the GIL enabled, a scheduled function "can preempt any thread and
  run in its context", and `time.sleep_ms` (even 0 ms) runs pending
  scheduled functions. So a callback scheduled from an IRQ may execute in
  the **UI thread's** context, not the main loop's — the UI thread's
  `sleep_ms` loop is not an isolated world. Either confine `schedule()`
  producers to core-0-side code, or make every scheduled callback touch
  only the same narrow shared state the thread already reads.

### 7.4 Supervision (the framework-ownership gap this must close)

A raw `_thread` escapes `TaskManager` and the WDT story (AGENTS.md: the
framework owns lifecycle; the app must not own the watchdog). The design
closes it inside the framework primitive:

- A core-0 `TaskManager` task checks the UI thread's heartbeat stamp; a
  stall → one edge-triggered WARN (flash, per logging policy) + thread
  restart with a bounded retry count.
- The WDT stays fed by the core-0 loop as today: a wedged UI thread must
  never take heating down, and a crashing UI thread must never take the app
  down (containment mirrors `create_periodic_task`).
- Config home (P4 decision): board-level plumbing (SPI pins, size, fps,
  stack) fits `system-config.json` (like `AUTOTEST`); UI behavior keys would
  join `app-config.json` sections later.

### 7.5 What the encoder adds to the case

The encoder (§6 P2) needs 2–5 ms edge attention. In S1 that attention
competes with the event loop; in S2 it is the UI thread's natural tight
loop with `sleep_ms` waits that are free for core 0. If P2 shows fast
polling beats IRQ cleanly, S2 gains a concrete functional argument beyond
isolation.

### 7.6 Grayscale bandwidth + the fps decision (owner target: ≥30 min, stretch 60)

SSD1322 is 4bpp grayscale: a full frame is 8192 B plus a few bytes of
window commands. The panel scans GRAM continuously (~330 Hz internal
multiplex), so 60 fps writes are electrically fine — the constraints are
SPI bandwidth and CPU/GIL, not the OLED drive:

| Mode | Bytes/frame | Write @10 MHz | duty @30 fps | duty @60 fps |
|---|---|---|---|---|
| Full frame | ~8.2 KB | 6.6 ms | 20% | **39%** |
| Dirty-row update (~20% of rows) | ~1.7 KB | 1.4 ms | 4% | 8% |

"Duty" = wall-clock consumed by SPI writes. **Why core 1 does not
automatically make this free: the GIL locks the interpreter, not a core,
and both cores share one interpreter.** A `_thread` on core 1 makes the
UI's *time* free for core 0 (scheduling isolation, `sleep_ms` waits), but
every call into the MicroPython runtime — `spi.write`, the framebuffer
draw, layout formatting — holds the GIL and stalls core 0's loop for its
duration. If P3's probe shows `SPI.write` holds the GIL across the
transfer, the duty above is stolen from core-0 Python **regardless of
which thread issues it**. If the probe shows it releases the GIL during
the transfer, full-frame 60 fps becomes viable and the table's worst case
evaporates — measure, don't assume.

Escape hatches if the probe lands on the bad branch, in order of sanity:
dirty-row updates (below), adaptive cadence, and **pure-Python async SPI
via `rp2.DMA`** — no custom firmware. Verified 2026-10-09 against the
**v1.29.0 docs**: `rp2.DMA` + `rp2.DMATimer` are in the pinned release
(earlier "no DMA API" claim in this doc was WRONG and is retracted).
`machine.SPI` already streams ≥32 B via DMA internally
(`ports/rp2/machine_spi.c`); the Python-level version:

```python
d = rp2.DMA()
ctrl = d.pack_ctrl(size=0, inc_write=False, treq_sel=24)  # DREQ_SPI0_TX (RP2350)
d.config(read=frame_buf, write=spi_obj,                    # SPI object
         count=8192, ctrl=ctrl, trigger=True)              # returns now
# completion: d.irq(handler) or poll d.active()
```

DREQ numbering (verified 2026-10-11 against pico-sdk `rp2350/regs/dreq.h` +
the RP2350 datasheet): the RP2350 **renumbered** DREQ vs RP2040 — SPI0
TX=24/RX=25, SPI1 TX=26/RX=27 (RP2040: 16/17, 18/19); PIO0 TX0-3 = 0-3,
PIO1 TX0-3 = 8-11, PIO2 TX0-3 = 16-19 (RX at +4). The MicroPython DMA docs'
`(pio<<3)+sm` formula agrees with the datasheet for TX FIFOs. The
renumbering is a gotchas-catalog entry (it bit tinygo's SPI DMA the same
way): `treq_sel=1` on RP2350 is PIO0_TX1 — the channel never triggers and a
`while d.active()` wait hangs forever.

This works because the SPI object exposes its data register via the
buffer protocol (`machine_spi_get_buffer` in machine_spi.c) — the DMA
docs' own example streams a bytearray into a PIO SM's FIFO the same way.
RX FIFO overrun while write-only is harmless (discard + clear flag).
Keep the framebuffer referenced and untouched mid-transfer (double
buffer). A PIO bit-bang pipeline (`treq_sel=(pio<<3)+sm`) is reachable
the same way but buys nothing for a plain SPI-0/0 panel the PL022 block
serves. CS note: PL022/MicroPython never assert CS — strap the OLED's CS
permanently low (single slave); DC is one pin write before the trigger.

**Recommended shape (P3 to validate):** single 8 KB framebuffer + a
**dirty-row bitmask** (one Python int, 64 bits — one bit per row) + push
contiguous dirty-row runs via SSD1322 column/row window addressing. No
double buffer, no per-pixel diffing; a text-UI update (a few lines) costs
~1–2 KB per frame. Adaptive cadence on top: idle screens drop to 1–5 fps,
**30–60 fps while interacting** — which also saves SPI traffic and panel
wear.

### 7.7 Driver layering: what is C and what is Python (decided 2026-10-09)

| Layer | Language | Ships via | Why |
|---|---|---|---|
| Async SPI split (`write_start`/`write_busy`) | C patch to the rp2 port (~40 lines, split of `machine_spi_transfer`) | custom UF2, pinned in `provision.py`, owned by `micropy-system` | DMA channel lifecycle is the only thing that must be C |
| SSD1322/SH1322 driver (init tables, window cmds, contrast, framebuffer + dirty rows) | **pure Python** (`app/display.py`) | the normal slot build — `mpy-cross` compiles it to `.mpy` today; OTA-updatable, host-testable with a fake SPI | clone init quirks (SH1322) are exactly the fix you want to ship over the air; hot path is already C (`framebuf` + SPI) |
| Layouts, nav, config screens | Python (app) | slot / OTA | domain code |

- **"Do we compile the driver?"** Yes — but as bytecode through the existing
  `build_firmware.py` pipeline, not native code. No new toolchain for the
  driver; the only new toolchain in the whole feature is the custom UF2,
  and that is gated on P3 data.
- **Capability shim:** the driver picks `_stream` = async start/poll if
  `hasattr(spi, "write_start")`, else plain `spi.write` (which on stock
  firmware already DMA-streams ≥32 B transfers, just synchronously). The
  prototype and the first production UI therefore run on the **official
  pinned UF2**; the patched build is a drop-in upgrade behind the shim.
- Framework-vs-app placement note: the generic framebuffer pump idea can
  start in the app slot — board `/lib/coresys` only refreshes at
  provisioning/USB push (AGENTS.md), and panel plumbing is precisely the
  part that wants OTA iteration. Promote it to the framework once stable.
- **Revisited same day in §7.8**: owner leans to a full-C display *engine*;
  the Python-driver row above then shrinks to personality + UI (init
  tables, contrast policy, layouts) on top of the C engine.

### 7.8 Full-C display engine (owner lean, 2026-10-09) — speed motivation refuted, default is Python

Speed breakdown (why a full-C driver is NOT a tremendous speedup): the
transfer is **wire-bound** (10 MHz panel ceiling) and stock `spi.write`
**already DMA-streams it in C**; drawing is `framebuf`, also already C.
Python-vs-C interpreter overhead is ~0.1 ms/frame of command/bookkeeping —
< 1% at any operating point (30 fps dirty-rows: ~0.3% of CPU; 60 fps
full-frame: the 39.6% is the wire, identical for both). C's real deltas
are orthogonal to driver language: the async/pipelined transfer (now
**pure Python via `rp2.DMA`**, §7.6 — no C patch needed) and static
`.bss` buffers (marginal — MicroPython's GC never moves objects; a
`bytearray` allocated once at boot, before fragmentation, is equally
DMA-stable).

**Default decided: whole driver in MicroPython on the official pinned
UF2, no fork** — dirty rows + adaptive cadence, async/pipelining via
`rp2.DMA` if the animation benchmark demands it. The full-C engine
design below stays documented as an option; after the `rp2.DMA` finding
its honest motivations shrink to static buffers and Arduino
familiarity, not throughput and not async.

Owner leans to writing the whole SSD1322 driver in C (years of 24/7 Arduino
C experience with these panels — the command-protocol knowledge ports
directly). Where it is strictly better than a Python driver:
**shadow-diff dirty-row tracking in C** (memcmp gram vs shadow, ~tens of
µs) works with any framebuf drawing, no Python bookkeeping — though the
Python driver avoids the need with draw-wrapper dirty marking instead.

The cut line that keeps it safe: **C = engine, Python = personality.**

- **C module `ssd1322.c`** (one additive file + 2 config lines): owns SPI0,
  a permanently-claimed DMA channel, and **static `.bss` buffers** —
  `gram[8192]` + `shadow[8192]` (GC-immune, never fragmented, DMA-stable;
  ~16 KB off the top of the heap). Methods: `init(table_bytes)` (personality
  from Python), `cmd(bytes)`, `buf()` (buffer-protocol memoryview so
  `framebuf.GS4_HMSB` draws directly into C-owned GRAM), `flush()` (diff ->
  window cmds -> DC high -> DMA start -> return in µs; completion IRQ
  clears a flag), `busy()`. Per-frame VM touch: microseconds; the 6.6 ms
  transfer involves no thread, no GIL, no interpreter. (Note: since the
  §7.6 `rp2.DMA` finding, this module would be a convenience wrapper
  around what Python can already orchestrate — its justification is now
  marginal.)
- **Python (app, OTA'd)**: init tables per clone (SSD1322 vs SH1322),
  contrast policy, layouts, nav, UI. Clone fixes stay OTA-able.

Embedding facts (correcting the "compiled blob" mental model): there is
**no blob and no runtime loading** — C is statically linked into the UF2
at build time. Build = vendored MicroPython pinned at v1.29.0 + patch set
(`SRC_C += ssd1322.c`; `MICROPY_PY_SSD1322` + `MP_PORT_MODULES` entry in
`mpconfigport.h`; QStrs auto-generated), `make BOARD=PICO2_W` with
gcc-arm-none-eabi + pico-sdk, UF2 published by CI, `provision.py` pin
swapped to our artifact (same `.cache/` mechanism). OTA/A-B unchanged —
it never touches the MicroPython base.

Costs, pinned:

1. **The C layer is provisioning-only** (no OTA for the base; self-flashing
   XIP flash is off the table). Hence the personality-in-Python rule.
2. **No host tests for the C module** — device autotest
   (`autotests/test_display_engine_live.py`: flush/busy/diff against the
   real panel) + selftest instead; keep the C protocol logic small.
3. **We own a MicroPython base**: every upstream upgrade = rebase + rebuild
   + re-provision. One additive file + two config lines is what makes that
   survivable; keep it upstream-shaped.

### 7.9 Smooth animation UI (owner requirement, 2026-10-09)

Owner wants vector animations + smooth scrolling, a modern UI that feels
performant. Consequences for the §7.6 math:

- **Dirty-row optimization evaporates during animation** — a pixel-smooth
  scroll dirties every row every frame. Animation runs in full-frame mode:
  wire 6.6 ms dominates. 60 fps budget (16.6 ms): wire 6.6 + framebuf
  redraw ~1–3 + Python glue ~0.5 → feasible but tight; 30 fps comfortable.
- **Pipelining is the unlock**: `rp2.DMA` (§7.6, pure Python) + double
  buffering (2×8 KB) → start DMA for frame N, draw N+1 while it streams,
  check `d.active()`/IRQ at frame end. Hides the wire time; no custom
  firmware involved. Reframes the async benefit: not core-0 protection,
  but wire/render overlap.
- **Vertical scroll = buffer memmove, not blit**: GS4_HMSB rows are 128 B,
  so 1-px scroll is `buf[:-128] = buf[128:]` (C-speed memcpy, µs) + draw
  the exposed edge row. The key trick for smooth Python list scrolling.
- **Horizontal scroll: even pixels only** (byte shift per row); odd-pixel
  nibble surgery is expensive in Python — design menus to scroll vertically
  and marquees to step 2 px.
- **Grayscale is the "modern" lever**: precomputed 4bpp anti-aliased glyph
  tables for text; the built-in 8×8 bitmap font will look like a
  calculator. AA text is what makes a 16-level panel feel premium.
- **60 fps verdict (2026-10-09, pending P1)**: pipelined double buffering
  via `rp2.DMA` fits the 16.67 ms budget — wire 6.55 ms @10 MHz (39%
  duty, hardware, overlapped) + CPU draw ~3–4 ms; ~10 ms headroom/frame.
  Gated on the P1 clock sweep: ≥8 MHz wires = 60 fps full-frame viable;
  5 MHz = 30 fps full-frame floor, 60 fps only via dirty runs. One thing
  double buffering can't fix: no vsync readback, so a frame landing
  mid-GRAM-scan (~330 Hz) can theoretically tear — P3's scroll scene
  checks whether it's ever visible.
- **Tiny tween engine** (Python): lerp + ease-in/out, 150–300 ms
  micro-interactions (page slides, value rolls, animated bars). Vector
  primitives (framebuf line/rect/circle in GS4) are C and cheap — the
  wire was always the cost, and pipelining hides it.

### 7.10 Core-0 interference channels + the hiccup defenses

The coupling channel is the GIL, not the core: all MicroPython code (S1 or
S2) queues through one interpreter, so core-0 work can delay a frame in
either shape. S2 removes *scheduling* jitter, not *GIL* jitter.

Typical occupancy (measured expectation, P3 verifies): control tick ~ms/
min, weather parse few ms/min, rooms pass ~1–2% during its ~47 s, re-assert
µs — **~2–3% total, invisible at 30 fps**. The hiccup sources are specific
and enumerable:

| Source | Worst case | Defense |
|---|---|---|
| GC passes (any allocation anywhere triggers) | ~5–30 ms | time-based animation (below); boot-time allocation of buffers/glyphs; lean per-frame path (pre-format strings on value change, not per frame) |
| Rooms rediscovery (hourly; ~50 × 10–50 KB parses) | tens of ms per device, minutes long | accept it, or deliberately drop UI to 15 fps during a discovery sweep |
| Busy-waits — OTGW `_await_ack` spin (up to 2 s, PLAN.md deferred) | full freeze, **S1 and S2 alike** (the spin holds the GIL) | the interim `sleep_ms` fix becomes a UI prerequisite |
| ENOMEM retry storm (watch item) | frame drops | UI degrades, never dies (S2 resilience bonus) |

**The #1 rule: time-based animation.** Tween state = f(`ticks_diff`
elapsed), never a frame counter. A GC pause then *skips* a frame instead
of shifting the animation's phase — drops become perceptually free.

**Owner decision (2026-10-09): no preemptive optimization.** P3 decides
which of these are actually nasty; then fix surgically, one at a time.
Example already on the list: rooms re-discovery demoted from hourly-auto
to a UI menu action — `rooms_rediscovery_s` set high disables the sweep
(existing key), and "rescan rooms" becomes a menu verb (also better UX:
the human decides when the room list is rescanned).

P3 additions: run the animation scene with the full stack live, plus
torture variants — force a rooms re-discovery mid-animation (count
dropped frames) and a dead-OTGW-link spin scenario.

---

## 8. Acceptance matrix (prototype exit criteria)

| # | Criterion | Evidence |
|---|---|---|
| A1 | Panel works on SPI0 GP2/3 + CS/DC/RST GP9/10/11 at a recorded clock ≥ 5 MHz | P1 log, 1 h soak |
| A2 | Full-frame write ≤ 7 ms at the design clock (10 MHz target) | P1 timing log |
| A2b | 30 fps full-frame sustained (the minimum bar); 60 fps stretch via dirty-row/pipelining — heartbeat gap < 1.5 s in both | P3 fps log |
| A3 | No brownout with Wi-Fi + full-white OLED on 3V3(OUT) | P1/P3, rail measurement |
| A4 | Encoder: 0 missed / 0 phantom detents at fast twist, chosen read shape | P2 log |
| A5 | Buttons/push: 0 double / 0 missed at the chosen debounce | P2 press log |
| A6 | 24 h combined soak: no crash, heap flat, heartbeat gap < 1.5 s | P3 hourly log |
| A7 | §11 layouts render legibly at 256×64 | photos |
| A8 | S1-vs-S2 verdict recorded: heartbeat gap, SPI GIL probe, stack floor | P3 comparison log |
| A10 | Animation scene: ≥30 fps sustained (60 attempted), heartbeat gap < 1.5 s, scroll visibly smooth | P3 animation log |
| A9 | Recorded design constants: SPI clock, contrast, debounce, repeat, encoder shape | prototype log → arch update |

---

## 9. Risks and open questions

1. **Clone variance (highest, and now certain):** the on-hand modules are
   probably SH1322. P1 identifies each module's controller; the app-side
   driver must carry both init sequences if the stock is mixed.
2. **GIL ceiling:** if P3 shows `SPI.write` holds the GIL and the frame
   time matters, the options are partial updates (dirty rows only) before
   they are custom no-GIL firmware. Record, don't speculate.
3. **Fallback path:** if the SPI panel is a dud, the arch-sanctioned
   fallback is a 128×64 SSD1306/SH1106 on **I2C0 GP4/5** with a §11 layout
   redesign (§3.3 allows it). Leave GP4/GP5 pads available.
4. **Input finalization:** encoder + 2 buttons is the owner's *draft*;
   arch §3.2 still says 5 buttons. Settle at P4 and update the arch either
   way.
5. **Legibility:** boiler-room placement — judge at P3 with photos.
6. **Power:** if full-white + Wi-Fi browns the rail, the enclosure needs a
   separate 5 V feed (VSYS) — record it.
7. **Arch §3.3 "ST7567" claim:** retail ST7567 modules are usually 128×64
   LCDs, not 256×64 OLEDs. SSD1322 is the primary; correct the arch note
   after the prototype.

---

## 10. Prototype log template (keep appending to this file or a sibling)

```
date | phase | module (SSD1322/SH1322) | SPI clock | frame ms | contrast | heap free | notes
```

Outcomes that must land back in the repo when the prototype passes:
arch §3.2 (input block + SPI pins) + §3.3 (exact module + clock +
contrast) updates, §11 layout tweaks, `MICROPYTHON-GOTCHAS.md` entries
(GIL/SPI probe result, `schedule()`/RingIO thread-boundary behavior,
`rp2.DMA` → SPI-FIFO/PIO-FIFO streaming on v1.29.0 + the RP2350 DREQ
renumbering (SPI0 TX 16→24), clone init findings, thread stack floor), a
framework `micropy-system` issue/plan for the core-1 runner primitive, and
PLAN.md re-scoped with the P4 integration steps.
