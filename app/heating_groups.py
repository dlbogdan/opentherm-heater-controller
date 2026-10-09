"""Room model: per-room setpoint + actual from the CCU3 (async, non-blocking).

Three sources (config ``room_source``):

* ``"heating_groups"`` (default) -- the ``HmIP-HEATING`` groups whose Homematic
  room also contains a WTH device. One reading per room.
* ``"etrv"`` -- every ``HmIP-eTRV``, averaged per Homematic room.
* ``"heating_groups+etrvs"`` (P6a) -- the union: a room uses its HEATING group
  if it has one, otherwise that room's eTRV average. A room with both counts
  once (the group wins). This covers installs that mix grouped rooms with
  standalone radiator valves.

**Parity with the HA provenance script** (``heating-demand-calculation.ccu3script``):
that script iterates *physical* WTH/STHD/STH devices and reads each transceiver
channel; the firmware reads the HEATING-group aggregate (WTH-presence filtered).
They agree only while each room has exactly one WTH-type thermostat and one
group (true in this house). The union mode + per-room weights are a deliberate
divergence from the script; the default weight of 1.0 keeps parity.

**Per-room weights + liveness registry (P6a)** (``/room-weights-config.json``,
separate from the disposable ``/ccu3_rooms_cache.json``): keyed by the stable
CCU ``room_id`` (survives device replacement and renames), each entry holds
``{name, kind, weight [0..1], sensors_alive, last_seen_ms}``. Discovery
reconciles it -- new rooms are added (weight 1.0), known rooms keep their human
weight, and vanished rooms are flagged ``sensors_alive=false`` but NEVER
auto-deleted (a dead battery must not erase the tuning; the user prunes by hand
with ``rooms forget``). A ``room_source`` change re-runs discovery and flags the
rooms the new source excludes the same way -- their weights survive a switch
back. Demand is a weighted mean over the rooms present this pass (weight 1
everywhere == the old behaviour; weight 0 mutes a room). A transient read
failure skips a room for one tick without touching its flag.

**Room names come from the CCU3's own Room API** (``Room.listAll`` /
``Room.get``, matched by numeric channel id) -- they are *never* parsed from a
device's name, so the model is independent of how anyone names their groups.
Rooms are keyed by the stable device address.

Discovery is done once and cached to flash (re-done on a miss, a
``room_source`` change, or after ``rooms_rediscovery_s`` to refresh liveness).
The per-tick read folds each room **one at a time** into O(1) scalars and also
computes the ReGaHd heating-demand signal. All I/O is async so it never stalls
the board's event loop.

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

from ccu3 import Ccu3Rpc, Ccu3Error, _now_ms, _diff_ms

SETPOINT_KEY = "SET_POINT_TEMPERATURE"   # room setpoint (deg C)
ACTUAL_KEY = "ACTUAL_TEMPERATURE"        # measured room temperature (deg C)

# Setpoints at/below or at/above these are OFF/ON modes, not real room
# setpoints -- the ReGaHd delta script skips them when computing demand.
SETPOINT_MIN_VALID = 5.0
SETPOINT_MAX_VALID = 30.0

# Retry backoff for a failed discovery (fixed pacing, independent of the
# data-freshness window ``sensor_cache_s``): after a failed full scan,
# retry at most this often instead of re-scanning on every poll (PLAN.md P2).
DISCOVERY_BACKOFF_S = 600


def _is_heating(itype):
    return str(itype).startswith("HmIP-HEATING")


def _is_wth(itype):
    return str(itype).startswith(("HmIP-WTH", "HmIP-STH"))


def _is_etrv(itype):
    return str(itype).startswith("HmIP-eTRV")


class HeatingGroups:
    """Collect per-room setpoints + actuals (async, O(1) RAM).

    Implements the ``RoomsSource`` interface (``app/rooms.py``): ``enabled``
    is True, ``poll_s`` is the refresh interval, ``read()`` does one pass, and
    ``last()`` returns the cached aggregate. Selected via
    ``rooms.make_rooms_source`` (not referenced by name in ``main.py``).
    """

    name = "rooms"
    enabled = True

    def __init__(self, config, cache_path="/ccu3_rooms_cache.json",
                 registry_path="/room-weights-config.json",
                 http_post=None, log=None, warn=None):
        self._config = config
        self._cache_path = cache_path
        self._registry_path = registry_path
        self._log = log            # INFO -> console only
        self._warn = warn if warn is not None else log  # WARN/ERROR -> flash
        self._mode = (config.get("room_source") or "heating_groups")
        self.poll_s = int(config.get("rooms_poll_s") or 300)
        # How often to re-run the full discovery scan even when the cache is
        # valid, so a dead/added device surfaces (liveness + membership).
        try:
            self._rediscovery_s = int(config.get("rooms_rediscovery_s") or 3600)
        except (KeyError, TypeError, ValueError):
            self._rediscovery_s = 3600
        self._rpc = Ccu3Rpc(config.get("ccu3_url"),
                            config.get("ccu3_user"),
                            config.get("ccu3_pass"),
                            http_post=http_post, log=log, warn=warn)
        self._rooms = self._load_cache()  # active rooms (room_id-keyed)
        # room_id -> {name, kind, weight, sensors_alive, last_seen_ms}
        self._registry = self._load_registry()
        self._discovery_failed_ms = None  # retry-backoff marker (see _discover)
        # Uptime stamp of the last discovery (or cache load at boot). Drives
        # the periodic re-discovery; never compared across reboots.
        self._discovered_ms = _now_ms() if self._rooms is not None else None
        self._last = None  # most recent aggregate (for the sync `rooms` command)

    # -- one pass over all rooms, one at a time ------------------------------
    def _discovery_in_backoff(self):
        """True while inside the retry backoff after a failed discovery.

        An unset marker means "never failed" (or a reboot / a successful
        discovery cleared it) -> attempt normally.
        """
        if self._discovery_failed_ms is None:
            return False
        return (_diff_ms(_now_ms(), self._discovery_failed_ms)
                < DISCOVERY_BACKOFF_S * 1000)

    async def read(self):
        """Full pass; returns a compact O(1) aggregate (None for empty)."""
        if self._rooms is None:
            if not self._discovery_in_backoff():
                await self._discover()
            # Backoff after a failed discovery (PLAN.md P2): skip the
            # ~50-device scan and do an empty pass -- demand_pct stays
            # None (the defined "no sensor" input) until the next attempt.
        elif (self._rediscovery_s > 0
              and _diff_ms(_now_ms(), self._discovered_ms or 0)
              >= self._rediscovery_s * 1000
              and not self._discovery_in_backoff()):
            # P6a: the cache is valid but stale -- re-run discovery so a
            # dead/added device surfaces (refreshes membership + liveness).
            # If the refresh FAILS, keep the previous room list (the cache
            # was good; only the scan failed) instead of dropping to None.
            previous = self._rooms
            await self._discover()
            if self._rooms is None:
                self._rooms = previous
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
        wsum = 0.0    # P6a: sum of weights over rooms with both values present
        wdelta = 0.0  # P6a: weight-scaled sum of demanding deltas
        room_data = {}  # room_id -> {"sp", "act"} for the shell room cards
        for room in rooms:
            sp, act = await self._room_values(room)
            if room.get("room_id") is not None:
                room_data[str(room["room_id"])] = {"sp": sp, "act": act}
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
                w = self._weight_for(room.get("room_id"))
                wsum += w
                if (SETPOINT_MIN_VALID < sp < SETPOINT_MAX_VALID
                        and sp > act):
                    delta = sp - act
                    total_delta += delta
                    wdelta += w * delta
                    n_delta += 1
                    if max_delta is None or delta > max_delta:
                        max_delta = delta
                        max_delta_name = room.get("room_name", "")
            if sp is not None or act is not None:
                reading += 1

        delta_cap = float(self._config.get("demand_delta_cap") or 3.0)
        demand_pct = None
        # P6a: weighted mean over the rooms present this pass. wsum == the
        # unweighted `thermostats` when every weight is 1.0 (parity); a dead
        # room is absent from `rooms` entirely, and weight 0 mutes a room.
        if wsum > 0:
            global_avg_delta = wdelta / wsum
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
            "weighted_thermostats": wsum,
            "demanding_rooms": n_delta,
            "avg_delta": (total_delta / thermostats) if thermostats else None,
            "max_delta": max_delta,
            "max_delta_room": max_delta_name,
            "demand_pct": demand_pct,
            # Per-room readings for the shell room cards. O(rooms) but tiny
            # (~a dozen 2-float entries); the O(1) rule that matters is
            # never holding the 10-50 KB CCU device payloads (AGENTS.md).
            "room_data": room_data,
            # When this aggregate was produced (ticks_ms at pass completion):
            # lets consumers (shell, device tests) check freshness against
            # rooms_poll_s instead of trusting the cache blindly.
            "ts": _now_ms(),
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
                self._discovery_failed_ms = None  # success clears backoff
                self._discovered_ms = _now_ms()
                self._reconcile_registry(self._rooms)  # P6a liveness + weights
                if self._log:
                    self._log("Rooms: %d active rooms (%s)"
                              % (len(self._rooms), self._mode))
                return
            except Exception as exc:
                last_error = exc
                if self._warn:
                    self._warn("Rooms: discovery attempt failed: %s" % exc)
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2 ** attempt))
        self._rooms = None
        self._discovery_failed_ms = _now_ms()  # backoff until the next poll
        if self._warn:
            self._warn("Rooms: discovery failed: %s" % last_error)

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
        heating_seen = set()       # rooms already claimed by a group
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
                # One group per room (the AGENTS.md aggregate): a second
                # HmIP-HEATING in the same room must NOT double-count the
                # room in the demand denominator.
                if room_id not in heating_seen:
                    heating_seen.add(room_id)
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
                    active.append({"room_id": str(room_id),
                                   "room_name": rooms[room_id]["name"],
                                   "addr": addr, "iface": iface,
                                   "kind": "group"})
        elif self._mode == "heating_groups+etrvs":
            # P6a union: group if the room has one (WTH-filtered, as today),
            # else the room's eTRV average. A room with a group at all is NOT
            # double-counted via its eTRVs (heating_seen covers that).
            for room_id, addr, iface in heating:
                if room_id in wth_rooms:
                    active.append({"room_id": str(room_id),
                                   "room_name": rooms[room_id]["name"],
                                   "addr": addr, "iface": iface,
                                   "kind": "group"})
            for room_id, entry in etrv_by_room.items():
                if room_id not in heating_seen:  # room has no group
                    active.append({"room_id": str(room_id),
                                   "room_name": rooms[room_id]["name"],
                                   "addrs": entry["addrs"],
                                   "iface": entry["iface"], "kind": "etrv"})
        else:  # etrv
            for room_id, entry in etrv_by_room.items():
                active.append({"room_id": str(room_id),
                               "room_name": rooms[room_id]["name"],
                               "addrs": entry["addrs"],
                               "iface": entry["iface"], "kind": "etrv"})
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
                            and r.get("room_id") is not None
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
            if self._warn:
                self._warn("Rooms: could not write cache")

    # -- per-room weights + liveness registry (P6a) --------------------------
    def _load_registry(self):
        try:
            with open(self._registry_path, "r") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("rooms"), dict):
                return dict((str(rid), entry)
                            for rid, entry in data["rooms"].items()
                            if isinstance(entry, dict))
        except (OSError, ValueError, AttributeError):
            pass
        return {}

    def _save_registry(self):
        try:
            temporary = self._registry_path + ".new"
            with open(temporary, "w") as handle:
                json.dump({"version": 1, "rooms": self._registry}, handle)
                handle.flush()
            try:
                os.remove(self._registry_path)
            except OSError:
                pass
            os.rename(temporary, self._registry_path)
        except Exception:
            if self._warn:
                self._warn("Rooms: could not write weights registry")

    def _reconcile_registry(self, active):
        """Add new rooms (weight 1.0), keep human weights, flag vanished rooms
        ``sensors_alive=false`` WITHOUT deleting them (a dead battery must not
        erase the tuning). Writes flash only on an actual change; liveness
        flips are edge-triggered (one WARN per transition, no flash storm).
        """
        now = _now_ms()
        changed = False
        seen = set()
        for room in active:
            if room.get("room_id") is None:  # defensive: unkeyable entry
                continue
            rid = str(room.get("room_id"))
            seen.add(rid)
            name = room.get("room_name", "")
            kind = room.get("kind", "")
            entry = self._registry.get(rid)
            if entry is None:
                self._registry[rid] = {"name": name, "kind": kind,
                                       "weight": 1.0, "sensors_alive": True,
                                       "last_seen_ms": now}
                changed = True
                continue
            entry["last_seen_ms"] = now
            if entry.get("name") != name or entry.get("kind") != kind:
                entry["name"] = name
                entry["kind"] = kind
                changed = True
            if not entry.get("sensors_alive"):
                entry["sensors_alive"] = True
                changed = True
                if self._log:
                    self._log("Rooms: room %s (%s) back in the room source"
                              % (name, rid))
        for rid, entry in self._registry.items():
            if rid not in seen and entry.get("sensors_alive"):
                entry["sensors_alive"] = False
                changed = True
                if self._warn:
                    # Covers both real device loss and mode exclusion (a room
                    # source change drops rooms from discovery too).
                    self._warn("Rooms: room %s (%s) no longer in the room "
                               "source" % (entry.get("name", ""), rid))
        if changed:
            self._save_registry()

    def _weight_for(self, room_id):
        entry = self._registry.get(str(room_id)) if room_id is not None else None
        if not entry:
            return 1.0
        try:
            weight = float(entry.get("weight", 1.0))
        except (TypeError, ValueError):
            return 1.0
        if weight < 0.0:
            return 0.0
        if weight > 1.0:
            return 1.0
        return weight

    # -- public surface for the shell / future UI / MQTT ---------------------
    def weights(self):
        """Registry snapshot (sorted by room name) for `rooms` (bare list)."""
        out = []
        for rid in sorted(self._registry,
                          key=lambda k: (self._registry[k].get("name", ""), k)):
            entry = self._registry[rid]
            out.append({"room_id": rid, "name": entry.get("name", ""),
                        "kind": entry.get("kind", ""),
                        "weight": float(entry.get("weight", 1.0)),
                        "sensors_alive": bool(entry.get("sensors_alive", False)),
                        "last_seen_ms": entry.get("last_seen_ms")})
        return out

    def card(self, ref):
        """One room card (registry + this pass's readings) -> (card, error).

        ``ref`` is a room_id or a (unique, case-insensitive) room name.
        """
        rid, err = self._resolve_room(ref)
        if err:
            return None, err
        return self._card_for(rid), None

    def cards(self):
        """Every registry card (same name-sorted order as weights())."""
        return [self._card_for(rid) for rid in sorted(
            self._registry,
            key=lambda k: (self._registry[k].get("name", ""), k))]

    def _card_for(self, rid):
        entry = self._registry[rid]
        rd = (self._last or {}).get("room_data", {}).get(rid) or {}
        sp = rd.get("sp")
        act = rd.get("act")
        return {"room_id": rid, "name": entry.get("name", ""),
                "kind": entry.get("kind", ""),
                "weight": float(entry.get("weight", 1.0)),
                "sensors_alive": bool(entry.get("sensors_alive", False)),
                "last_seen_ms": entry.get("last_seen_ms"),
                "setpoint": sp, "actual": act,
                "demand_delta": (sp - act) if (sp is not None
                                               and act is not None) else None}

    def _resolve_room(self, ref):
        """room_id for a ref given as id or (unique) name -> (id, error)."""
        ref = str(ref)
        if ref in self._registry:
            return ref, None
        matches = [rid for rid, entry in self._registry.items()
                   if str(entry.get("name", "")).lower() == ref.lower()]
        if len(matches) == 1:
            return matches[0], None
        if not matches:
            return None, "no such room: %s" % ref
        return None, ("ambiguous room name %s (%d rooms); use the room_id"
                      % (ref, len(matches)))

    def set_weight(self, ref, weight):
        rid, err = self._resolve_room(ref)
        if err:
            return False, err
        try:
            value = float(weight)
        except (TypeError, ValueError):
            return False, "weight must be a number in 0..1"
        if value < 0.0 or value > 1.0:
            return False, "weight must be in 0..1"
        self._registry[rid]["weight"] = value
        self._save_registry()
        return True, "weight %s (%s) = %.2f" % (
            self._registry[rid].get("name", ""), rid, value)

    def forget(self, ref):
        rid, err = self._resolve_room(ref)
        if err:
            return False, err
        name = self._registry.pop(rid, {}).get("name", rid)
        self._save_registry()
        return True, "removed %s (%s) from the weights registry" % (name, rid)
