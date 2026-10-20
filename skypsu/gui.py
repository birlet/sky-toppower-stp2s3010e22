"""Local web GUI for the SKY TOPPOWER STP2S3010E22: live chart, output switch, setpoints.

Starts a small web server on 127.0.0.1 ONLY and opens the page in the browser.
A background thread owns the serial connection, polls the device ~4x per second and executes
commands from the page through SkyPSU (identity, limit and read-back checks).

  python -m skypsu.gui                 # find the device automatically
  python -m skypsu.gui --port COM8 --http-port 8765 --no-browser

Software is no substitute for a physical emergency stop.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import serial

from . import modbus
from .psu import PsuError, SkyPSU, assess

HTML_FILE = Path(__file__).with_name("gui.html")
ON_CHECK_DELAY = 1.2  # s after switching on: the voltage is stable after ~0.7 s


class Controller(threading.Thread):
    """Owns the serial connection. Every device access runs in this thread."""

    def __init__(self, port: str | None, interval: float, history_s: float):
        super().__init__(daemon=True)
        self.port = port
        self.interval = interval
        self.psu: SkyPSU | None = None
        self.lock = threading.Lock()
        self.samples: deque[dict] = deque(maxlen=int(history_s / interval) + 10)
        self.events: deque[dict] = deque(maxlen=100)
        self.seq = 0
        self.event_id = 0
        self.state: dict = {"connected": False, "link": None, "error": "connecting ...", "status": None}
        self.commands: queue.Queue = queue.Queue()
        self.on_check_due: float | None = None
        self.stop = threading.Event()

    # ---------------------------------------------------------------- called from HTTP threads

    def submit(self, name: str, **kwargs) -> dict:
        done = threading.Event()
        box: dict = {}
        self.commands.put((name, kwargs, done, box))
        if not done.wait(10):
            return {"ok": False, "error": "timeout"}
        return box

    def snapshot(self, after_seq: int, after_event: int) -> dict:
        with self.lock:
            return {**self.state,
                    "samples": [s for s in self.samples if s["seq"] > after_seq],
                    "events": [e for e in self.events if e["id"] > after_event],
                    "interval": self.interval}

    # ---------------------------------------------------------------- internal

    def _event(self, level: str, text: str) -> None:
        with self.lock:
            self.event_id += 1
            self.events.append({"id": self.event_id, "t": time.time(), "level": level, "text": text})

    def _connect(self) -> None:
        psu = SkyPSU(port=self.port)
        link = psu.connect()
        self.psu = psu
        with self.lock:
            self.state.update(connected=True, error=None,
                              link={"port": link.port, "baud": link.baud, "slave": link.slave, "info": link.port_info})
        self._event("info", f"Connected: {link.port} @ {link.baud} baud, slave {link.slave}")

    def _disconnect(self, reason: str) -> None:
        if self.psu:
            self.psu.close()
        self.psu = None
        with self.lock:
            self.state.update(connected=False, error=reason)
        self._event("error", f"Connection lost: {reason}")

    def _fail_pending(self, reason: str) -> None:
        while not self.commands.empty():
            _name, _kw, done, box = self.commands.get_nowait()
            box.update(ok=False, error=reason)
            done.set()

    def _run_command(self, name: str, kw: dict) -> dict:
        psu = self.psu
        if not psu:
            return {"ok": False, "error": "not connected"}
        try:
            if name == "output":
                if kw.get("on"):
                    psu.output_on(settle=None)
                    self.on_check_due = time.monotonic() + ON_CHECK_DELAY
                    self._event("warn", "Output ON - voltage is on the terminals")
                else:
                    psu.output_off()
                    self.on_check_due = None
                    self._event("info", "Output OFF")
            elif name == "set":
                volt, curr = kw.get("volt"), kw.get("curr")
                v, a = psu.set(volt=None if volt is None else float(volt), curr=None if curr is None else float(curr))
                self._event("info", f"Setpoints: {v:.2f} V / {a:.3f} A")
            else:
                return {"ok": False, "error": f"unknown command {name}"}
            return {"ok": True}
        except (PsuError, OSError, ValueError) as exc:  # OSError covers Modbus and port errors
            self._event("error", str(exc))
            return {"ok": False, "error": str(exc)}

    def run(self) -> None:
        while not self.stop.is_set():
            if not self.psu:
                try:
                    self._connect()
                except (PsuError, serial.SerialException, OSError) as exc:
                    with self.lock:
                        self.state.update(connected=False, error=str(exc))
                    self._fail_pending("not connected")
                    self.stop.wait(2.0)
                    continue
            t0 = time.monotonic()
            try:
                while not self.commands.empty():
                    name, kw, done, box = self.commands.get_nowait()
                    box.update(self._run_command(name, kw))
                    done.set()
                st = self.psu.status()
            except (PsuError, modbus.ModbusError) as exc:  # single read errors: keep trying
                with self.lock:
                    self.state["error"] = f"read error: {exc}"
                self.stop.wait(self.interval)
                continue
            except OSError as exc:  # serial.SerialException: port gone (cable unplugged) -> search again
                self._disconnect(str(exc))
                continue

            with self.lock:
                self.seq += 1
                self.samples.append({"seq": self.seq, "t": time.time(), "v": st["voltage"], "i": st["current"],
                                     "p": st["power"], "vs": st["set_voltage"], "is": st["set_current"],
                                     "out": st["output"]})
                self.state.update(status=st, error=None)
            if self.on_check_due and time.monotonic() >= self.on_check_due:
                self.on_check_due = None
                for w in assess(st["output"], st["mode"] == "CC", st["ocp_tripped"],
                                st["voltage"], st["current"], st["set_voltage"]):
                    self._event("error", w)
            self.stop.wait(max(0.0, self.interval - (time.monotonic() - t0)))
        if self.psu:
            self.psu.close()


def make_handler(ctrl: Controller, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # no request log on the console
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data: dict) -> None:
            self._send(code, json.dumps(data).encode(), "application/json")

        def _host_ok(self) -> bool:
            # DNS rebinding protection: only requests addressed to 127.0.0.1/localhost
            if self.headers.get("Host", "") not in allowed_hosts:
                self._json(403, {"error": "host not allowed"})
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(200, HTML_FILE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/state":
                q = parse_qs(url.query)
                self._json(200, ctrl.snapshot(int(q.get("after", ["0"])[0]), int(q.get("ev", ["0"])[0])))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return
            # JSON only: plain cross-site forms cannot send this content type without a preflight
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._json(415, {"error": "application/json expected"})
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid JSON"})
                return
            path = urlparse(self.path).path
            if path == "/api/output":
                res = ctrl.submit("output", on=bool(body.get("on")))
            elif path == "/api/set":
                res = ctrl.submit("set", volt=body.get("volt"), curr=body.get("curr"))
            else:
                self._json(404, {"error": "not found"})
                return
            self._json(200 if res.get("ok") else 409, res)

    return Handler


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m skypsu.gui", description="Local web GUI for the SKY TOPPOWER STP2S3010E22")
    p.add_argument("--port", help="serial port (default: search)")
    p.add_argument("--http-port", type=int, default=8765)
    p.add_argument("--interval", type=float, default=0.25, help="poll interval in s (default: %(default)s)")
    p.add_argument("--history", type=float, default=600, help="history kept in s (default: %(default)s)")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = p.parse_args(argv)

    ctrl = Controller(args.port, args.interval, args.history)
    ctrl.start()
    hosts = {f"127.0.0.1:{args.http_port}", f"localhost:{args.http_port}"}
    server = ThreadingHTTPServer(("127.0.0.1", args.http_port), make_handler(ctrl, hosts))
    url = f"http://127.0.0.1:{args.http_port}/"
    print(f"GUI running at {url} (Ctrl+C stops; the output state is left unchanged)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ctrl.stop.set()
        ctrl.join(3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
