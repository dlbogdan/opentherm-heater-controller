"""Memory-efficient heating-group (room) setpoint/actual collector.

Polls the CCU3 heating groups **one by one**: for each room it reads
``SET_POINT_TEMPERATURE`` (the room setpoint) and ``ACTUAL_TEMPERATURE``
(the measured room temperature), folds them into running scalars, and drops
the per-group values before the next room. It never holds a per-group list,
so per-poll RAM is O(1) no matter how many rooms there are. Only the small
group *identity* list (iface/addr/name) is kept, discovered once and cached
to flash -- like the weather endpoint -- so a poll is just N sequential
read pairs.

``read()`` returns a compact aggregate: the raw per-room setpoint/actual
stats **and** the heating-demand signal, computed exactly like the working
ReGaHd delta script: a room is *demanding* when ``5 < setpoint < 30`` and
``setpoint > actual``; the demand is the average delta over **all**
thermostats, saturated at ``demand_delta_cap`` (default 3 degC) and clamped
to 0..100%.

The canonical CCU3 endpoint / auth / value-key recipe is in ``AGENTS.md``
under "CCU3 / Homematic data reference". In particular: the setpoint key is
``SET_POINT_TEMPERATURE`` (NOT ``SETPOINT`` -- that key is valid but always
empty), and the groups are the ``HmIP-HEATING`` VirtualDevices (one per main
room), not the individual eTRV valves.
"""

try:
    import ujson as json
except ImportError:  # CPython host tests
    import json
try:
    import uos as os
except ImportError:  # CPython host tests
    import os

from ccu3 import Ccu3Rpc, Ccu3Error

# Homematic IP heating value-keys (see AGENTS.md; verified on-device).
SETPOINT_KEY = "SET_POINT_TEMPERATURE"   # room setpoint (deg C)
ACTUAL_KEY = "ACTUAL_TEMPERATURE"        # measured room temperature (deg C)

# Setpoints at or below/above these are OFF/ON modes, not real room
# setpoints -- the ReGaHd delta script skips them when computing demand.
SETPOINT_MIN_VALID = 5.0
SETPOINT_MAX_VALID = 30.0


class HeatingGroups:
    """Collect per-room setpoints + actuals from the heating groups."""

    name = "heating_groups"

    def __init__(self, config, cache_path="/ccu3_groups_cache.json",
                 http_post=None, log=None):
        self._config = config
        self._cache_path = cache_path
        self._log = log
        self._rpc = Ccu3Rpc(config.get("ccu3_url"),
                            config.get("ccu3_user"),
                            config.get("ccu3_pass"),
                            http_post=http_post, log=log)
        self._groups = self._load_cache()  # list of {iface, addr, name}

    # -- one pass over all rooms, one at a time --------------------------------
    def read(self):
        """Full pass; returns a compact O(1) aggregate (None for empty)."""
        if self._groups is None:
            self._discover()
        groups = self._groups or []

        total = len(groups)
        reading = 0
        sum_sp = 0.0
        n_sp = 0
        max_sp = None
        sum_act = 0.0
        n_act = 0
        max_act = None
        # Demand (ReGaHd delta script): denominator = all thermostats,
        # numerator = delta summed over *demanding* rooms only.
        thermostats = 0
        total_delta = 0.0
        n_delta = 0
        max_delta = None
        max_delta_name = ""
        for group in groups:
            sp = self._value(group, SETPOINT_KEY)
            act = self._value(group, ACTUAL_KEY)
            # Fold this room in, then drop it (never accumulated across rooms).
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
                        max_delta_name = group.get("name", "")
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

        return {
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

    def _value(self, group, key):
        """Read one value key for one room -> float, or None if absent."""
        try:
            result = self._rpc.call(
                "Interface.getValue",
                {"interface": group["iface"],
                 "address": group["addr"] + ":1",
                 "valueKey": key})
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

    # -- discovery (once; cached to flash) -------------------------------------
    def _discover(self):
        group_type = self._config.get("heating_group_type") or "HmIP-HEATING"
        found = []
        try:
            ids = self._rpc.call("Device.listAll", {}) or []
        except Ccu3Error as exc:
            if self._log:
                self._log("HeatingGroups: discovery failed: %s" % exc)
            return
        for device_id in ids:
            try:
                device = self._rpc.call("Device.get", {"id": device_id})
            except Ccu3Error:
                continue  # skip CCU system/virtual devices that error
            if not isinstance(device, dict):
                continue
            if group_type not in str(device.get("type", "")):
                continue
            iface = device.get("interface")
            addr = device.get("address")
            if iface and addr:
                found.append({"iface": iface, "addr": addr,
                              "name": device.get("name", "")})
        self._groups = found
        self._save_cache(found)
        if self._log:
            self._log("HeatingGroups: %d groups (%s)" % (len(found), group_type))

    # -- flash cache (discovered once, read at boot) ----------------------------
    def _load_cache(self):
        try:
            with open(self._cache_path, "r") as handle:
                data = json.load(handle)
            if isinstance(data, list) and data and all(
                    isinstance(g, dict) and g.get("iface") and g.get("addr")
                    for g in data):
                return data
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def _save_cache(self, groups):
        try:
            temporary = self._cache_path + ".new"
            with open(temporary, "w") as handle:
                json.dump(groups, handle)
                handle.flush()
            try:
                os.remove(self._cache_path)
            except OSError:
                pass
            os.rename(temporary, self._cache_path)
        except Exception:
            if self._log:
                self._log("HeatingGroups: could not write group cache")
