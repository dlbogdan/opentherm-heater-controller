"""Room model: per-room setpoint + actual from the CCU3 (async, non-blocking).

Two sources (config ``room_source``):

* ``"heating_groups"`` (default) -- the ``HmIP-HEATING`` groups whose Homematic
  room also contains a WTH device. One reading per room.
* ``"etrv"`` -- every ``HmIP-eTRV``, averaged per Homematic room.

**Room names come from the CCU3's own Room API** (``Room.listAll`` /
``Room.get``, matched by numeric channel id) -- they are *never* parsed from a
device's name, so the model is independent of how anyone names their groups.
Rooms are keyed by the stable device address.

Discovery is done once and cached to flash (re-done only on a miss or a
``room_source`` change). The per-tick read folds each room **one at a time**
into O(1) scalars and also computes the ReGaHd heating-demand signal. All I/O
is async so it never stalls the board's event loop.

The CCU3 value-key recipe is in ``AGENTS.md`` ("CCU3 / Homematic data
reference"): the setpoint key is ``SET_POINT_TEMPERATURE`` (NOT ``SETPOINT``,
which is always empty).
"""

try:
    import ujson as json
except ImportError:  # CPython host tests
    import json
try:
    import uos as os
except ImportError:  # CPython host tests
    import os
try:
    import uasyncio as asyncio
except ImportError:  # CPython host tests
    import asyncio
try:
    import gc  # keep the heap bounded while streaming device payloads
except ImportError:  # CPython host tests
    gc = None

from ccu3 import Ccu3Rpc, Ccu3Error, _now_ms

SETPOINT_KEY = "SET_POINT_TEMPERATURE"   # room setpoint (deg C)
ACTUAL_KEY = "ACTUAL_TEMPERATURE"        # measured room temperature (deg C)

# Setpoints at/below or at/above these are OFF/ON modes, not real room
# setpoints -- the ReGaHd delta script skips them when computing demand.
SETPOINT_MIN_VALID = 5.0
SETPOINT_MAX_VALID = 30.0


def _is_heating(itype):
    return str(itype).startswith("HmIP-HEATING")


def _is_wth(itype):
    return str(itype).startswith(("HmIP-WTH", "HmIP-STH"))


def _is_etrv(itype):
    return str(itype).startswith("HmIP-eTRV")


