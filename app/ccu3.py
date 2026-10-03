"""Homematic CCU3 JSON-RPC client + weather-station sensor source (arch §3.4).

**Non-blocking.** All I/O is async (uasyncio on device, asyncio on host) using
the same ``open_connection`` / ``wait_for`` pattern the framework's firmware
updater uses, so a CCU3 read *yields* the event loop instead of stalling the
whole board. Plain HTTP/1.0, one request per connection, no TLS (trusted LAN).

JSON-RPC: ``Session.login`` for a session id, then every call carries
``_session_id_``. Session expiry (``code == -1`` / "not logged in" /
"access denied" / "nicht angemeldet") triggers one re-login + retry.

The CCU3 sends **no** ``Content-Length``, so the response body is delimited by
the server closing the connection; we read it in bounded 512-byte chunks until
EOF (memory-safe on the Pico).

``http_post`` is injectable (host tests pass an async fake); the default is the
built-in async client.
"""

import time

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
    import gc  # defragment the heap before a (potentially large) read
except ImportError:  # CPython host tests
    gc = None


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


async def _aclose(writer):
    """Close a uasyncio/asyncio writer (``close()`` may be sync or a coro)."""
    try:
        result = writer.close()
        if hasattr(result, "__await__"):
            await result
    except Exception:
        pass
    try:
        if hasattr(writer, "wait_closed"):
            closed = writer.wait_closed()
            if hasattr(closed, "__await__"):
                await closed
    except Exception:
        pass


async def _http_post_async(url, body, timeout_s=5.0):
    """Minimal non-blocking HTTP/1.0 POST, one request per connection.

    Returns ``(status_line, headers, body_bytes)``. Raises on connect/read
    problems (incl. ``asyncio.TimeoutError``). The body is read until the
    server closes the connection (the CCU3 sends no ``Content-Length``).
    """
    host, port, path = _parse_url(url)
    request = ("POST %s HTTP/1.0\r\n"
               "Host: %s\r\n"
               "Content-Type: application/json\r\n"
               "Content-Length: %d\r\n"
               "Connection: close\r\n\r\n"
               % (path, host, len(body))).encode()
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout_s)
    try:
        writer.write(request + body)  # headers AND the JSON body
        await asyncio.wait_for(writer.drain(), timeout_s)
        if gc is not None:
            gc.collect()  # defragment the heap before the (possibly large) read
        status_line = await asyncio.wait_for(reader.readline(), timeout_s)
        headers = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout_s)
            if line == b"" or line.strip() == b"":
                break
            if b":" in line:
                name, value = line.split(b":", 1)
                headers[name.strip().lower().decode()] = value.strip().decode()
        if gc is not None:
            gc.collect()  # again, right before the body read (largest alloc)
        chunks = []
        while True:
            chunk = await asyncio.wait_for(reader.read(512), timeout_s)
            if not chunk:
                break
            chunks.append(chunk)
        payload = b"".join(chunks)
    finally:
        await _aclose(writer)
    # NB: MicroPython's bytes.decode() takes NO keyword args (no errors=),
    # so decode with the default (utf-8); the status line is ASCII.
    return status_line.decode().strip(), headers, payload


http_post = _http_post_async  # public alias (also the default)


