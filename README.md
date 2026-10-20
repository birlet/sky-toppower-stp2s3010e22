# skypsu – Python control for the SKY TOPPOWER STP2S3010E22

Unofficial, reverse-engineered Modbus RTU tools for the **SKY TOPPOWER STP2S3010E22** programmable lab
power supply. Works on Windows and Linux; the vendor software is Windows only.

- **Register map** of the device, verified on two units ([docs/PROTOCOL.md](docs/PROTOCOL.md))
- **Python library** with guarded writes (identity check, limits, read-back)
- **Web GUI** with live chart, output switch and setpoints
- **Register scanner** to explore and verify the register layout
- **Live plot** with matplotlib

> **Safety:** Software is no substitute for a physical emergency stop. Keep the output key or the mains
> switch within reach whenever voltage is on the terminals. This project is not affiliated with
> SKY TOPPOWER; use at your own risk.

## The device

| | |
|---|---|
| Model | SKY TOPPOWER **STP2S3010E22**, OEM name **E22-3010W** (E22-xxxx series) |
| Output | 0–32 V, 0–10 A, 320 W (rated values reported by the device: 32.00 V / 10.200 A / 326.4 W) |
| Display | 4 digits each for voltage, current and power; CV / CC / lock indicators |
| Resolution over Modbus | 10 mV, 1 mA, 1 mW |
| Protection | OVP, OCP, OPP, OTP; panel lock; configurable output state at power-up |
| Interface | front USB-C port labelled **COM** (data only, 4-wire cable needed); the **QC** port is an 18 W charger without data |
| USB bridge | WCH **CH340** (`1A86:7523`), shows up as a COM port / `/dev/ttyUSB*`; no USB serial number |
| Protocol | **Modbus RTU**, 8N1, default 9600 baud, slave address 1 (menu `bps` / `Addr`) |

The register layout is not public (the vendor refers to a separate "Communication Protocol ModBusVxx"
document). It resembles the Hanmatek HM310P / eTOMMENS platform but differs in several places
(no model register, different status bits, max. 4 registers per read). Full details, quirks and the
front-panel menu are in **[docs/PROTOCOL.md](docs/PROTOCOL.md)**.

### Key registers

| Address | Meaning | Scale |
|---|---|---|
| 0x0001 | output on/off (RW) | 0/1 |
| 0x0002 | status flags: lock 0x01, OCP tripped 0x08, CC mode 0x40, output-at-power-up 0x80 | bits |
| 0x0010 / 0x0011 | measured voltage / current | /100 V, /1000 A |
| 0x0012–13 | measured power, 32 bit | /1000 W |
| 0x0030 / 0x0031 | voltage setpoint / current limit (RW) | /100 V, /1000 A |
| 0x0020 | OVP threshold | /100 V |
| 0x0042 / 0x0043 / 0x0044 | OVP / OCP / OPP enable | 0/1 |

## Installation

Python 3.10 or newer.

```bash
git clone <this repository>
cd <repository>
python -m pip install -e .            # or: python -m pip install -r requirements.txt
python -m pip install -e ".[plot]"    # optional, for the matplotlib live plot
```

Linux: the user needs access to `/dev/ttyUSB*` (`sudo usermod -aG dialout $USER`, then log in again).

### Connecting the device

1. Plug a **data** USB-C cable into the front port labelled **COM** (not QC).
2. A new serial port should appear: `python -m serial.tools.list_ports -v` (CH340, `1A86:7523`).
3. Default settings are 9600 baud, address 1. They can be checked in the menu (hold the output key 3 s).
4. Close the vendor software or anything else that holds the port.

## Web GUI

```bash
python -m skypsu.gui                     # finds the device, opens http://127.0.0.1:8765/
python -m skypsu.gui --port COM8 --no-browser
```

- Live chart of current and voltage (measured and set) over 30 s / 2 min / 10 min, with crosshair tooltip
- Large output switch; **Esc switches the output off** at any time
- Setpoints for voltage and current (applying them never switches the output)
- Warning right after switching on when OCP trips or the device goes into CC mode
  (short circuit or reversed polarity?)
- Warning when the device is configured to switch its output on at power-up
- Reconnects automatically after unplugging and only accepts a device with matching identity
- Listens on 127.0.0.1 only; requests from other sites are rejected (host check, JSON-only POST)

The server holds the serial port while it runs. Ctrl+C stops it; the output state is left unchanged.

## Library

```python
from skypsu import SkyPSU

with SkyPSU() as psu:                  # or SkyPSU(port="COM8")
    print(psu.identify())              # port, rated values, identity check
    psu.set(volt=5.0, curr=0.2)        # limits checked, written, read back
    result = psu.output_on()           # waits 1 s, then measures and assesses
    print(result["warnings"])          # e.g. OCP tripped / CC mode right after switching on
    print(psu.measure())               # (volts, amps, watts)
    print(psu.status())                # full state incl. mode, protection, temperature
    psu.output_off()
```

Guards on every write:

- **Identity check** before the first write of a connection: 0x0005 = 0x0233, 0x0014 = 3200,
  0x0015 = 10200. Another device on the port is refused.
- **Limits** 0–32 V and 0–10 A (`limits` in `register_map.yaml`).
- **Read-back** after every write, waiting up to 1 s (the device reflects OFF with a delay).
- `status()` discards implausible values, which the device returns briefly while powering up.

Writing protection settings (`set_protection`) is deliberately disabled: the registers are known, but
writing them has not been tested.

## Register scanner

Read only; never writes.

```bash
python -m skypsu.scan find --addr 1-5           # search all ports, baud rates and addresses
python -m skypsu.scan ident --port COM8         # read every register of the map, check identity
python -m skypsu.scan scan --port COM8          # scan address ranges, save CSV + JSON
python -m skypsu.scan watch --port COM8         # poll and highlight changes while you operate the device
python -m skypsu.scan read --regs 0x0010-0x0013,0x0030
```

Results go to `scan_results/`. `watch` is how the register map was built: change one setting on the
device at a time and note which register changes.

## Live plot (matplotlib)

```bash
python -m skypsu.plot --port COM8 --window 300 --csv log.csv
```

## Tests

The tests run against a simulated device and need no hardware:

```bash
python -m pip install -e ".[test]"
python -m pytest
```

## Project layout

```
skypsu/
  modbus.py           minimal Modbus RTU master, port discovery, robust reads
  psu.py              SkyPSU driver with guarded writes
  register_map.yaml   register map, identity and limits (single source of truth)
  scan.py             read-only scanner (find / ident / read / scan / watch)
  gui.py, gui.html    local web GUI
  plot.py             matplotlib live plot
docs/PROTOCOL.md      register map, device quirks, menu
tests/                tests against a simulated device
```

## Open points

- Status bits for OVP and OTP trips are unknown (according to the manual these need a power cycle).
- Writing OVP threshold, protection enables, panel lock and slave address is untested.
- Meaning of 0x0000, 0x0004, 0x0006, 0x0041, 0x00F1, 0x00F2 is unknown.
- During a power cycle of the device, `scan watch` once stopped printing without an error message;
  the cause is not yet known.
