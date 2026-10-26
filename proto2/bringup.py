"""SSD1322 clone bring-up ladder for Raspberry Pi Pico 1 (RP2040, MicroPython).

Wiring (GP numbers -- rdagger demo pinout):
    SCK  GP18  (SPI0 SCK)
    MOSI GP19  (SPI0 TX)
    CS   GP17
    DC   GP20
    RES  GP21

Run from the host (self-contained, no imports to upload):
    mpremote run proto2/bringup.py

Each step prints what the panel SHOULD show. Watch the panel AND the
serial output -- the step where reality diverges says which knob to
turn (see README.md in this directory).

Key facts baked in (init AND write path from rdagger/micropython-ssd1322 --
that demo is verified working on this exact module; the attached bring-up
guide is wrong on several points, see README.md):
  - SPI MODE 0 (rdagger's working demo uses SPI defaults -- the guide's
    mode-3 claim is wrong for this module)
  - command byte with DC=0, its PARAMETER BYTES SENT AS DATA (DC=1),
    one CS transaction per command -- rdagger's write_cmd shape
  - slow init clock, faster clock only for framebuffer writes
  - 0xFD 0x12 unlock before any extended command
  - multiplex is 0xCA (0xA8 is SSD1306); enhancement B is 0xD1 (not 0xB2)
  - 0xA4 on SSD1322 = ENTIRE DISPLAY OFF, NOT "resume from RAM"
  - 4-grayscale column addressing: 1 address = 4 pixels, and the panel
    starts at column 28 (segment 112) -- see COL_OFFSET
  - RES low 50 ms, 100 ms settle before init (rdagger's timing)
"""

import time
from machine import Pin, SPI

# ---------------------------------------------------------------------------
# Config -- edit these first
# ---------------------------------------------------------------------------
SCK_PIN  = 18
MOSI_PIN = 19
CS_PIN   = 17
DC_PIN   = 20
RES_PIN  = 21

WIDTH  = 256      # set to 128 if the right half shows garbage
HEIGHT = 64

USE_BITBANG = False    # True: software SPI -- rules out HW SPI mode/phase issues
INIT_HZ   = 1_000_000  # clones: keep init slow (<= 2-4 MHz)
FRAME_HZ  = 10_000_000  # keep frames SLOW until probes are clean, then try 8M

# Horizontal column offset: these 256x64 modules wire the panel to segment
# 112 -> column address 28 (rdagger driver: "pixels start from segment 112").
# In 4-grayscale mode ONE column address = 4 pixels (2 RAM bytes).
COL_OFFSET = 28

DUTY = 1000            # ms each visual test stays on screen


