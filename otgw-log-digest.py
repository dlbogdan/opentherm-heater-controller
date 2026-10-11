#!/usr/bin/env python3
"""otgw-log-digest: compress a 24 h otgw-stream-capture log into a review-sized digest.

The raw capture is ~2 lines/s (~7 MB/day -- far beyond any context window)
and 99 % of it is the gateway's repetitive polling cycle. This tool keeps
only what matters for OTGW driver validation (Bug 4 Phase B):

  * line-type histogram (what the gateway actually emits);
  * EVERY command / ack / error line with timestamps (CS=, CH=, CS:, CH:,
    NG/SE/... -- these are rare and are the whole point of the capture);
  * CS= -> ack latency stats (the Phase B step-2 measurement);
  * TRANSITIONS only for the chatty signals: the wire control setpoint
    (MsgID 1 write_data), MsgID 0 status flags, gateway event lines and
    the PR: status banner (change-only -- the firmware re-dumps them
    every ~10 min); a plateau of identical acks (the 30 s re-assert
    rhythm) collapses to ONE line with count + cadence; connect/reconnect
    markers and commands always print; ESP status chatter (SC:/SR:/PS:/PR:)
    collapses to one histogram bucket each;
  * telemetry min/max/last per folded data id.

Output is a few hundred lines regardless of capture length -- safe to
paste into a chat, an issue, or a commit message.

Usage:
    python3 otgw-log-digest.py otgw-capture.log [--raw-grep REGEX]

--raw-grep dumps (capped) raw lines matching REGEX for a closer look.
Pure stdlib; runs on any Python 3. Frame layout matches
app/transport/opentherm.py (3-bit type + 8-bit id + 16-bit value, odd
parity) -- inlined on purpose so this stays a standalone host tool.
"""
import re
import sys
from collections import Counter
from datetime import datetime

MSG_TYPES = ("read_data", "write_data", "invalid", "reserved",
             "read_ack", "write_ack", "data_invalid", "unknown_data")
F88_IDS = (1, 14, 16, 17, 18, 24, 25, 26, 27, 28, 56, 57)
NAMES = {0: "status", 1: "control_setpoint", 5: "fault_flags",
         14: "max_modulation", 17: "modulation", 18: "water_pressure",
         25: "flow_temp", 26: "dhw_temp", 27: "outside_temp",
         28: "return_temp", 56: "dhw_setpoint", 57: "max_boiler_setpoint"}
ERRORS = ("NG", "SE", "BV", "OR", "NS", "NF", "OE")
TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d\d\d)  (.*)$")
TS_FMT = "%Y-%m-%d %H:%M:%S.%f"
FRAME_RE = re.compile(r"^([TRBA])([0-9A-Fa-f]{8})$")
CMD_RE = re.compile(r"^([A-Z]{2})=(.*)$")
ACK_RE = re.compile(r"^([A-Z]{2}): (.*)$")
STATUS_RE = re.compile(r"^[A-Z][A-Z]: ")
CAP = 200          # max lines per dumped section


