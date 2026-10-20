# STP2S3010E22 Modbus RTU protocol (reverse engineered)

The vendor documents the register layout in a separate document ("Digital Power Supply Communication
Protocol ModBusVxx") that is not publicly available. Everything below was found by scanning the
register space and operating the device while watching the registers change
(`python -m skypsu.scan watch`). Two units of the same model were tested and behaved identically.

The machine-readable version of this table is [`skypsu/register_map.yaml`](../skypsu/register_map.yaml);
the code reads it from there.

## Link layer

| Item | Value |
|---|---|
| Physical | USB-C port labelled **COM** (front panel). The **QC** port is a charger only. Needs a 4-wire data cable. |
| USB bridge | WCH CH340, VID:PID `1A86:7523`, **no USB serial number** |
| Framing | 8N1 |
| Baud rate | 9600 (factory default, menu `bps`) |
| Slave address | 1 (factory default, menu `Addr`, 1–247) |
| Function codes | FC03 read holding registers, FC06 write single register |
| Response time | ~25–30 ms per request |

## Device quirks

- **Max. 4 registers per read.** Requests for 5 or more registers are answered with exception 3
  (illegal data value).
- **Only the start address is validated.** A block read that starts on a valid register but runs past
  the end of real registers returns *phantom values* for the non-existent ones (they even change from
  read to read). Scan unknown ranges one register at a time; read blocks only over known registers.
- **Output OFF is reflected with a delay.** After writing `0` to 0x0001 the register can still read `1`
  for up to ~0.4 s. Switching ON is reflected immediately. Read-back verification must wait.
- **Garbage while powering up.** For about one poll cycle after power-up the device returns nonsense with
  a valid CRC (e.g. 0x0030 = 64256, 0x0015 = 0). Values must be checked for plausibility.
- **Opening the menu switches the output off.** While the menu item `Addr` is shown the device does not
  answer for ~3 s.
- **Menu `reset` resets the setpoints** (to 2.00 V / 0.200 A on the tested unit) but keeps protection
  settings. Never assume setpoints are unchanged; read them before switching the output on.
- **OCP trips instead of limiting.** With OCP enabled (0x0043 = 1) the device switches the output off as
  soon as the current reaches the current limit; with OCP disabled it goes into CC mode.
- Both units report the same identity registers; two devices **cannot be told apart** over Modbus or USB.
  Give them different Modbus addresses to run them in parallel.

## Registers

`R` = read, `RW` = read/write. "Write verified" means an FC06 write was tested on the device.

| Address | Name | Access | Scale | Meaning | Status |
|---|---|---|---|---|---|
| 0x0000 | – | R | | always 0 | unknown |
| 0x0001 | output | RW | 0/1 | output on/off. **Write verified** | confirmed |
| 0x0002 | status flags | R | bits | see below | confirmed |
| 0x0003 | temperature | R | °C | internal temperature (23 → 31 °C under load) | likely |
| 0x0004 | – | R | | 134–135, changes slightly under load | unknown |
| 0x0005 | decimals | R | | `0x0233` = V 2, A 3, W 3 decimals | likely |
| 0x0006 | – | R | | usually 4 | unknown |
| 0x0010 | voltage | R | V, /100 | measured output voltage | confirmed |
| 0x0011 | current | R | A, /1000 | measured output current | confirmed |
| 0x0012–13 | power | R | W, /1000, 32 bit | measured output power (high word first) | confirmed |
| 0x0014 | rated voltage | R | V, /100 | 3200 = 32.00 V | likely |
| 0x0015 | rated current | R | A, /1000 | 10200 = 10.200 A | likely |
| 0x0016–17 | rated power | R | W, /1000, 32 bit | 326400 = 326.4 W | likely |
| 0x0020 | OVP threshold | RW | V, /100 | menu `ouP` value (writing untested) | confirmed |
| 0x0021 | OCP threshold | R | A, /1000 | always equal to the current limit 0x0031 | confirmed |
| 0x0022–23 | OPP threshold | R | W, /1000, 32 bit | 326.4 W, not adjustable in the menu | likely |
| 0x0030 | voltage setpoint | RW | V, /100 | **Write verified** | confirmed |
| 0x0031 | current limit | RW | A, /1000 | **Write verified** | confirmed |
| 0x0040 | panel lock | RW | 0/1 | 1 = locked (writing untested) | confirmed |
| 0x0041 | – | R | | always 1, probably menu item `o1` | unknown |
| 0x0042 | OVP enable | RW | 0/1 | menu `ouP` yes/no (writing untested) | confirmed |
| 0x0043 | OCP enable | RW | 0/1 | menu `ocp` on/off (writing untested) | confirmed |
| 0x0044 | OPP enable | RW | 0/1 | menu `Opp` on/off (writing untested) | likely |
| 0x00F0 | power cycles | R | | increments on every power-up and menu reset | likely |
| 0x00F1 | – | R | | always 0 | unknown |
| 0x00F2 | – | R | | 1 after a menu reset, 0 after the next power cycle | unknown |
| 0x9999 | slave address | RW | | menu `Addr` (writing untested) | likely |

Not present (exception 2, illegal data address): 0x1000–0x10FF, 0x8800–0x88FF (incl. 0x8804, the buzzer
register of the similar Hanmatek HM310P), 0x9990–0x99FF except 0x9999, 0xC100–0xC1FF.

### Status flags (0x0002)

| Bit | Mask | Meaning | Status |
|---|---|---|---|
| 0 | 0x01 | panel locked (mirrors 0x0040) | confirmed |
| 1 | 0x02 | buzzer enabled (menu `beep`) | likely |
| 3 | 0x08 | OCP tripped, output switched off by the device | confirmed |
| 6 | 0x40 | CC mode (0 = CV) | confirmed |
| 7 | 0x80 | output switches on automatically at power-up (menu `n0` = yes); also blinks while editing in the menu | confirmed |

The bits for OVP and OTP trips are not known yet. According to the manual, OVP and OTP trips can only be
cleared by a power cycle; OCP and OPP trips are cleared by a short press on the output key.

## Front-panel menu

Hold the output key for 3 s to enter the menu, short press for the next item, turn the voltage encoder
to change a value, hold for 3 s to save and exit.

| # | Display | Meaning | Register |
|---|---|---|---|
| 1 | `reset` yes/off | reset the setpoints | – |
| 2 | `n0` yes/no | output on at power-up | 0x0002 bit 7 |
| 3 | `Addr` | Modbus address | 0x9999 |
| 4 | `bps` | baud rate | – |
| 5 | `beep` | buzzer | 0x0002 bit 1 |
| 6 | `Lock` | panel lock | 0x0040 |
| 7 | `ouP` yes/no | OVP enable | 0x0042 |
| 8 | `ouP` value | OVP threshold | 0x0020 |
| 9 | `ocp` on/off | OCP enable | 0x0043 |
| 10 | `ocp` `---` | OCP threshold (follows the current limit) | 0x0021 |
| 11 | `Opp` on/off | OPP enable | 0x0044 |
| 12 | `Opp` `----` | OPP threshold (fixed) | 0x0022–23 |
| 13 | temperature display off/on | show the temperature on the display | – |
| 14 | `o1` | fixed, probably a version | 0x0041? |

The printed manual covers the whole E22 series and lists `TEMP` (OTP threshold) and `DIM` (brightness)
instead of `reset`/`beep`/temperature display; these items do not exist on the tested unit.