class SSD1322:
    """Minimal 4-wire SPI SSD1322 driver (write-only bus, 4bpp grayscale)."""

    def __init__(self, width, height, bitbang=False):
        self.width = width
        self.height = height
        # 4bpp packed: 2 pixels/byte, HIGH nibble = lower (even) column
        self.fb = bytearray(width * height // 2)

        self.cs  = Pin(CS_PIN, Pin.OUT, value=1)
        self.dc  = Pin(DC_PIN, Pin.OUT, value=0)
        self.res = Pin(RES_PIN, Pin.OUT, value=1)

        self.spi = None
        if bitbang:
            self.sck  = Pin(SCK_PIN, Pin.OUT, value=0)  # mode 0: clock idles LOW
            self.mosi = Pin(MOSI_PIN, Pin.OUT, value=0)
        else:
            self.sck = None
            self.mosi = None
            # rdagger's WORKING demo uses SPI defaults -- MODE 0, not mode 3.
            # (The bring-up guide's mode-3 claim is wrong for this module.)
            self.spi = SPI(0, baudrate=INIT_HZ, firstbit=SPI.MSB,
                           sck=Pin(SCK_PIN), mosi=Pin(MOSI_PIN))

    # -- low level (write path copied from rdagger's proven driver) ---------
    def _write(self, buf):
        if self.spi is not None:
            self.spi.write(buf)
            return
        # bit-bang SPI mode 0: data set while clock low, slave samples on rising
        for byte in buf:
            for mask in (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01):
                self.mosi.value(1 if byte & mask else 0)
                self.sck.value(1)   # rising edge: slave samples the bit
                self.sck.value(0)   # back to idle-low

    def cmd(self, command, *args):
        # rdagger's write_cmd: command byte with DC=0, then its PARAMETER
        # BYTES AS DATA (DC=1) in a separate transaction. This module needs
        # that split -- sending params with DC=0 corrupts them.
        self.dc.value(0)
        self.cs.value(0)
        self._write(bytes((command,)))
        self.cs.value(1)
        if args:
            self.data(bytes(args))

    def data(self, buf):
        self.dc.value(1)
        self.cs.value(0)
        self._write(buf)
        self.cs.value(1)

    # -- control -------------------------------------------------------------
    def hard_reset(self):
        # rdagger's proven timing: 50 ms low, 100 ms settle
        self.res.value(0)
        time.sleep_ms(50)
        self.res.value(1)
        time.sleep_ms(100)

    def init(self):
        """Init sequence from rdagger/micropython-ssd1322 (verified on a real
        256x64 SSD1322 module + Pico). The attached bring-up guide mixed in
        SSD1306 commands (0xA8 multiplex, 0xA4 'resume from RAM') -- on the
        SSD1322 0xA4 is ENTIRE DISPLAY OFF and multiplex is 0xCA."""
        self.cmd(0xFD, 0x12)          # unlock extended commands
        self.cmd(0xAE)                # display off (sleep on)
        self.cmd(0xB3, 0x91)          # front clock divider / osc freq
        self.cmd(0xCA, 0x3F)          # multiplex ratio 1/64 (NOT 0xA8!)
        self.cmd(0xA2, 0x00)          # display offset
        self.cmd(0xA1, 0x00)          # display start line
        self.cmd(0xA0, 0x14, 0x11)    # remap + dual COM line mode
        self.cmd(0xB5, 0x00)          # GPIO: disable inputs
        self.cmd(0xAB, 0x01)          # internal VDD regulator ON
        self.cmd(0xB4, 0xA0, 0xFD)    # enhancement A: ext VSL, enhanced gray
        self.cmd(0xC1, 0x9F)          # contrast current
        self.cmd(0xC7, 0x0F)          # master current control
        self.cmd(0xB9)                # default linear gray table
        self.cmd(0xB1, 0xE2)          # phase length: 1st=5, 2nd=14 clocks
        self.cmd(0xD1, 0xA2, 0x20)    # enhancement B (NOT 0xB2!)
        self.cmd(0xBB, 0x1F)          # pre-charge voltage 0.60*VCC
        self.cmd(0xB6, 0x08)          # 2nd pre-charge period
        self.cmd(0xBE, 0x07)          # VCOMH 0.86*VCC (0xBB/0xBE swapped
                                      #   vs the guide -- rdagger is truth)
        self.cmd(0xA6)                # normal display (NOT 0xA4 = all-off!)
        self.cmd(0xA9)                # partial display disable
        self.cmd(0xAF)                # display on (sleep off)
        if self.spi is not None:
            self.spi.init(baudrate=FRAME_HZ)  # raise clock for frames only

    def contrast(self, v):
        self.cmd(0xC1, v & 0xFF)
        self.cmd(0xC7, 0x0F)

    # -- framebuffer -----------------------------------------------------------
    def fill(self, v):
        v &= 0x0F
        # bytes-pattern multiply: ONE allocation, no intermediate list
        # (list mult like [x]*8192 costs 32KB of pointers and OOMs the Pico)
        self.fb[:] = bytes([(v << 4) | v]) * len(self.fb)

    def pixel(self, x, y, v):
        if 0 <= x < self.width and 0 <= y < self.height:
            i = (y * self.width + x) >> 1
            if x & 1:   # odd column -> LOW nibble
                self.fb[i] = (self.fb[i] & 0xF0) | (v & 0x0F)
            else:       # even column -> HIGH nibble (SSD1322 4-grayscale packing)
                self.fb[i] = (self.fb[i] & 0x0F) | ((v & 0x0F) << 4)

    def show(self):
        # rdagger's proven present(): separate transactions, params as data.
        # 4-grayscale addressing: ONE column address = 4 pixels (2 RAM bytes),
        # and these modules start the panel at column COL_OFFSET (segment 112).
        c0 = COL_OFFSET
        c1 = c0 + self.width // 4 - 1
        self.cmd(0x15, c0, c1)
        self.cmd(0x75, 0x00, self.height - 1)
        self.cmd(0x5C)
        self.data(self.fb)


# ---------------------------------------------------------------------------
# Debug ladder -- each step isolates one failure class
# ---------------------------------------------------------------------------
def t1_bus_testmode(d):
    print("[T1] Entire-panel ON (0xA5) -- test mode, no RAM, no addressing.")
    print("     Panel should go FULLY lit. Nothing lit = bus/power/interface,")
    print("     NOT addressing. Try USE_BITBANG=True next.")
    d.cmd(0xA5)
    time.sleep_ms(DUTY)
    print("[T2] Back to normal (0xA6) -- should visibly go dark and STAY in")
    print("     RAM mode. Never 0xA4 here: on SSD1322 that is ENTIRE DISPLAY")
    print("     OFF and silently kills every later write.")
    d.cmd(0xA6)
    time.sleep_ms(1000)


def t3_blank(d):
    print("[T3] RAM blank (all 0x00) -- panel should be fully dark.")
    d.fill(0)
    d.show()
    time.sleep_ms(1000)


def block_probe(d, x0, y0):
    """One 16x16 white block at (x0,y0), everything else black."""
    d.fill(0)
    for y in range(y0, min(y0 + 16, d.height)):
        for x in range(x0, min(x0 + 16, d.width)):
            d.pixel(x, y, 15)
    d.show()
    time.sleep_ms(DUTY)


def t4_reach_probes(d):
    print("[T4] 16x16 white block at x=0 (top-left corner).")
    print("     No block at all -> RAM data path dead (not an addressing issue).")
    block_probe(d, 0, 0)
    print("[T5] same block at x=64.  Missing -> panel columns stop before 64.")
    block_probe(d, 64, 0)
    print("[T6] same block at x=128. Missing -> 128-wide module: set WIDTH=128.")
    block_probe(d, 128, 0)
    print("[T7] same block at x=192. Missing -> panel narrower than 256.")
    block_probe(d, 192, 0)
    print("[T8] same block at y=48 (left edge, near bottom). Row reach.")
    block_probe(d, 0, 48)
    print("[T9] block at x=0 AGAIN -- identical to T4? If not, writes after")
    print("     the first one are not landing (controller state lost).")
    block_probe(d, 0, 0)


def t10_checker(d):
    print("[T10] 8px checkerboard -- 4bpp packing (nibble order) at full size.")
    d.fill(0)
    for y in range(d.height):
        for x in range(d.width):
            if ((x // 8) + (y // 8)) & 1:
                d.pixel(x, y, 15)
    d.show()
    time.sleep_ms(DUTY)


def t11_gradient(d):
    print("[T11] Vertical gradient, 16 gray bands -- gray table + row ramp.")
    d.fill(0)
    for y in range(d.height):
        v = y * 16 // d.height
        for x in range(d.width):
            d.pixel(x, y, v)
    d.show()
    time.sleep_ms(DUTY)


def t12_contrast(d):
    print("[T12] Contrast sweep 0x00..0xFF on the static gradient.")
    print("      Brightness should rise smoothly, then plateau.")
    for v in range(0x00, 0x100, 0x11):
        d.contrast(v)
        time.sleep_ms(300)
    d.contrast(0x9F)
    print("      Parked at 0x9F.")


def main():
    print("SSD1322 clone bring-up: %dx%d, %s, SPI mode 0, %d Hz"
          % (WIDTH, HEIGHT,
             "bit-bang" if USE_BITBANG else "HW SPI0 @ GP%d/GP%d" % (SCK_PIN, MOSI_PIN),
             FRAME_HZ))
    d = SSD1322(WIDTH, HEIGHT, bitbang=USE_BITBANG)
    d.hard_reset()
    d.init()
    print("Init sent (0xAB SPI select, 0xFD 0x12 unlock, 0xAF ON).")

    t1_bus_testmode(d)
    t3_blank(d)
    t4_reach_probes(d)
    t10_checker(d)
    t11_gradient(d)
    t12_contrast(d)

    print("Bring-up ladder done -- panel left on the vertical gradient.")
    print("If a step failed, see the troubleshooting map in README.md.")


main()