class Ccu3Rpc:
    """Async JSON-RPC client with login + one re-login on session expiry.

    ``http_post`` is an async callable ``(url, body) -> (status, headers,
    payload)``, injectable for host tests; default is the built-in client.
    """

    def __init__(self, url, username, password, http_post=None, log=None,
                 warn=None):
        self.url = url
        self.username = username
        self.password = password
        self._post = (http_post if http_post is not None
                      else _http_post_async)
        self._log = log            # INFO -> console only
        self._warn = warn if warn is not None else log  # WARN/ERROR -> flash
        self._session = None
        self._next_id = 0

    async def _raw_call(self, method, params):
        self._next_id += 1
        request = json.dumps({
            "method": method, "params": params,
            "id": self._next_id, "jsonrpc": "2.0"}).encode()
        status, _headers, payload = await self._post(self.url, request)
        if not (status.startswith("HTTP/1.0 200")
                or status.startswith("HTTP/1.1 200")):
            raise Ccu3Error("http %s" % status)
        try:
            response = json.loads(payload.decode())
        except (ValueError, AttributeError) as exc:
            raise Ccu3Error("invalid JSON response") from exc
        if response.get("error"):
            raise Ccu3Error(str(response["error"].get(
                "message", response["error"])))
        return response.get("result")

    async def login(self):
        self._session = await self._raw_call(
            "Session.login",
            {"username": self.username, "password": self.password})
        if self._log:
            self._log("CCU3: session established")
        return self._session

    async def call(self, method, params, _retried=False):
        """One RPC call; re-logins once on a session-expiry error."""
        if self._session is None:
            await self.login()
        call_params = dict(params)
        call_params["_session_id_"] = self._session
        try:
            return await self._raw_call(method, call_params)
        except Ccu3Error as exc:
            if not _retried and _looks_like_session_error(exc):
                if self._warn:
                    self._warn("CCU3: session expired (%s); re-login + retry"
                               % exc)
                self._session = None
                await self.login()
                return await self.call(method, params, _retried=True)
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
                 http_post=None, sleep=None, log=None, warn=None):
        self._config = config
        self._cache_path = cache_path
        self._log = log            # INFO -> console only
        self._warn = warn if warn is not None else log  # WARN/ERROR -> flash
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._rpc = Ccu3Rpc(config.get("ccu3_url"),
                            config.get("ccu3_user"),
                            config.get("ccu3_pass"),
                            http_post=http_post, log=log, warn=warn)
        self._values = None  # (t_out, lux, ts_ms)
        self._endpoint = None  # (interface, address)
        cache = self._load_cache()
        if cache:
            self._endpoint = (cache["interface"], cache["address"])
            if log:
                log("CCU3: endpoint from cache (%s %s)"
                    % (cache["interface"], cache["address"]))

    # -- SensorSource --------------------------------------------------------
    async def read(self):
        now = _now_ms()
        poll_ms = int(self._config.get("ccu3_poll_s")) * 1000
        if self._values is None or now - self._values[2] >= poll_ms:
            await self._poll(now)
        if self._values is None:
            return (None, None, None)
        t_out, lux, ts = self._values
        if now - ts > self.STALE_LIMIT_S * 1000:
            self._values = None  # too old to trust: let the controller failsafe
            if self._warn:
                self._warn("CCU3: last value too stale; reporting no reading")
            return (None, None, None)
        return (t_out, lux, None)

    # -- polling -------------------------------------------------------------
    async def _poll(self, now):
        try:
            if self._endpoint is None:
                await self._discover()
            iface, addr = self._endpoint
            t_out = await self._get_value(iface, addr, "ACTUAL_TEMPERATURE")
            lux = await self._get_value(iface, addr, "ILLUMINATION")
            self._values = (t_out, lux, now)
            if self._log:
                self._log("CCU3: t_out=%s lux=%s" % (t_out, lux))
        except Exception as exc:
            if self._warn:
                self._warn("CCU3: poll failed (keeping last value if any): %s"
                           % exc)

    async def _get_value(self, iface, addr, key):
        result = await self._rpc.call(
            "Interface.getValue",
            {"interface": iface, "address": addr + ":1", "valueKey": key})
        if isinstance(result, dict):
            result = result.get("value")
        try:
            return float(result)
        except (TypeError, ValueError):
            return None

    # -- discovery (only on cache miss) --------------------------------------
    async def _discover(self):
        weather_type = self._config.get("ccu3_weather_type")
        last_error = None
        for attempt in range(3):  # initial try + 2 retries, backoff
            try:
                ids = await self._rpc.call("Device.listAll", {}) or []
                for device_id in ids:
                    try:
                        device = await self._rpc.call(
                            "Device.get", {"id": device_id})
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
                    await self._sleep(0.5 * (2 ** attempt))
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
            if self._warn:
                self._warn("CCU3: could not write discovery cache")
