"""Homematic CCU3 weather-station sensor source (arch §3.4).

JSON-RPC-over-HTTP against the CCU3 (``POST /api/``): login for a session,
one-time device discovery (cached to ``ccu3_cache.json``), then per-poll
``Interface.getValue`` for ``ACTUAL_TEMPERATURE`` (-> ``t_out``) and
``ILLUMINATION`` (-> ``lux``). Plain HTTP/1.0, one request per connection,
no TLS (trusted LAN). The HTTP layer is injectable so the whole protocol
logic is unit-testable on a host; the device path uses the built-in
``usocket`` client.

Read semantics (what the control loop sees from ``read()``):
* fresh value (< ``ccu3_poll_s`` old) -> returned directly;
* otherwise one poll is attempted; a failure keeps the last value;
* a last value older than ``STALE_LIMIT_S`` is dropped (``t_out=None``),
  which is the controller's defined failsafe input -- the boiler goes to a
  safe fixed flow instead of trusting a day-old temperature.
"""

import time

try:
    import ujson as json
except ImportError:  # CPython host tests
    import json
try:
    import usocket as socket
except ImportError:  # CPython host tests
    import socket
try:
    import uos as os
except ImportError:  # CPython host tests
    import os


class Ccu3Error(Exception):
    """Protocol-level CCU3 failure (transport or RPC error)."""


def _now_ms():
    try:
        return time.ticks_ms()
    except AttributeError:
        return int(time.monotonic() * 1000)


def _parse_url(url):
    """scheme://host[:port]/path -> (host, port, path)."""
    _scheme, _sep, rest = url.partition("://")
    host, _sep2, path = rest.partition("/")
    path = "/" + path
    port = 80
    if ":" in host:
        host, port_str = host.rsplit(":", 1)
        port = int(port_str)
    return host, port, path


def _http_post_default(url, body, timeout_s=5):
    """Minimal HTTP/1.0 POST, one request per connection (arch §3.4).

    Returns ``(status_line, headers, body_bytes)``. Raises ``OSError`` on
    connect/read problems. The response is read until the server closes the
    connection (HTTP/1.0 semantics), which matches the CCU3.
    """
    host, port, path = _parse_url(url)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect((host, port))
        request = ("POST %s HTTP/1.0\r\n"
                   "Host: %s\r\n"
                   "Content-Type: application/json\r\n"
                   "Content-Length: %d\r\n"
                   "Connection: close\r\n\r\n"
                   % (path, host, len(body))).encode()
        sock.sendall(request + body)
        chunks = []
        while True:
            chunk = sock.recv(512)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        sock.close()
    head, _sep, body_out = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    if not lines or not lines[0]:
        raise Ccu3Error("empty HTTP response")
    status = lines[0].decode()
    headers = {}
    for line in lines[1:]:
        if b":" in line:
            key, value = line.split(b":", 1)
            headers[key.strip().lower().decode()] = value.strip().decode()
    return status, headers, body_out


def _is_session_error(error):
    """True when an RPC error means the session must be re-established."""
    if not isinstance(error, dict):
        return False
    if error.get("code") == -1:
        return True
    message = str(error.get("message", "")).lower()
    return ("session" in message or "not logged in" in message
            or "access denied" in message or "nicht angemeldet" in message)


http_post = _http_post_default  # public alias (also the default)


class Ccu3Rpc:
    """JSON-RPC client with login + one re-login on session expiry.

    ``http_post`` is injectable (host tests pass a fake); default is the
    built-in HTTP/1.0 client.
    """

    def __init__(self, url, username, password, http_post=None, log=None):
        self.url = url
        self.username = username
        self.password = password
        self._post = (http_post if http_post is not None
                 else _http_post_default)
        self._log = log
        self._session = None
        self._next_id = 0

    def _raw_call(self, method, params):
        self._next_id += 1
        request = json.dumps({
            "method": method, "params": params,
            "id": self._next_id, "jsonrpc": "2.0"}).encode()
        status, _headers, payload = self._post(self.url, request)
        if not (status.startswith("HTTP/1.0 200")
                or status.startswith("HTTP/1.1 200")):
            raise Ccu3Error("http %s" % status.strip())
        try:
            response = json.loads(payload.decode())
        except (ValueError, AttributeError) as exc:
            raise Ccu3Error("invalid JSON response") from exc
        if response.get("error"):
            raise Ccu3Error(str(response["error"].get(
                "message", response["error"])))
        return response.get("result")

    def login(self):
        self._session = self._raw_call(
            "Session.login",
            {"username": self.username, "password": self.password})
        if self._log:
            self._log("CCU3: session established")
        return self._session

    def call(self, method, params, _retried=False):
        """One RPC call; re-logins once on a session-expiry error."""
        if self._session is None:
            self.login()
        call_params = dict(params)
        call_params["_session_id_"] = self._session
        try:
            return self._raw_call(method, call_params)
        except Ccu3Error as exc:
            if not _retried and _looks_like_session_error(exc):
                if self._log:
                    self._log("CCU3: session expired (%s); re-login + retry"
                              % exc)
                self._session = None
                self.login()
                return self.call(method, params, _retried=True)
            raise


