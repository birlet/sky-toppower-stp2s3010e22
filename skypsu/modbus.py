"""Minimal Modbus RTU master on top of pyserial, plus port discovery and robust read helpers.

Why not minimalmodbus: on an exception response (5 bytes) minimalmodbus keeps waiting for the
full expected frame length, i.e. it always runs into the timeout. RtuClient reads the 3-byte
header first and then knows how many bytes are left. Measured on the device: ~25 ms per request.

The helpers in this module only read (FC03/FC04). Writing (FC06/FC10) is offered by RtuClient;
the scanner never calls it, only skypsu.psu does.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

import serial
from serial.tools import list_ports

DEFAULT_BAUDS: tuple[int, ...] = (9600, 19200, 38400, 57600, 115200)
DEFAULT_TIMEOUT = 0.2  # seconds; the device answers after ~25 ms

# Registers used to probe for a device. Any answer with a valid CRC counts, including a Modbus
# exception ("illegal data address"): a slave is answering on that port/baud/address.
PROBE_REGISTERS: tuple[int, ...] = (0x0003, 0x0000)

MAX_REGISTERS_PER_READ = 125  # Modbus limit for FC03/FC04
DEFAULT_BLOCK_SIZE = 4  # STP2S3010E22 answers exception 3 (illegal data value) for 5+ registers


# --------------------------------------------------------------------------- RTU client


class ModbusError(OSError):
    """Base class for Modbus-level errors (not for port errors)."""


class NoResponseError(ModbusError):
    pass


class InvalidResponseError(ModbusError):
    pass


class SlaveException(ModbusError):
    """The device answered with an exception response (valid CRC)."""

    TEXT = {1: "illegal function", 2: "illegal data address", 3: "illegal data value",
            4: "slave device failure", 6: "slave device busy"}

    def __init__(self, code: int):
        self.code = code
        super().__init__(f"exception {code}: {self.TEXT.get(code, 'unknown')}")


def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


class RtuClient:
    """Minimal Modbus RTU master for one slave on an open serial port."""

    def __init__(self, ser: serial.Serial, slave: int):
        self.serial = ser
        self.slave = slave
        self._last_rx = 0.0

    def close(self) -> None:
        self.serial.close()

    def _transact(self, pdu: bytes) -> bytes:
        """Send a PDU and return the response PDU (without address and CRC)."""
        # Modbus requires 3.5 characters of silence between frames (~4 ms at 9600 baud)
        gap = max(0.002, 3.5 * 11 / self.serial.baudrate)
        wait = self._last_rx + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        adu = bytes([self.slave]) + pdu
        self.serial.reset_input_buffer()
        self.serial.write(adu + crc16(adu))
        try:
            head = self.serial.read(3)
            if len(head) < 3:
                if head:
                    raise InvalidResponseError(f"incomplete response: {head.hex(' ')}")
                raise NoResponseError("no response")
            fc = head[1]
            if fc & 0x80:
                rest_len = 2  # exception: code is head[2], only the CRC follows
            elif fc in (3, 4):
                rest_len = head[2] + 2  # byte count + CRC
            else:
                rest_len = 5  # FC06/FC10: 4 data bytes (one already in head) + CRC
            rest = self.serial.read(rest_len)
        finally:
            self._last_rx = time.monotonic()
        frame = head + rest
        if len(rest) < rest_len:
            raise InvalidResponseError(f"incomplete response: {frame.hex(' ')}")
        if crc16(frame[:-2]) != frame[-2:]:
            raise InvalidResponseError(f"CRC error: {frame.hex(' ')}")
        if frame[0] != self.slave:
            raise InvalidResponseError(f"response from slave {frame[0]} instead of {self.slave}")
        if fc & 0x80:
            raise SlaveException(frame[2])
        if fc != pdu[0]:
            raise InvalidResponseError(f"function code {fc} instead of {pdu[0]}")
        return frame[1:-2]

    def read_registers(self, address: int, count: int, functioncode: int = 3) -> list[int]:
        resp = self._transact(struct.pack(">BHH", functioncode, address, count))
        if resp[1] != 2 * count:
            raise InvalidResponseError(f"{resp[1]} data bytes instead of {2 * count}")
        return list(struct.unpack(f">{count}H", resp[2:]))

    def read_register(self, address: int, functioncode: int = 3) -> int:
        return self.read_registers(address, 1, functioncode)[0]

    def write_register(self, address: int, value: int) -> None:
        """FC06. The device must echo the request unchanged."""
        pdu = struct.pack(">BHH", 6, address, value)
        resp = self._transact(pdu)
        if resp != pdu:
            raise InvalidResponseError(f"FC06 echo differs: {resp.hex(' ')}")

    def write_registers(self, address: int, values: list[int]) -> None:
        """FC10 (multiple registers)."""
        pdu = struct.pack(f">BHHB{len(values)}H", 0x10, address, len(values), 2 * len(values), *values)
        resp = self._transact(pdu)
        if resp != pdu[:5]:
            raise InvalidResponseError(f"FC10 response differs: {resp.hex(' ')}")


# --------------------------------------------------------------------------- Ports


@dataclass(frozen=True)
class Link:
    """Connection parameters of a found device."""

    port: str
    baud: int
    slave: int
    port_info: str = ""

    def __str__(self) -> str:
        info = f" ({self.port_info})" if self.port_info else ""
        return f"{self.port}{info} @ {self.baud} baud 8N1, slave {self.slave}"


def list_serial_ports() -> list:
    """All serial ports, USB devices (with VID:PID) first."""
    ports = list(list_ports.comports())
    ports.sort(key=lambda p: (p.vid is None, p.device))
    return ports


def describe_port(p) -> str:
    parts = []
    if p.vid is not None:
        parts.append(f"{p.vid:04X}:{p.pid:04X}")
    if p.description and p.description != "n/a":
        parts.append(p.description)
    return ", ".join(parts)


def open_serial(port: str, baud: int, timeout: float = DEFAULT_TIMEOUT) -> serial.Serial:
    """Open the port with fixed 8N1 framing."""
    return serial.Serial(port=port, baudrate=baud, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                         stopbits=serial.STOPBITS_ONE, timeout=timeout, write_timeout=1.0)


def open_client(link: Link, timeout: float = DEFAULT_TIMEOUT) -> RtuClient:
    """Open a connection with known parameters. Close with ``client.close()``."""
    return RtuClient(open_serial(link.port, link.baud, timeout), link.slave)


# --------------------------------------------------------------------------- Reading


@dataclass
class ReadResult:
    """Result of reading one register."""

    address: int
    value: int | None
    status: str  # "ok" | "exception" | "timeout" | "invalid"
    detail: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _classify(exc: ModbusError) -> tuple[str, str]:
    if isinstance(exc, NoResponseError):
        return "timeout", "no response"
    if isinstance(exc, SlaveException):
        return "exception", str(exc)
    return "invalid", str(exc)


def read_single(client: RtuClient, address: int, functioncode: int = 3) -> ReadResult:
    """Read one register. Modbus-level errors are returned as status.

    serial.SerialException (port gone, e.g. cable unplugged) is passed on.
    """
    try:
        return ReadResult(address, client.read_register(address, functioncode), "ok")
    except ModbusError as exc:
        status, detail = _classify(exc)
        return ReadResult(address, None, status, detail)


def read_range(
    client: RtuClient,
    start: int,
    count: int,
    functioncode: int = 3,
    block_size: int = DEFAULT_BLOCK_SIZE,
    on_block: Callable[[int, int, list[ReadResult]], None] | None = None,
) -> list[ReadResult]:
    """Read ``count`` registers starting at ``start`` in blocks.

    If a block fails (exception, timeout, invalid response), its registers are read one by one so
    readable registers next to missing ones are still found.
    ``on_block(block_start, n, results)`` is called after every block (progress).

    Caution, STP2S3010E22: the device only validates the start address. A block that extends past
    the end of real registers returns phantom values there. Scan unknown ranges with block_size=1.
    """
    block_size = max(1, min(block_size, MAX_REGISTERS_PER_READ))
    end = start + count
    if start < 0 or end > 0x10000:
        raise ValueError(f"range 0x{start:04X}+{count} is outside 0x0000-0xFFFF")

    results: list[ReadResult] = []
    for block_start in range(start, end, block_size):
        n = min(block_size, end - block_start)
        try:
            values = client.read_registers(block_start, n, functioncode)
            ts = time.time()
            block = [ReadResult(block_start + i, v, "ok", timestamp=ts) for i, v in enumerate(values)]
        except ModbusError:
            block = [read_single(client, a, functioncode) for a in range(block_start, block_start + n)]
        results.extend(block)
        if on_block:
            on_block(block_start, n, block)
    return results


def contiguous_runs(addresses: Iterable[int]) -> list[tuple[int, int]]:
    """[1, 2, 3, 7, 8] -> [(1, 3), (7, 2)]"""
    runs: list[tuple[int, int]] = []
    for a in sorted(set(addresses)):
        if runs and runs[-1][0] + runs[-1][1] == a:
            runs[-1] = (runs[-1][0], runs[-1][1] + 1)
        else:
            runs.append((a, 1))
    return runs


def read_addresses(
    client: RtuClient,
    addresses: Iterable[int],
    functioncode: int = 3,
    block_size: int = DEFAULT_BLOCK_SIZE,
) -> dict[int, ReadResult]:
    """Read arbitrary addresses; contiguous addresses are read as blocks."""
    result: dict[int, ReadResult] = {}
    for start, count in contiguous_runs(addresses):
        for r in read_range(client, start, count, functioncode, block_size):
            result[r.address] = r
    return result


# --------------------------------------------------------------------------- Discovery


def probe(client: RtuClient) -> tuple[bool, str]:
    """Does a slave answer with a valid CRC using the current parameters?"""
    last = "no response"
    for reg in PROBE_REGISTERS:
        r = read_single(client, reg)
        if r.ok:
            return True, f"FC03 0x{reg:04X} = {r.value} (0x{r.value:04X})"
        if r.status == "exception":
            return True, f"FC03 0x{reg:04X} -> exception response with valid CRC ({r.detail})"
        if r.status == "invalid":
            last = "data received but CRC/format invalid (wrong baud rate or noise?)"
    return False, last


def discover(
    ports: Iterable[str] | None = None,
    bauds: Iterable[int] = DEFAULT_BAUDS,
    slaves: Iterable[int] = (1,),
    timeout: float = DEFAULT_TIMEOUT,
    log: Callable[[str], None] | None = None,
    accept: Callable[[RtuClient], str | None] | None = None,
) -> Iterator[tuple[Link, str]]:
    """Try port x baud rate x slave address and yield every hit as (Link, detail).

    Each port is opened once and the baud rate is switched. The port stays open while the
    generator runs; to open it yourself afterwards, stop the generator first (break / ``.close()``).
    ``accept(client)`` can check a hit (e.g. the device identity): None = accepted, otherwise a
    reason that is logged.
    """
    log = log or (lambda _msg: None)
    infos = {p.device: describe_port(p) for p in list_serial_ports()}
    port_list = list(ports) if ports else list(infos)
    if not port_list:
        log("No serial ports found.")
        return
    bauds = list(bauds)
    slaves = list(slaves)

    for port in port_list:
        info = infos.get(port, "")
        log(f"Port {port}" + (f" ({info})" if info else ""))
        try:
            ser = open_serial(port, bauds[0], timeout)
        except (serial.SerialException, OSError) as exc:
            log(f"  cannot open: {exc}")
            continue
        try:
            for baud in bauds:
                ser.baudrate = baud
                for slave in slaves:
                    client = RtuClient(ser, slave)
                    try:
                        found, detail = probe(client)
                    except (serial.SerialException, OSError) as exc:
                        log(f"  {baud:>6} baud, slave {slave}: port error: {exc}")
                        break
                    if found and accept:
                        reason = accept(client)
                        if reason:
                            found, detail = False, f"answers, but identity does not match ({reason})"
                    log(f"  {baud:>6} baud, slave {slave:>3}: {'FOUND - ' if found else ''}{detail}")
                    if found:
                        yield Link(port, baud, slave, info), detail
        finally:
            ser.close()


def find_first(**kwargs) -> tuple[Link, str] | None:
    """First hit of discover(); the port is released again before returning."""
    for hit in discover(**kwargs):
        return hit
    return None