def decode(frame):
    mtype = (frame >> 28) & 0x7
    did = (frame >> 16) & 0xFF
    raw = frame & 0xFFFF
    out = {"type": MSG_TYPES[mtype], "id": did, "raw": raw}
    if did in F88_IDS:
        out["value"] = (raw - 0x10000 if raw & 0x8000 else raw) / 256.0
    else:
        out["hb"] = (raw >> 8) & 0xFF
        out["lb"] = raw & 0xFF
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    path = sys.argv[1]
    raw_grep = None
    if "--raw-grep" in sys.argv:
        raw_grep = re.compile(sys.argv[sys.argv.index("--raw-grep") + 1])

    hist = Counter()
    events = []           # (ts, text) -- transitions, commands, markers
    last_sig = {}         # signal key -> last text (emit only on CHANGE)
    telemetry = {}        # id -> [min, max, last, count]
    latencies = []        # ms
    pending = {}          # cmd -> ts of the command line
    ack_run = [None, []]  # [text, [ts...]] -- identical acks collapse
    first_ts = last_ts = None
    total = frames = 0

    def signal(ts, key, text):
        """Record a transition; chatty signals emit only on change."""
        if last_sig.get(key) == text:
            return
        last_sig[key] = text
        events.append((ts, text))

    def flush_ack_run():
        """Collapse a plateau of identical acks (the 30 s re-assert rhythm
        would otherwise blow the event cap): one line with count + cadence."""
        text, stamps = ack_run
        if text is None:
            return
        if len(stamps) > 2:
            d0 = datetime.strptime(stamps[0], TS_FMT)
            d1 = datetime.strptime(stamps[-1], TS_FMT)
            step = (d1 - d0).total_seconds() / (len(stamps) - 1)
            events.append((stamps[0], "%s  x%d every ~%.0fs (until %s)"
                           % (text, len(stamps), step, stamps[-1][11:16])))
        else:
            for t in stamps:
                events.append((t, text))
        ack_run[0], ack_run[1] = None, []

    with open(path, errors="replace") as f:
        for line in f:
            total += 1
            m = TS_RE.match(line.rstrip("\n"))
            if not m:
                continue
            ts, body = m.groups()
            first_ts = first_ts or ts
            last_ts = ts
            if raw_grep and raw_grep.search(body):
                events.append((ts, "GREP " + body))
            fm = FRAME_RE.match(body)
            if fm:
                frames += 1
                direction, hexs = fm.groups()
                d = decode(int(hexs, 16))
                key = "%s id=%-2d %-12s %s" % (direction, d["id"],
                                               NAMES.get(d["id"], "?"),
                                               d["type"])
                hist[key] += 1
                if d["id"] == 1 and d["type"] == "write_data" \
                        and direction in "TR":
                    signal(ts, "cs_wire",
                           "CS wire (%s) -> %.1f" % (direction, d["value"]))
                elif d["id"] == 0:
                    signal(ts, "status %s %s" % (direction, d["type"]),
                           "status %s %s -> hb=0x%02X lb=0x%02X"
                           % (direction, d["type"], d["hb"], d["lb"]))
                # telemetry: boiler ANSWERS only (request lines carry a
                # dummy 0 value; data_invalid is the boiler saying "no")
                if direction == "B" and d["type"] in ("read_ack",
                                                      "write_ack") \
                        and "value" in d:
                    t = telemetry.setdefault(d["id"], [d["value"], d["value"],
                                                       d["value"], 0])
                    t[0] = min(t[0], d["value"])
                    t[1] = max(t[1], d["value"])
                    t[2] = d["value"]
                    t[3] += 1
                continue
            cm = CMD_RE.match(body)
            if cm:
                cmd = cm.group(1)
                hist["command %s=" % cmd] += 1
                pending[cmd] = ts
                events.append((ts, ">>> " + body))
                continue
            am = ACK_RE.match(body)
            if am and am.group(1) in ("CS", "CH"):
                cmd, value = am.groups()
                hist["ack %s:" % cmd] += 1
                text = "<<< %s: %s" % (cmd, value)
                if ack_run[0] == text:
                    ack_run[1].append(ts)
                else:
                    flush_ack_run()
                    ack_run[0], ack_run[1] = text, [ts]
                if cmd in pending:
                    t0 = datetime.strptime(pending[cmd], TS_FMT)
                    t1 = datetime.strptime(ts, TS_FMT)
                    latencies.append(((t1 - t0).total_seconds() * 1000.0))
                continue
            if body in ERRORS:
                hist["error " + body] += 1
                events.append((ts, "!!! " + body))
                continue
            if body.startswith("###"):
                hist["marker"] += 1
                events.append((ts, body))
                continue
            if body.startswith("PR: A="):
                hist["banner"] += 1
                # NOT a reboot marker: the rvdbreemen firmware re-dumps its
                # PR: status block periodically. Emit only on CHANGE (a real
                # version change); reconnects are the ### markers' job.
                signal(ts, "banner", "banner " + body)
                continue
            if "Thermostat" in body or "power" in body.lower():
                hist["event line"] += 1
                signal(ts, "evt:" + body, "evt " + body)
                continue
            sm = STATUS_RE.match(body)
            # collapse the ESP's periodic status chatter (SC:/SR:/PS:/PR: ...)
            # into ONE bucket each -- per-minute SC: lines otherwise spawn
            # a thousand one-hit histogram entries
            hist["other: " + (sm.group(0)[:3] if sm
                               else body.split("=")[0][:16])] += 1

    flush_ack_run()
    events.sort(key=lambda e: e[0])   # run summaries land at run START

    print("== otgw-log-digest %s ==" % path)
    print("lines %d  frames %d  span %s .. %s" % (total, frames, first_ts,
                                                  last_ts))
    print("\n-- line types --")
    for key, n in hist.most_common():
        print("%8d  %s" % (n, key))
    if latencies:
        lat = sorted(latencies)
        print("\n-- command ack latency (ms) --")
        print("n=%d  min=%.0f  median=%.0f  p95=%.0f  max=%.0f"
              % (len(lat), lat[0], lat[len(lat) // 2],
                 lat[int(len(lat) * 0.95)], lat[-1]))
    if telemetry:
        print("\n-- telemetry (f88 ids seen: min / max / last / count) --")
        for did in sorted(telemetry):
            t = telemetry[did]
            print("  id=%-2d %-18s %8.2f / %8.2f / %8.2f  x%d"
                  % (did, NAMES.get(did, "?"), t[0], t[1], t[2], t[3]))
    print("\n-- events / transitions (%d, cap %d) --" % (len(events), CAP))
    for ts, text in events[:CAP]:
        print("%s  %s" % (ts, text))
    if len(events) > CAP:
        print("... %d more (raise CAP or use --raw-grep)" % (len(events) - CAP))
    return 0


if __name__ == "__main__":
    sys.exit(main())
