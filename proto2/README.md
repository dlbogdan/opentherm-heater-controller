# proto2 — SSD1322 clone bring-up (Pico 1)

Self-contained MicroPython bring-up ladder for an unbranded 256×64
SSD1322-class OLED module. One file, no imports to upload.

## Wiring (Pico 1 / RP2040)

| Signal | GP | Note |
|---|---|---|
| SCK  | GP18 | SPI0 SCK |
| MOSI | GP19 | SPI0 TX (bus is write-only) |
| CS   | GP17 | held low per transaction |
| DC   | GP20 | low = command, high = data |
| RES  | GP21 | active-low, 50 ms pulse |
| VCC  | 3V3(OUT) | not VBUS |
| GND  | GND | |

## Run

```sh
mpremote run proto2/bringup.py
```

Watch the panel **and** the serial output together — every step prints
what should be visible.

## Knobs (top of `bringup.py`)

- `COL_OFFSET = 28` — these 256×64 modules wire the panel to segment 112,
  i.e. column address 28 in 4-grayscale units (1 address = 4 pixels).
  A module wired to SEG0 would use 0.
- `WIDTH = 128` — if the right half is garbage (T4–T7), the module is
  128-wide, not 256.
- `USE_BITBANG = True` — software SPI on the same pins; rules out a
  hardware-SPI mode/phase problem.
- `INIT_HZ` / `FRAME_HZ` — both default to 1 MHz during bring-up; raise
  `FRAME_HZ` (e.g. 8 MHz) only after all probes render cleanly.

## What each step proves

| Step | Pattern | Isolates |
|---|---|---|
| T1/T2 | 0xA5 all-ON / 0xA6 back to normal | transport + power only (no RAM, no addressing) |
| T3 | RAM blank | RAM path is dark, no garbage |
| T4–T9 | single 16×16 block at x=0/64/128/192, y=48, then x=0 again | column reach (true width), row reach, and whether a 2nd write still lands |
| T10 | 8px checkerboard | 4bpp packing (high nibble = even column) |
| T11 | 16-band vertical gradient | gray LUT (`0xB9` linear) |
| T12 | contrast sweep 0x00→0xFF | `0xC1`/`0xC7` extended commands landed (the `0xFD 0x12` unlock worked) |

`show()` mirrors rdagger's `present()`: `0x15`/`0x75`/`0x5C` as separate
transactions (params as data), then the framebuffer as one DC=1 burst.

## Other facts baked in

- SPI **mode 0** — rdagger's working demo uses SPI defaults; the guide's
  mode-3 claim corrupts this module's multi-byte streams.
- `0xFD 0x12` unlock **before** any extended command.
- 256×64 @ 4bpp framebuffer = **8192 bytes** packed (the source guide's
  "2048" is wrong — that's the 1bpp monochrome size).

## Init + write-path provenance (important)

The init **and the low-level write path** are rdagger's — his demo is
verified working on this exact module. The attached bring-up guide is
wrong on several points:

| Guide said | Works on this module (rdagger) |
|---|---|
| SPI mode 3 (CPOL=1/CPHA=1) | **SPI mode 0** (SPI defaults) — mode 3 corrupts multi-byte streams |
| params with DC=0 | command byte DC=0, **parameter bytes as DATA (DC=1)**, separate CS per command |
| `0xA8 0x3F` multiplex | multiplex is **`0xCA 0x3F`** (`0xA8` is SSD1306) |
| `0xA4` "resume from RAM" | `0xA4` = **ENTIRE DISPLAY OFF** — normal mode is `0xA6` |
| `0xB2`/`0xB4` enhancement A/B | enhancement A = `0xB4` (2 params), B = **`0xD1`** (2 params) |
| `0xBB`=VCOMH, `0xBE`=VSL | `0xBB` = pre-charge, `0xBE` = VCOMH |
| column window `0x15 0x00 0xFF` | 4-grayscale units: 1 address = 4 pixels; panel starts at **column 28** (segment 112) → window `28..91` for 256 px |
| bare `0xAB` "SPI select" | `0xAB 0x01` = **internal VDD regulator ON** |

Reference: <https://github.com/rdagger/micropython-ssd1322> (demo runs at
25 MHz SPI on a real 256×64 SSD1322 + Pico).

## Troubleshooting map

| Symptom | Likely cause | Try |
|---|---|---|
| T1 shows nothing | SPI mode mismatch | `USE_BITBANG = True` |
| T1 shows nothing | interface hard-wired to 8080 | check module jumpers (R19–R22) |
| T1 OK, probes garbage | column window / offset | `COL_OFFSET`, `WIDTH = 128`, `0xA0` remap |
| Blocks land shifted horizontally | wrong `COL_OFFSET` | try 0 or 28 |
| Left half OK, right half garbage | 128-wide module | `WIDTH = 128` |
| Faint / no light | power/pre-charge defaults | tune `0xB4`, `0xBB`, `0xB6`, `0xBE` |
| T12 no brightness change | extended cmds locked | re-send `0xFD 0x12` before each extended write |
| Works at low clock, garbles fast | clone SPI margin | lower `FRAME_HZ` |
| Garbage on power-up only | short reset / power-on delay | already: RES 50 ms + 100 ms settle |