def _looks_like_session_error(exc):
    message = str(exc).lower()
    return ("session" in message or "not logged in" in message
            or "access denied" in message or "nicht angemeldet" in message)


class Ccu3SensorSource:
    """``read() -> (t_out, lux, demand_raw)`` from the CCU3 weather station."""

    name = "ccu3"
    STALE_LIMIT_S = 600  # last value older than this -> failsafe (t_out=None)

    def __init__(self, config, cache_path="/ccu3_cache.json",
                 http_post=None, sleep=None, log=None):
        self._config = config
        self._cache_path = cache_path
        self._log = log
        self._sleep = sleep if sleep is not None else time.sleep
        self._rpc = Ccu3Rpc(config.get("ccu3_url"),
                            config.get("ccu3_user"),
                            config.get("ccu3_pass"),
                            http_post=http_post, log=log)
        self._values = None  # (t_out, lux, ts_ms)
        self._endpoint = None  # (interface, address)
        cache = self._load_cache()
        if cache:
            self._endpoint = (cache["interface"], cache["address"])
            if log:
                log("CCU3: endpoint from cache (%s %s)"
                    % (cache["interface"], cache["address"]))

    # -- SensorSource --------------------------------------------------------
    def read(self):
        now = _now_ms()
        poll_ms = int(self._config.get("ccu3_poll_s")) * 1000
        if self._values is None or now - self._values[2] >= poll_ms:
            self._poll(now)
        if self._values is None:
            return (None, None, None)
        t_out, lux, ts = self._values
        if now - ts > self.STALE_LIMIT_S * 1000:
            self._values = None  # too old to trust: let the controller failsafe
            if self._log:
                self._log("CCU3: last value too stale; reporting no reading")
            return (None, None, None)
        return (t_out, lux, None)

    # -- polling -------------------------------------------------------------
    def _poll(self, now):
        try:
            if self._endpoint is None:
                self._discover()
            iface, addr = self._endpoint
            t_out = self._get_value(iface, addr, "ACTUAL_TEMPERATURE")
            lux = self._get_value(iface, addr, "ILLUMINATION")
            self._values = (t_out, lux, now)
            if self._log:
                self._log("CCU3: t_out=%s lux=%s" % (t_out, lux))
        except Exception as exc:
            if self._log:
                self._log("CCU3: poll failed (keeping last value if any): %s"
                          % exc)

    def _get_value(self, iface, addr, key):
        result = self._rpc.call(
            "Interface.getValue",
            {"interface": iface, "address": addr + ":1", "valueKey": key})
        if isinstance(result, dict):
            result = result.get("value")
        try:
            return float(result)
        except (TypeError, ValueError):
            return None

    # -- discovery (only on cache miss) --------------------------------------
    def _discover(self):
        weather_type = self._config.get("ccu3_weather_type")
        last_error = None
        for attempt in range(3):  # initial try + 2 retries, 0.5s * 2^n backoff
            try:
                ids = self._rpc.call("Device.listAll", {}) or []
                for device_id in ids:
                    try:
                        device = self._rpc.call("Device.get",
                                                {"id": device_id})
                    except Ccu3Error:
                        # Some CCU system/virtual devices break the CCU's own
                        # get handler (Tcl error); skip them and keep looking.
                        continue
                    if not isinstance(device, dict):
                        continue
                    if weather_type not in str(device.get("type", "")):
                        continue
                    iface = device.get("interface")
                    addr = device.get("address")
                    if iface and addr:
                        self._endpoint = (iface, addr)
                        self._save_cache(iface, addr)
                        if self._log:
                            self._log("CCU3: discovered %s at %s (%s)"
                                      % (weather_type, addr, iface))
                        return
                raise Ccu3Error("no %s device found" % weather_type)
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    self._sleep(0.5 * (2 ** attempt))
        raise Ccu3Error("discovery failed: %s" % last_error)

    # -- flash cache (discovery result; written once, read at boot) ----------
    def _load_cache(self):
        try:
            with open(self._cache_path, "r") as handle:
                data = json.load(handle)
            if (isinstance(data, dict) and data.get("interface")
                    and data.get("address")):
                return data
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def _save_cache(self, iface, addr):
        try:
            temporary = self._cache_path + ".new"
            with open(temporary, "w") as handle:
                json.dump({"interface": iface, "address": addr,
                           "discovered_at": _now_ms()}, handle)
                handle.flush()
            try:
                os.remove(self._cache_path)
            except OSError:
                pass
            os.rename(temporary, self._cache_path)
        except Exception:
            # The cache is an optimization (skip re-discovery); its absence
            # must never break a poll.
            if self._log:
                self._log("CCU3: could not write discovery cache")