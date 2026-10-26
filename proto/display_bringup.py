"""
display_bringup.py -- SSD1322 perf bench, VERIFIED recipe (2026-10-13).

Rewritten against SSD1322.md / proto2/bringup.py (rdagger-verified):
  SPI mode 0; params as DATA (DC=1); window 0x15 28 91 / 0x75 0 63;
  8192 B GS4_HMSB fb, one burst after 0x5C; reset 50/100 ms; no A4.

Stages:
  [1] heap cost of driver + framebuffer
  [2] single-frame wire time @ 1 MHz
  [3] clock sweep (actual SPI baud readback) -- WATCH THE PANEL for the
      first clock that shows artifacts; that's the ceiling
  [4] draw (framebuf) vs wire (SPI) split
  [5] partial push: one 8-row page (1024 B) -- the production shape
  [6] fps loop at the best clean clock

Run:  mpremote run proto/display_bringup.py
REPORT: sweep table + last artifact-free clock + fps.
"""

import time

import gc
from framebuf import FrameBuffer, GS4_HMSB
from machine import Pin, SPI

h0 = gc.mem_free()                          # baseline BEFORE any allocs

SCK, MOSI, CS, DC, RES = 18, 19, 17, 20, 21   # rdagger-verified pinout
INIT_HZ = 1_000_000
SWEEP = (1_000_000, 2_000_000, 4_000_000, 6_000_000, 8_000_000,
         10_000_000, 12_500_000, 16_000_000, 20_000_000, 25_000_000)
W, H = 256, 64
COL_OFF = 28
CONTRAST = 0x9F
WATCH_S = 2

spi = SPI(0, baudrate=INIT_HZ, firstbit=SPI.MSB,
          sck=Pin(SCK), mosi=Pin(MOSI))          # MODE 0
cs = Pin(CS, Pin.OUT, value=1)
dc = Pin(DC, Pin.OUT, value=0)
res = Pin(RES, Pin.OUT, value=1)

fb_buf = bytearray(W * H // 2)
fb = FrameBuffer(fb_buf, W, H, GS4_HMSB)


def cmd(command, *args):
    dc.value(0)
    cs.value(0)
    spi.write(bytes((command,)))
    cs.value(1)
    if args:
        dc.value(1)
        cs.value(0)
        spi.write(bytes(args))
        cs.value(1)


def data(buf):
    dc.value(1)
    cs.value(0)
    spi.write(buf)
    cs.value(1)


def hard_reset():
    res.value(0)
    time.sleep_ms(50)
    res.value(1)
    time.sleep_ms(100)


def init():
    cmd(0xFD, 0x12)
    cmd(0xAE)
    cmd(0xB3, 0x91)
    cmd(0xCA, 0x3F)
    cmd(0xA2, 0x00)
    cmd(0xA1, 0x00)
    cmd(0xA0, 0x14, 0x11)
    cmd(0xB5, 0x00)
    cmd(0xAB, 0x01)
    cmd(0xB4, 0xA0, 0xFD)
    cmd(0xC1, CONTRAST)
    cmd(0xC7, 0x0F)
    cmd(0xB9)
    cmd(0xB1, 0xE2)
    cmd(0xD1, 0xA2, 0x20)
    cmd(0xBB, 0x1F)
    cmd(0xB6, 0x08)
    cmd(0xBE, 0x07)
    cmd(0xA6)
    cmd(0xA9)
    cmd(0xAF)


def present():
    c0 = COL_OFF
    cmd(0x15, c0, c0 + W // 4 - 1)
    cmd(0x75, 0x00, H - 1)
    cmd(0x5C)
    data(fb_buf)


def present_page(y0):
    """One 8-row page -- partial-update shape for the production UI."""
    c0 = COL_OFF
    cmd(0x15, c0, c0 + W // 4 - 1)
    cmd(0x75, y0, y0 + 7)
    cmd(0x5C)
    data(fb_buf[y0 * (W // 2):(y0 + 8) * (W // 2)])


def wire_ms(fn):
    t0 = time.ticks_us()
    fn()
    return time.ticks_diff(time.ticks_us(), t0) / 1000.0


def approx_baud(hz):
    """rp2 port has no spi.baudrate() readback. RP2040 SPI divides the
    125 MHz peripheral clock by an EVEN integer >= 2."""
    div = 125_000_000 // hz
    if div < 2:
        div = 2
    if div & 1:
        div += 1
    return 125_000_000 // div


print("SSD1322 perf bench (verified recipe)")

gc.collect()
print("[1] heap free at boot: %d" % h0)

hard_reset()
init()
gc.collect()
print("    after driver+fb: %d (driver+fb cost %d B)" % (gc.mem_free(), h0 - gc.mem_free()))

fb.fill(15)
print("[2] one frame @1MHz: %.2f ms" % wire_ms(present))
time.sleep(WATCH_S)

print("[3] clock sweep -- report first clock with artifacts")
best = 1_000_000
for hz in SWEEP:
    spi.init(baudrate=hz)
    fb.fill(0)
    for y in range(0, H, 8):
        for x in range(0, W, 16):
            if ((x >> 4) + (y >> 3)) & 1:
                fb.fill_rect(x, y, 16, 8, 15)
    ms = wire_ms(present)
    print("    req %9d -> ~actual %9d : %6.2f ms/frame"
          % (hz, approx_baud(hz), ms))
    best = hz
    time.sleep(WATCH_S)

print("[4] draw vs wire split @ %d" % best)
spi.init(baudrate=best)
t0 = time.ticks_us()
fb.fill(0)
fb.text("DRAW+WIRE SPLIT ABCDEFGHIJ", 10, 24, 15)
t1 = time.ticks_us()
ms_w = wire_ms(present)
print("    draw=%.2fms  wire=%.2fms" % (time.ticks_diff(t1, t0) / 1000.0, ms_w))
time.sleep(WATCH_S)

print("[5] partial push: one 8-row page (1024 B)")
ms_p = wire_ms(lambda: present_page(24))
print("    page wire=%.2fms" % ms_p)
time.sleep(WATCH_S)

print("[6] fps loop @ %d" % best)
n = 30
t0 = time.ticks_us()
for i in range(n):
    fb.fill(0)
    fb.text("FPS FRAME %02d" % i, 10, 10, 15)
    fb.fill_rect(10 + (i * 8) % (W - 74), 32, 48, 20, 15)
    present()
dt = time.ticks_diff(time.ticks_us(), t0) / 1000.0
print("    %d frames in %.1f ms -> %.1f fps (draw+wire)" % (n, dt, n / dt * 1000))
print("bench done.")
