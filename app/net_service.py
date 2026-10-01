"""On-device network service: remote status, debug, and update -- no USB needed.

Once Wi-Fi is up, this exposes two listeners (uasyncio, framework-free of the
control core):

* **HTTP API** (``http_port``, default 8080)
    - ``GET  /status``   device + control status as JSON
    - ``GET  /log?n=N``  tail of the framework log (``/log.txt``)
    - ``POST /selftest`` run the control-core self-test, return the result
    - ``POST /reboot``   clean reboot (the boot-time OTA check then installs any
      newer build served by the update server) -- this is "update via network"
* **Debug console** (``console_port``, default 8081)
    A line-based Python eval loop over raw TCP. Each line is ``exec``'d in a
    persistent namespace and its stdout/stderr is returned, terminated by an
    ``<<<END>>>`` marker (see ``tools/console.py`` for the client).

The service is deliberately small and defensive: any handler error is contained
to that request, and a failure to start the service must never take down the
control loop.
"""

import gc
import io
import sys
import time

import uasyncio as asyncio
import ujson as json
import machine
import uos

import lib.coresys.logger as logger
from lib.coresys.ota_state import load_state

from config import config
from state import state

SERVICE_NAME = "otc-net"
END_MARKER = "<<<END>>>"


class NetService:
    def __init__(self, wifi):
        self.wifi = wifi
        self.start_ms = time.ticks_ms()
        self._http_server = None
        self._console_server = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self):
        """Bind both listeners and serve until the loop is torn down."""
        http_port = int(config.get("http_port"))
        console_port = int(config.get("console_port"))
        self._http_server = await asyncio.start_server(
            self._handle_http, "0.0.0.0", http_port)
        self._console_server = await asyncio.start_server(
            self._handle_console, "0.0.0.0", console_port)
        ip = self.wifi.get_ip() or "0.0.0.0"
        logger.info("Net: listening on %s (http :%s, console :%s)"
                    % (ip, http_port, console_port), log_to_file=True)
        # Serve forever; per-connection handlers run as independent loop tasks.
        while True:
            await asyncio.sleep(3600)

    # -------------------------------------------------------------------- helpers
    def _respond(self, writer, code, ctype, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        reason = {200: "OK", 404: "Not Found",
                  405: "Method Not Allowed", 500: "Server Error"}.get(code, "OK")
        head = ("HTTP/1.1 %d %s\r\nContent-Type: %s\r\n"
                "Content-Length: %d\r\nConnection: close\r\n\r\n"
                % (code, reason, ctype, len(body)))
        writer.write(head.encode() + body)

    def _status_obj(self):
        d = {"service": SERVICE_NAME}
        try:
            d["version"] = open("/version.txt").read().strip()
        except OSError:
            d["version"] = None
        try:
            st = load_state()
            d["slot"] = {"active": st.get("active"), "pending": st.get("pending"),
                         "rejected": st.get("rejected_version")}
        except Exception:
            d["slot"] = None
        gc.collect()
        d["uptime_s"] = time.ticks_diff(time.ticks_ms(), self.start_ms) // 1000
        d["heap_free"] = gc.mem_free()
        w = self.wifi
        d["wifi"] = {"state": w.get_state(), "ip": w.get_ip(),
                     "rssi": w.get_signal_strength(), "ssid": w.get_ssid()}
        d["heating_on"] = state.heating_on
        d["control"] = {k: config.get(k) for k in
                        ("t_off", "t_on", "flow_min", "flow_min_on", "flow_max",
                         "t_design", "flow_design", "curve_base", "b",
                         "transport", "net_enabled")}
        return d

    @staticmethod
    def _log_tail(n):
        n = min(200, max(1, n))
        lines = []
        try:
            with open("/log.txt") as log_file:
                for line in log_file:
                    lines.append(line.rstrip("\r\n"))
                    if len(lines) > n:
                        lines.pop(0)
        except OSError:
            return "(no log)"
        return "\n".join(lines)

    @staticmethod
    def _run_selftest():
        for p in ("apps/a", "apps/b"):
            try:
                sys.path.insert(0, p)
            except Exception:
                pass
        import selftest
        ok = selftest.run()
        return ok, "control core self-test: %s" % ("PASS" if ok else "FAIL")

    # ----------------------------------------------------------------------- HTTP
    async def _read_request(self, reader):
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = await reader.read(256)
            if not chunk:
                break
            request += chunk
            if len(request) > 8192:
                break
        head, _, _body = request.partition(b"\r\n\r\n")
        lines = head.decode("utf-8", "replace").split("\r\n")
        method, path = "GET", "/"
        if lines:
            parts = lines[0].split()
            if len(parts) >= 2:
                method, path = parts[0], parts[1]
        return method, path

    async def _handle_http(self, reader, writer):
        try:
            method, path = await self._read_request(reader)
            query = path.split("?", 1)[1] if "?" in path else ""
            route = path.split("?", 1)[0]

            if method == "GET" and route == "/status":
                self._respond(writer, 200, "application/json",
                              json.dumps(self._status_obj()))
            elif method == "GET" and route == "/log":
                n = 40
                if "n=" in query:
                    try:
                        n = int(query.split("n=", 1)[1].split("&", 1)[0])
                    except ValueError:
                        n = 40
                self._respond(writer, 200, "text/plain", self._log_tail(n))
            elif method == "POST" and route == "/selftest":
                ok, out = self._run_selftest()
                self._respond(writer, 200, "application/json",
                              json.dumps({"pass": ok, "output": out[-2000:]}))
            elif method == "POST" and route == "/reboot":
                self._respond(writer, 200, "application/json",
                              json.dumps({"reboot": True}))
                await writer.drain()
                logger.error("Net: reboot requested via HTTP", log_to_file=True)
                await asyncio.sleep(0.3)
                machine.reset()
                return  # unreachable; reset() does not return
            elif method == "GET" and route in ("/", "/index.html"):
                self._respond(writer, 200, "text/plain",
                              SERVICE_NAME + " ok. Try /status /log /selftest /reboot")
            else:
                self._respond(writer, 404, "text/plain", "not found: " + route)
        except Exception as e:
            try:
                self._respond(writer, 500, "text/plain", "error: %s" % e)
                await writer.drain()
            except Exception:
                pass
            logger.error("Net HTTP handler error: %s" % e, log_to_file=True)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    # ------------------------------------------------------------------- console
    async def _handle_console(self, reader, writer):
        ns = {"__name__": "pico-console"}
        try:
            writer.write(("OpenTherm Pico debug console (persistent namespace).\n"
                          "Type Python lines; 'exit' to quit.\n" +
                          END_MARKER + "\n").encode())
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                if text in ("exit", "quit"):
                    break
                out, err_text = self._eval_line(text, ns)
                writer.write((out + err_text + END_MARKER + "\n").encode())
                await writer.drain()
        except Exception as e:
            logger.error("Net console error: %s" % e, log_to_file=True)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    @staticmethod
    def _eval_line(text, ns):
        output = []

        def console_print(*values, **kwargs):
            sep = kwargs.get("sep", " ")
            end = kwargs.get("end", "\n")
            output.append(sep.join(str(value) for value in values) + end)

        ns["print"] = console_print
        err_text = ""
        try:
            try:
                result = eval(text, ns)
            except SyntaxError:
                exec(text, ns)
                result = None
            if result is not None:
                output.append(repr(result) + "\n")
        except Exception as e:
            errbuf = io.StringIO()
            sys.print_exception(e, file=errbuf)
            err_text = errbuf.getvalue()
        return "".join(output), err_text