class HeatingGroups:
    """Collect per-room setpoints + actuals (async, O(1) RAM)."""

    name = "rooms"

    def __init__(self, config, cache_path="/ccu3_rooms_cache.json",
                 http_post=None, log=None):
        self._config = config
        self._cache_path = cache_path
        self._log = log
        self._mode = (config.get("room_source") or "heating_groups")
        self._rpc = Ccu3Rpc(config.get("ccu3_url"),
                            config.get("ccu3_user"),
                            config.get("ccu3_pass"),
                            http_post=http_post, log=log)
        self._rooms = self._load_cache()  # list of {room_name, addr(s), iface}
        self._last = None  # most recent aggregate (for the sync `rooms` command)

    # -- one pass over all rooms, one at a time ------------------------------
    async def read(self):
        """Full pass; returns a compact O(1) aggregate (None for empty)."""
        if self._rooms is None:
            await self._discover()
        rooms = self._rooms or []

        total = len(rooms)
        reading = 0
        sum_sp = 0.0
        n_sp = 0
        max_sp = None
        sum_act = 0.0
        n_act = 0
        max_act = None
        thermostats = 0
        total_delta = 0.0
        n_delta = 0
        max_delta = None
        max_delta_name = ""
        for room in rooms:
            sp, act = await self._room_values(room)
            if sp is not None:
                sum_sp += sp
                n_sp += 1
                max_sp = sp if max_sp is None or sp > max_sp else max_sp
            if act is not None:
                sum_act += act
                n_act += 1
                max_act = act if max_act is None or act > max_act else max_act
            if sp is not None and act is not None:
                thermostats += 1
                if (SETPOINT_MIN_VALID < sp < SETPOINT_MAX_VALID
                        and sp > act):
                    delta = sp - act
                    total_delta += delta
                    n_delta += 1
                    if max_delta is None or delta > max_delta:
                        max_delta = delta
                        max_delta_name = room.get("room_name", "")
            if sp is not None or act is not None:
                reading += 1

        delta_cap = float(self._config.get("demand_delta_cap") or 3.0)
        demand_pct = None
        if thermostats > 0:
            global_avg_delta = total_delta / thermostats
            demand_pct = (global_avg_delta / delta_cap) * 100.0
            if demand_pct > 100.0:
                demand_pct = 100.0
            if demand_pct < 0.0:
                demand_pct = 0.0

        result = {
            "total": total,
            "reading": reading,
            "avg_setpoint": (sum_sp / n_sp) if n_sp else None,
            "avg_actual": (sum_act / n_act) if n_act else None,
            "max_setpoint": max_sp,
            "max_actual": max_act,
            "thermostats": thermostats,
            "demanding_rooms": n_delta,
            "avg_delta": (total_delta / thermostats) if thermostats else None,
            "max_delta": max_delta,
            "max_delta_room": max_delta_name,
            "demand_pct": demand_pct,
        }
        self._last = result  # keep for the sync `rooms` command
        return result

    def last(self):
        """Most recent aggregate (or None before the first read)."""
        return self._last

    # -- per-room value(s) ---------------------------------------------------
    async def _room_values(self, room):
        """(setpoint, actual) for one room; None for any absent value."""
        if "addr" in room:  # heating_groups: single group
            sp = await self._value(room["iface"], room["addr"], SETPOINT_KEY)
            act = await self._value(room["iface"], room["addr"], ACTUAL_KEY)
            return sp, act
        sps = []
        acts = []  # etrv: average the room's valves (bounded, per-room only)
        for addr in room.get("addrs", []):
            sp = await self._value(room["iface"], addr, SETPOINT_KEY)
            act = await self._value(room["iface"], addr, ACTUAL_KEY)
            if sp is not None:
                sps.append(sp)
            if act is not None:
                acts.append(act)
        sp = (sum(sps) / len(sps)) if sps else None
        act = (sum(acts) / len(acts)) if acts else None
        return sp, act

    async def _value(self, iface, addr, key):
        """One value key -> float, or None if absent/errored."""
        try:
            result = await self._rpc.call(
                "Interface.getValue",
                {"interface": iface, "address": addr + ":1", "valueKey": key})
        except Ccu3Error:
            return None
        if isinstance(result, dict):
            result = result.get("value")
        if result is None:
            return None
        try:
            return float(result)
        except (TypeError, ValueError):
            return None

    # -- discovery (once; cached to flash) -----------------------------------
    async def _discover(self):
        last_error = None
        for attempt in range(3):
            try:
                self._rooms = await self._build_rooms()
                self._save_cache(self._mode, self._rooms)
                if self._log:
                    self._log("Rooms: %d active rooms (%s)"
                              % (len(self._rooms), self._mode))
                return
            except Exception as exc:
                last_error = exc
                if self._log:
                    self._log("Rooms: discovery attempt failed: %s" % exc)
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2 ** attempt))
        self._rooms = None
        if self._log:
            self._log("Rooms: discovery failed: %s" % last_error)

    async def _build_rooms(self):
        """Stream devices one at a time; hold only the small results.

        Holding every ``Device.get`` payload at once OOMs the Pico (a full
        payload is ~10-50 KB and there are ~50 of them). So we fold each
        device into the (small) results and drop the payload immediately.
        """
        rooms = await self._all_rooms()  # room_id -> {"name", "chanIds": set}
        chan2room = {}
        for room_id, info in rooms.items():
            for cid in info["chanIds"]:
                chan2room[str(cid)] = room_id

        def room_of(device):
            for c in (device.get("channels") or []):
                if isinstance(c, dict) and c.get("id") is not None:
                    cid = str(c["id"])
                    if cid in chan2room:
                        return chan2room[cid]
            return None

        wth_rooms = set()          # rooms that contain a WTH (filter)
        heating = []               # (room_id, addr, iface) -- a handful
        etrv_by_room = {}          # room_id -> {"addrs": [...], "iface"}
        ids = await self._rpc.call("Device.listAll", {}) or []
        seen = 0
        for device_id in ids:
            try:
                device = await self._rpc.call("Device.get", {"id": device_id})
            except Ccu3Error:
                continue  # skip CCU system/virtual devices that error
            if not isinstance(device, dict):
                continue
            dtype = str(device.get("type", ""))
            room_id = room_of(device)
            addr = device.get("address")
            iface = device.get("interface")
            del device             # drop the (large) payload before next RPC
            if _is_wth(dtype) and room_id is not None:
                wth_rooms.add(room_id)
            if _is_heating(dtype) and room_id is not None:
                heating.append((room_id, addr, iface))
            if _is_etrv(dtype) and room_id is not None:
                entry = etrv_by_room.setdefault(
                    room_id, {"addrs": [], "iface": iface})
                entry["addrs"].append(addr)
            seen += 1
            if gc is not None and seen % 10 == 0:
                gc.collect()       # keep the Pico heap bounded

        active = []
        if self._mode == "heating_groups":
            for room_id, addr, iface in heating:
                if room_id in wth_rooms:  # no WTH in the room -> excluded
                    active.append({"room_name": rooms[room_id]["name"],
                                   "addr": addr, "iface": iface})
        else:  # etrv
            for room_id, entry in etrv_by_room.items():
                active.append({"room_name": rooms[room_id]["name"],
                               "addrs": entry["addrs"],
                               "iface": entry["iface"]})
        return active

    async def _all_rooms(self):
        out = {}
        ids = await self._rpc.call("Room.listAll", {}) or []
        for room_id in ids:
            try:
                room = await self._rpc.call("Room.get", {"id": room_id})
            except Ccu3Error:
                continue
            if not isinstance(room, dict):
                continue
            chs = room.get("channelIds") or []
            out[room_id] = {"name": room.get("name", ""),
                            "chanIds": set(chs) if isinstance(chs, list) else set()}
        return out

    # -- flash cache (discovered once, read at boot) --------------------------
    def _load_cache(self):
        try:
            with open(self._cache_path, "r") as handle:
                data = json.load(handle)
            if (isinstance(data, dict) and data.get("mode") == self._mode
                    and isinstance(data.get("rooms"), list)
                    and data["rooms"]
                    and all(isinstance(r, dict) and r.get("room_name")
                            for r in data["rooms"])):
                return data["rooms"]
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def _save_cache(self, mode, rooms):
        try:
            temporary = self._cache_path + ".new"
            with open(temporary, "w") as handle:
                json.dump({"mode": mode, "rooms": rooms,
                           "discovered_at": _now_ms()}, handle)
                handle.flush()
            try:
                os.remove(self._cache_path)
            except OSError:
                pass
            os.rename(temporary, self._cache_path)
        except Exception:
            if self._log:
                self._log("Rooms: could not write cache")
