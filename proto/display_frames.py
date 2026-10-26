"""
display_frames.py -- SSD1322 animation demo, VERIFIED recipe (2026-10-13).

512x128-era experiments are dead: the module is 256x64 @4bpp, 8192 B fb,
window 0x15 28 91 / 0x75 0 63, SPI mode 0, params as data (DC=1).
Counter must increment IN PLACE, clean, at FRAME_HZ.

Run:  mpremote run proto/display_frames.py
"""

import time

from framebuf import FrameBuffer, GS4_HMSB
from machine import Pin, SPI

SCK, MOSI, CS, DC, RES = 18, 19, 17, 20, 21   # rdagger-verified pinout
INIT_HZ = 1_000_000
FRAME_HZ = 10_000_000
W, H = 256, 64
COL_OFF = 28
CONTRAST = 0x9F
FRAMES = 20

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


print("SSD1322 animation (verified recipe) -- counter must march IN PLACE")
hard_reset()
init()
spi.init(baudrate=FRAME_HZ)

us = []
for n in range(FRAMES):
    fb.fill(0)
    fb.rect(0, 0, W - 1, H - 1, 15)
    fb.text("FRAME %02d  256x64 verified path" % n, 10, 10, 15)
    x = 10 + (n * 11) % (W - 74)
    fb.fill_rect(x, 34, 48, 18, 15)
    t0 = time.ticks_us()
    present()
    us.append(time.ticks_diff(time.ticks_us(), t0))
    time.sleep_ms(200)

avg = sum(us) / len(us)
print("avg wire=%.2fms -> max fps ~= %.1f" % (avg / 1000, 1e6 / avg))
