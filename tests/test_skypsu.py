"""Tests against a simulated STP2S3010E22 behind a fake serial port (no hardware needed).

Run: python -m pytest
"""

import struct

import pytest

from skypsu import modbus
from skypsu.psu import IdentityError, LimitError, SkyPSU

REGS = {
    0x0001: 0, 0x0002: 0x02, 0x0003: 25, 0x0005: 0x0233,
    0x0010: 1200, 0x0011: 150, 0x0012: 0, 0x0013: 1800,
    0x0014: 3200, 0x0015: 10200, 0x0016: 4, 0x0017: 64256,
    0x0020: 3840, 0x0021: 1000, 0x0022: 4, 0x0023: 64256,
    0x0030: 1200, 0x0031: 1000,
    0x0040: 0, 0x0041: 1, 0x0042: 1, 0x0043: 1, 0x0044: 1,
}


class FakeDevice:
    """Serial-port stand-in that behaves like the power supply on the Modbus level."""

    def __init__(self, regs=None, slave=1, max_block=4, off_delay_reads=0):
        self.regs = dict(REGS if regs is None else regs)
        self.slave = slave
        self.max_block = max_block
        self.off_delay_reads = off_delay_reads  # emulates OFF showing up late in register 0x0001
        self._pending_off = 0
        self.baudrate = 9600
        self.timeout = 0.05
        self.is_open = True
        self.buf = b""
        self.writes: list[tuple[int, int]] = []

    def close(self):
        self.is_open = False

    def reset_input_buffer(self):
        self.buf = b""

    def _reply(self, body: bytes) -> None:
        self.buf = body + modbus.crc16(body)

    def write(self, req: bytes) -> int:
        assert modbus.crc16(req[:-2]) == req[-2:]
        slave, fc = req[0], req[1]
        if slave != self.slave:
            return len(req)
        addr, val = struct.unpack(">HH", req[2:6])
        if fc in (3, 4):
            if val > self.max_block:
                self._reply(bytes([slave, fc | 0x80, 3]))
            elif addr not in self.regs:  # like the real device: only the start address is validated
                self._reply(bytes([slave, fc | 0x80, 2]))
            else:
                values = []
                for a in range(addr, addr + val):
                    v = self.regs.get(a, 0xFB00)
                    if a == 0x0001 and self._pending_off:
                        self._pending_off -= 1
                        v = 1
                    values.append(v)
                self._reply(bytes([slave, fc, 2 * val]) + struct.pack(f">{val}H", *values))
        elif fc == 6:
            self.writes.append((addr, val))
            self.regs[addr] = val
            if addr == 0x0001 and val == 0:
                self._pending_off = self.off_delay_reads
            self._reply(req[:6])
        return len(req)

    def read(self, n: int) -> bytes:
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


@pytest.fixture
def device(monkeypatch):
    dev = FakeDevice()
    monkeypatch.setattr(modbus, "open_serial", lambda port, baud, timeout=0.2: dev)
    monkeypatch.setattr(modbus, "list_serial_ports", lambda: [])
    return dev


def test_crc16_known_value():
    # read 1 register at 0x0003 from slave 1
    assert modbus.crc16(bytes.fromhex("010300030001")) == bytes.fromhex("740a")


def test_exception_response_is_reported(device):
    client = modbus.RtuClient(device, 1)
    with pytest.raises(modbus.SlaveException) as exc:
        client.read_register(0x0050)
    assert exc.value.code == 2


def test_read_range_falls_back_to_single_reads(device):
    client = modbus.RtuClient(device, 1)
    res = modbus.read_range(client, 0x0000, 8, block_size=4)
    ok = {r.address: r.value for r in res if r.ok}
    assert ok == {a: REGS[a] for a in range(0x0000, 0x0008) if a in REGS}


def test_block_larger_than_device_limit_falls_back(device):
    client = modbus.RtuClient(device, 1)
    res = modbus.read_range(client, 0x0010, 8, block_size=8)
    assert [r.value for r in res] == [REGS[a] for a in range(0x0010, 0x0018)]


def test_status_scales_values(device):
    with SkyPSU(port="SIM") as psu:
        st = psu.status()
    assert st["voltage"] == 12.0 and st["current"] == 0.15 and st["power"] == 1.8
    assert st["set_current"] == 1.0 and st["mode"] == "CV" and st["protection"]["ovp"] == 38.4


def test_set_writes_and_verifies(device):
    with SkyPSU(port="SIM") as psu:
        assert psu.set(volt=5.0, curr=0.2) == (5.0, 0.2)
    assert device.writes == [(0x0030, 500), (0x0031, 200)]


def test_limits_are_checked_before_writing(device):
    with SkyPSU(port="SIM") as psu, pytest.raises(LimitError):
        psu.set(volt=40)
    assert device.writes == []


def test_wrong_identity_refuses_connection(device):
    device.regs[0x0015] = 5000  # a different model
    with pytest.raises(Exception):
        SkyPSU(port="SIM").connect()


def test_identity_is_checked_before_first_write(device):
    psu = SkyPSU(port="SIM")
    psu.connect()
    psu._identity_ok = False
    device.regs[0x0014] = 0
    with pytest.raises(IdentityError):
        psu.output_on(settle=None)
    assert device.writes == []


def test_output_off_waits_for_delayed_register(device):
    device.off_delay_reads = 3
    device.regs[0x0001] = 1
    with SkyPSU(port="SIM") as psu:
        psu.output_off()
    assert device.regs[0x0001] == 0


def test_garbage_while_powering_up_is_discarded(device):
    device.regs[0x0030] = 64256
    with SkyPSU(port="SIM") as psu, pytest.raises(Exception, match="implausible"):
        psu.status()
