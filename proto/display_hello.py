"""
display_hello.py -- super-safe SSD1322 256x64 bring-up, VERIFIED recipe.

Rewritten 2026-10-13 against SSD1322.md + proto2/bringup.py (rdagger-
verified). The previous version of this file was built on wrong premises
(SPI mode 3, params sent with DC=0, column window 0..127, A4 semantics).

Verified facts baked in:
  * SPI MODE 0 (SPI defaults) -- mode 3 passes commands but corrupts
    multi-byte streams
  * command byte with DC=0, its PARAMETER BYTES as DATA (DC=1), one CS
    transaction per command -- params bundled with DC=0 are mis-read
    (that is why unlock/window/remap "did nothing" for 25 rounds)
  * column window in 4-pixel address units, panel wired from segment 112:
    0x15 28 91 for 256 px  (NOT 0x15 00 7F)
  * 0xA4 = ENTIRE DISPLAY OFF on SSD1322 (never send it); normal mode is
    0xA6; 0xA5 = entire display ON (bus test)
  * reset: RES low 50 ms, settle 100 ms
  * framebuffer: 8192 B, framebuf GS4_HMSB (high nibble = even pixel),
    sent as ONE DC=1 burst after 0x5C -- no byte swap

Stages (watch the panel):
  1  A5 -> whole panel lit;  A6 -> dark          (bus/power/interface)
  2  hard reset + full init + RAM blank          (stays dark)
  3  white fill                                  (all lit)
  4  8px checkerboard                            (4bpp packing)
  5  border + text                               (readable = DONE)

Run:  mpremote run proto/display_hello.py
"""

import time

from framebuf import FrameBuffer, GS4_HMSB
from machine import Pin, SPI

SCK, MOSI, CS, DC, RES = 18, 19, 17, 20, 21   # rdagger-verified pinout
INIT_HZ = 1_000_000
W, H = 256, 64
COL_OFF = 28                                # segment 112 -> col addr 28
CONTRAST = 0x9F
STAGE_S = 4                                 # seconds per stage

spi = SPI(0, baudrate=INIT_HZ, firstbit=SPI.MSB,
          sck=Pin(SCK), mosi=Pin(MOSI))     # MODE 0 = defaults
cs = Pin(CS, Pin.OUT, value=1)
dc = Pin(DC, Pin.OUT, value=0)
res = Pin(RES, Pin.OUT, value=1)

fb_buf = bytearray(W * H // 2)              # 8192 B
fb = FrameBuffer(fb_buf, W, H, GS4_HMSB)


def cmd(command, *args):
    dc.value(0)
    cs.value(0)
    spi.write(bytes((command,)))
    cs.value(1)
    if args:                                # params are DATA (DC=1)
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
    cmd(0xFD, 0x12)                         # unlock extended cmds FIRST
    cmd(0xAE)                               # display off
    cmd(0xB3, 0x91)                         # clock
    cmd(0xCA, 0x3F)                         # multiplex 1/64 (NOT 0xA8)
    cmd(0xA2, 0x00)                         # offset
    cmd(0xA1, 0x00)                         # start line
    cmd(0xA0, 0x14, 0x11)                   # remap + dual COM
    cmd(0xB5, 0x00)                         # GPIO off
    cmd(0xAB, 0x01)                         # internal VDD regulator
    cmd(0xB4, 0xA0, 0xFD)                   # enhancement A
    cmd(0xC1, CONTRAST)                     # contrast
    cmd(0xC7, 0x0F)                         # master current
    cmd(0xB9)                               # linear gray table
    cmd(0xB1, 0xE2)                         # phase length
    cmd(0xD1, 0xA2, 0x20)                   # enhancement B (NOT 0xB2)
    cmd(0xBB, 0x1F)                         # pre-charge voltage
    cmd(0xB6, 0x08)                         # 2nd pre-charge
    cmd(0xBE, 0x07)                         # VCOMH
    cmd(0xA6)                               # NORMAL (never 0xA4 = all-off)
    cmd(0xA9)                               # partial off
    cmd(0xAF)                               # display on


def present():
    c0 = COL_OFF
    c1 = c0 + W // 4 - 1                    # 28..91 (4 px per address)
    cmd(0x15, c0, c1)
    cmd(0x75, 0x00, H - 1)
    cmd(0x5C)
    data(fb_buf)                            # one DC=1 burst, no swap


print("SSD1322 hello -- verified recipe (mode 0, params as data, win 28..91)")

print("[1] A5 (all ON) then A6 (normal)")
cmd(0xA5)
time.sleep(STAGE_S)
cmd(0xA6)
time.sleep(1)

print("[2] hard reset + init + RAM blank")
hard_reset()
init()
fb.fill(0)
present()
time.sleep(STAGE_S)

print("[3] white fill")
fb.fill(15)
present()
time.sleep(STAGE_S)

print("[4] 8px checkerboard")
fb.fill(0)
for y in range(H):
    for x in range(0, W, 8):
        if ((x >> 3) + (y >> 3)) & 1:
            fb.fill_rect(x, y, 8, min(8, H - y), 15)
present()
time.sleep(STAGE_S)

print("[5] border + text -- readable = bring-up DONE")
fb.fill(0)
fb.rect(0, 0, W - 1, H - 1, 15)
fb.text("HELLO SSD1322 256x64", 12, 12, 15)
fb.text("mode0 + params-as-data + win 28..91", 12, 28, 15)
fb.text("ABCDEF 0123456789", 12, 44, 15)
present()
print("left on screen.")
