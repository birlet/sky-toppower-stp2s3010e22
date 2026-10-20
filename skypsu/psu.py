"""High-level driver for the SKY TOPPOWER STP2S3010E22.

The register layout comes from register_map.yaml. Writes are guarded:
- the device identity (section ``identity``) is checked before the first write of a connection
- limits (section ``limits``) are checked before every write
- every write is read back and verified

Example:
    with SkyPSU() as psu:              # finds the device (port given, or scan all ports)
        print(psu.identify())
        psu.set(volt=5.0, curr=0.2)
        print(psu.output_on())         # measures after 1 s and warns on OCP trip / CC mode
        print(psu.measure())
        psu.output_off()
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import serial
import yaml

from . import modbus

DEFAULT_MAP = Path(__file__).with_name("register_map.yaml")
VERIFY_TIMEOUT = 1.0  # s to wait for a written value to show up (output OFF takes up to ~0.4 s)


class PsuError(Exception):
    pass


class IdentityError(PsuError):
    pass


class LimitError(PsuError, ValueError):
    pass


class VerifyError(PsuError):
    pass


@dataclass(frozen=True)
class Reg:
    name: str
    addr: int
    words: int = 1
    decimals: int | None = None
    access: str = "r"
    status: str = "unknown"
    unit: str = ""
    note: str = ""


class RegisterMap:
    """Parsed register_map.yaml."""

    def __init__(self, data: dict):
        self.regs = {
            name: Reg(name, int(r["addr"]), int(r.get("words", 1)), r.get("decimals"), r.get("access", "r"),
                      r.get("status", "unknown"), r.get("unit", ""), r.get("note", ""))
            for name, r in data["registers"].items()
        }
        self.flags = {name: int(f["bit"]) for name, f in (data.get("flags") or {}).items()}
        self.identity = {int(a): int(v) for a, v in (data.get("identity") or {}).items()}
        limits = data.get("limits") or {}
        self.max_voltage = float(limits.get("voltage", 32.0))
        self.max_current = float(limits.get("current", 10.0))
        link = data.get("link") or {}
        self.baud = int(link.get("baud", 9600))
        self.slave = int(link.get("slave", 1))

    @classmethod
    def load(cls, path: str | Path = DEFAULT_MAP) -> "RegisterMap":
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f))

    def __getitem__(self, name: str) -> Reg:
        return self.regs[name]

    def addresses(self) -> list[int]:
        """All register addresses, 32-bit registers expanded to both words."""
        return sorted(a for r in self.regs.values() for a in range(r.addr, r.addr + r.words))

    def by_address(self) -> dict[int, Reg]:
        """Address -> register (both words of a 32-bit register map to it)."""
        return {a: r for r in self.regs.values() for a in range(r.addr, r.addr + r.words)}

    def scale(self, name: str, raw: int) -> float:
        d = self.regs[name].decimals
        return raw / 10**d if d is not None else float(raw)


def assess(output: bool, cc_mode: bool, ocp_tripped: bool, v: float, i: float, v_set: float) -> list[str]:
    """Warnings for the state right after switching the output on."""
    if ocp_tripped:
        return ["OCP tripped - the device switched the output off. Short circuit, reversed polarity "
                "or current limit too low?"]
    if not output:
        return ["Output is not (or no longer) on."]
    if cc_mode:
        return [f"CC mode right after switching on: current is at the limit ({i:.3f} A at {v:.2f} V instead "
                f"of {v_set:.2f} V) - short circuit or reversed polarity?"]
    return []


class SkyPSU:
    def __init__(self, port: str | None = None, baud: int | None = None, slave: int | None = None,
                 regmap: RegisterMap | None = None, timeout: float = modbus.DEFAULT_TIMEOUT):
        self.map = regmap or RegisterMap.load()
        self.port = port
        self.baud = baud or self.map.baud
        self.slave = slave or self.map.slave
        self.timeout = timeout
        self.client: modbus.RtuClient | None = None
        self.link: modbus.Link | None = None
        self._identity_ok = False

    # ------------------------------------------------------------------ connection

    def connect(self) -> modbus.Link:
        """Open the connection. Without a port all ports are searched; only a device whose identity
        registers match is accepted."""
        self.close()
        ports = [self.port] if self.port else None
        bauds = [self.baud] + [b for b in modbus.DEFAULT_BAUDS if b != self.baud]
        hit = modbus.find_first(ports=ports, bauds=bauds, slaves=[self.slave], timeout=self.timeout,
                                accept=self.check_identity)
        if not hit:
            raise PsuError(f"no power supply with matching identity found (ports: {', '.join(ports) if ports else 'all'})")
        self.link = hit[0]
        self.client = modbus.open_client(self.link, self.timeout)
        self.ensure_identity()
        return self.link

    def close(self) -> None:
        if self.client:
            try:
                self.client.close()
            except (serial.SerialException, OSError):
                pass
        self.client = None
        self._identity_ok = False

    def __enter__(self) -> "SkyPSU":
        if not self.client:
            self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _c(self) -> modbus.RtuClient:
        if not self.client:
            raise PsuError("not connected")
        return self.client

    def check_identity(self, client: modbus.RtuClient) -> str | None:
        """None if the identity registers match, otherwise a description of the mismatch."""
        for addr, want in self.map.identity.items():
            try:
                got = client.read_register(addr)
            except modbus.ModbusError as exc:
                return f"0x{addr:04X}: {exc}"
            if got != want:
                return f"0x{addr:04X} = {got}, expected {want}"
        return None

    def ensure_identity(self) -> None:
        if self._identity_ok:
            return
        problem = self.check_identity(self._c())
        if problem:
            raise IdentityError(f"device identity does not match ({problem}) - write refused")
        self._identity_ok = True

    # ------------------------------------------------------------------ reading

    def read_raw(self, *names: str) -> dict[str, int]:
        """Read several registers; contiguous addresses are read as blocks (max. 4)."""
        regs = [self.map[n] for n in names]
        res = modbus.read_addresses(self._c(), [a for r in regs for a in range(r.addr, r.addr + r.words)])
        bad = next((r for r in res.values() if not r.ok), None)
        if bad:
            raise PsuError(f"read error 0x{bad.address:04X}: {bad.detail}")
        out = {}
        for r in regs:
            val = 0
            for a in range(r.addr, r.addr + r.words):
                val = (val << 16) | res[a].value
            out[r.name] = val
        return out

    def read(self, *names: str) -> dict[str, float]:
        return {n: self.map.scale(n, v) for n, v in self.read_raw(*names).items()}

    def _flag(self, flags: int, name: str) -> bool:
        return bool(flags & self.map.flags[name])

    def identify(self) -> dict:
        nominal = self.read("max_voltage", "max_current", "max_power")
        return {
            "model": "SKY TOPPOWER STP2S3010E22",
            "port": self.link.port if self.link else None,
            "port_info": self.link.port_info if self.link else None,
            "baud": self.link.baud if self.link else None,
            "slave": self.slave,
            "identity_ok": self.check_identity(self._c()) is None,
            "rated": {"voltage": nominal["max_voltage"], "current": nominal["max_current"],
                      "power": nominal["max_power"]},
        }

    def measure(self) -> tuple[float, float, float]:
        m = self.read("voltage", "current", "power")
        return m["voltage"], m["current"], m["power"]

    def setpoints(self) -> tuple[float, float]:
        s = self.read("set_voltage", "set_current")
        return s["set_voltage"], s["set_current"]

    def protection_status(self) -> dict:
        raw = self.read_raw("status_flags", "ovp_enable", "ocp_enable", "opp_enable", "key_lock")
        flags = raw["status_flags"]
        return {"ocp_tripped": self._flag(flags, "ocp_tripped"), "cc_mode": self._flag(flags, "cc_mode"),
                "ovp_enabled": bool(raw["ovp_enable"]), "ocp_enabled": bool(raw["ocp_enable"]),
                "opp_enabled": bool(raw["opp_enable"]), "key_lock": bool(raw["key_lock"]), "flags_raw": flags}

    def status(self) -> dict:
        """Complete state in a few block requests (~200 ms)."""
        raw = self.read_raw("output", "status_flags", "temperature", "voltage", "current", "power",
                            "set_voltage", "set_current", "ovp", "opp",
                            "key_lock", "ovp_enable", "ocp_enable", "opp_enable")
        flags = raw["status_flags"]
        s = lambda n: self.map.scale(n, raw[n])  # noqa: E731
        # While powering up the device briefly returns garbage with a valid CRC -> discard
        if (raw["output"] > 1 or s("set_voltage") > self.map.max_voltage * 1.1 or s("voltage") > 40
                or s("set_current") > 11 or s("current") > 11):
            raise PsuError("implausible values (device powering up?) - discarded")
        return {
            "output": bool(raw["output"]),
            "voltage": s("voltage"), "current": s("current"), "power": s("power"),
            "set_voltage": s("set_voltage"), "set_current": s("set_current"),
            "mode": "CC" if self._flag(flags, "cc_mode") else "CV",
            "temperature": raw["temperature"],
            "ocp_tripped": self._flag(flags, "ocp_tripped"),
            "key_lock": bool(raw["key_lock"]),
            "power_on_output": self._flag(flags, "power_on_output"),
            "protection": {
                "ovp_enabled": bool(raw["ovp_enable"]), "ovp": s("ovp"),
                "ocp_enabled": bool(raw["ocp_enable"]),  # OCP threshold = current setpoint
                "opp_enabled": bool(raw["opp_enable"]), "opp": s("opp"),
            },
            "flags_raw": flags,
        }

    # ------------------------------------------------------------------ writing

    def _write_verified(self, name: str, raw: int) -> None:
        reg = self.map[name]
        if "w" not in reg.access or reg.words != 1:
            raise PsuError(f"register {name} is not configured as a writable single register")
        self.ensure_identity()
        self._c().write_register(reg.addr, raw)
        deadline = time.monotonic() + VERIFY_TIMEOUT
        while True:
            back = self._c().read_register(reg.addr)
            if back == raw:
                return
            if time.monotonic() >= deadline:
                raise VerifyError(f"{name}: wrote {raw}, read back {back} after {VERIFY_TIMEOUT:.1f} s")
            time.sleep(0.05)

    def set(self, volt: float | None = None, curr: float | None = None) -> tuple[float, float]:
        """Set voltage and/or current limit without switching the output. Returns the read-back setpoints."""
        if volt is not None and not 0 <= volt <= self.map.max_voltage:
            raise LimitError(f"voltage {volt} V outside 0-{self.map.max_voltage} V")
        if curr is not None and not 0 <= curr <= self.map.max_current:
            raise LimitError(f"current {curr} A outside 0-{self.map.max_current} A")
        if volt is not None:
            self._write_verified("set_voltage", round(volt * 10 ** self.map["set_voltage"].decimals))
        if curr is not None:
            self._write_verified("set_current", round(curr * 10 ** self.map["set_current"].decimals))
        return self.setpoints()

    def output_on(self, settle: float | None = 1.0) -> dict:
        """Put voltage on the terminals. With ``settle`` the state is measured and assessed afterwards."""
        self._write_verified("output", 1)
        if settle is None:
            return {"output": True, "warnings": []}
        time.sleep(settle)
        st = self.status()
        st["warnings"] = assess(st["output"], st["mode"] == "CC", st["ocp_tripped"],
                                st["voltage"], st["current"], st["set_voltage"])
        return st

    def output_off(self) -> None:
        """Switch the output off; setpoints are kept."""
        self._write_verified("output", 0)

    def set_protection(self, ovp: float | None = None, ocp: float | None = None, opp: float | None = None):
        raise PsuError("writing protection settings is not enabled: the OVP/OPP registers are known but writing "
                       "them is untested, OCP always follows the current limit and OPP is fixed")
