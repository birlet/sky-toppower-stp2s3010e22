"""Read-only register scanner for the SKY TOPPOWER STP2S3010E22 (Modbus RTU).

READ ONLY: uses FC03 (or FC04 with --fc 4) and never writes a register.

Commands:
  find    search the device (all ports x baud rates x addresses)
  ident   read every register of register_map.yaml, show scaled values and check the identity
  read    read single registers / ranges once
  scan    scan address ranges register by register, save CSV + JSON
  watch   poll registers and highlight changes (operate the device to map registers)

Examples:
  python -m skypsu.scan find --addr 1-5
  python -m skypsu.scan ident --port COM8
  python -m skypsu.scan scan --port COM8 --range 0x0000-0x00FF
  python -m skypsu.scan watch --port COM8 --regs 0x0001-0x0002,0x0010-0x0013
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import serial

from . import modbus
from .psu import RegisterMap

DEFAULT_SCAN_RANGES = [(0x0000, 0x00FF), (0x1000, 0x10FF), (0x8800, 0x88FF), (0x9990, 0x99FF), (0xC100, 0xC1FF)]


# --------------------------------------------------------------------------- output


class C:
    """ANSI colours; disabled by setup_colors() when not supported."""

    RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
    RED, GREEN, YELLOW, CYAN, INVERSE = "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[7m"


def setup_colors(enabled: bool) -> None:
    if enabled and sys.stdout.isatty():
        if os.name == "nt":
            os.system("")  # enables VT processing in the Windows console
        return
    for name in ("RESET", "BOLD", "DIM", "RED", "GREEN", "YELLOW", "CYAN", "INVERSE"):
        setattr(C, name, "")


def hx(v: int | None) -> str:
    return "" if v is None else f"0x{v:04X}"


def now_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time()).isoformat(timespec="milliseconds")


def log_dim(msg: str) -> None:
    print(f"{C.DIM}{msg}{C.RESET}")


# --------------------------------------------------------------------------- arguments


def parse_int_list(text: str) -> list[int]:
    """'1' | '1-5' | '1,3,0x10-0x13' -> list of ints (hex with 0x allowed)."""
    out: list[int] = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        a, sep, b = part.partition("-")
        lo, hi = int(a, 0), int(b, 0) if sep else int(a, 0)
        if hi < lo:
            raise argparse.ArgumentTypeError(f"reversed range: {part}")
        out.extend(range(lo, hi + 1))
    return out


def parse_ranges(texts: list[str]) -> list[tuple[int, int]]:
    ranges = []
    for t in texts:
        for part in t.split(","):
            a, _, b = part.partition("-")
            start, end = int(a, 0), int(b or a, 0)
            if not 0 <= start <= end <= 0xFFFF:
                raise SystemExit(f"invalid range: {part}")
            ranges.append((start, end))
    return ranges


# --------------------------------------------------------------------------- connection


def connect(args) -> tuple[modbus.RtuClient, modbus.Link]:
    """Find the device and return an open client."""
    ports = [args.port] if args.port else None
    bauds = [args.baud] if args.baud else parse_int_list(args.bauds)
    print(f"{C.BOLD}Searching power supply{C.RESET} (ports: {', '.join(ports) if ports else 'all'}, "
          f"baud: {', '.join(map(str, bauds))}, addresses: {args.addr}, timeout {args.timeout} s)")
    hit = modbus.find_first(ports=ports, bauds=bauds, slaves=parse_int_list(args.addr),
                            timeout=args.timeout, log=log_dim)
    if not hit:
        print_not_found_help()
        raise SystemExit(2)
    link, detail = hit
    print(f"{C.GREEN}{C.BOLD}Found:{C.RESET} {link}\n  {detail}\n")
    return modbus.open_client(link, args.timeout), link


def print_not_found_help() -> None:
    print(f"""
{C.RED}{C.BOLD}No Modbus slave found.{C.RESET} Check:
  - the cable is plugged into the USB-C port labelled "COM" (not "QC") and is a data cable (4 wires)
  - a new serial port appears when plugging in (python -m serial.tools.list_ports -v)
  - read ADDR and BAUD in the device menu and pass them: --addr <ADDR> --baud <BAUD>
  - try more addresses (--addr 1-10) or baud rates (--bauds 2400,4800,9600,19200,38400,57600,115200)
  - no other program (vendor software, GUI) is holding the port
  - Linux: user is in group 'dialout' (sudo usermod -aG dialout $USER, then log in again)
  - longer timeout: --timeout 0.5""")


# --------------------------------------------------------------------------- saving


def out_prefix(args, kind: str) -> Path:
    p = Path(args.out) if args.out else Path("scan_results") / f"{kind}_{datetime.now():%Y%m%d-%H%M%S}"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def save_scan(prefix: Path, link: modbus.Link, args, results: list[modbus.ReadResult], complete: bool) -> None:
    csv_path, json_path = prefix.with_suffix(".csv"), prefix.with_suffix(".json")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["timestamp", "address_hex", "address_dec", "fc", "status", "raw_dec", "raw_hex", "detail"])
        for r in results:
            w.writerow([now_iso(r.timestamp), hx(r.address), r.address, args.fc, r.status,
                        "" if r.value is None else r.value, hx(r.value), r.detail])
    data = {
        "created": now_iso(),
        "complete": complete,
        "link": {"port": link.port, "port_info": link.port_info, "baud": link.baud, "slave": link.slave},
        "functioncode": args.fc,
        "registers": [{"address_hex": hx(r.address), "address_dec": r.address, "status": r.status,
                       "raw_dec": r.value, "raw_hex": hx(r.value) or None, "detail": r.detail,
                       "timestamp": now_iso(r.timestamp)} for r in results],
    }
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Saved: {csv_path} and {json_path}")


# --------------------------------------------------------------------------- commands


def cmd_find(args) -> int:
    print(f"{C.BOLD}Serial ports:{C.RESET}")
    for p in modbus.list_serial_ports():
        print(f"  {p.device:<14} {modbus.describe_port(p)}  {C.DIM}{p.hwid}{C.RESET}")
    print()
    hits = []
    for link, detail in modbus.discover(ports=[args.port] if args.port else None,
                                        bauds=[args.baud] if args.baud else parse_int_list(args.bauds),
                                        slaves=parse_int_list(args.addr), timeout=args.timeout, log=log_dim):
        print(f"{C.GREEN}{C.BOLD}Found:{C.RESET} {link} - {detail}")
        hits.append(link)
        if not args.all:
            break
    if not hits:
        print_not_found_help()
        return 2
    print(f"\nFor the other commands: --port {hits[0].port} --baud {hits[0].baud} --addr {hits[0].slave}")
    return 0


def cmd_ident(args) -> int:
    regmap = RegisterMap.load()
    client, _link = connect(args)
    try:
        res = modbus.read_addresses(client, regmap.addresses(), args.fc, args.block_size)
    finally:
        client.close()
    colour = {"confirmed": C.GREEN, "likely": "", "unknown": C.DIM}
    print(f"{C.BOLD}Registers from register_map.yaml{C.RESET}")
    print(f"  {'address':<8} {'name':<14} {'raw':>8} {'hex':>8}  {'value':<12} {'status':<10} note")
    for reg in sorted(regmap.regs.values(), key=lambda r: r.addr):
        parts = [res[a] for a in range(reg.addr, reg.addr + reg.words)]
        if not all(p.ok for p in parts):
            bad = next(p for p in parts if not p.ok)
            print(f"  {C.RED}0x{reg.addr:04X}   {reg.name:<14} {bad.status}: {bad.detail}{C.RESET}")
            continue
        raw = 0
        for p in parts:
            raw = (raw << 16) | p.value
        value = f"{regmap.scale(reg.name, raw):.{reg.decimals}f} {reg.unit}" if reg.decimals is not None else reg.unit
        print(f"  {colour.get(reg.status, '')}0x{reg.addr:04X}   {reg.name:<14} {raw:>8} {hx(raw):>8}  "
              f"{value:<12} {reg.status:<10} {reg.note}{C.RESET}")
    flags = res[regmap["status_flags"].addr].value
    if flags is not None:
        set_flags = [n for n, bit in regmap.flags.items() if flags & bit]
        print(f"\n  status_flags = 0x{flags:02X}: {', '.join(set_flags) or 'none'}")
    mismatch = [f"0x{a:04X} = {res[a].value}, expected {v}" for a, v in regmap.identity.items() if res[a].value != v]
    if mismatch:
        print(f"\n{C.RED}{C.BOLD}Identity does NOT match:{C.RESET} " + "; ".join(mismatch))
        return 1
    print(f"\n{C.GREEN}{C.BOLD}Identity matches (STP2S3010E22).{C.RESET}")
    return 0


def cmd_read(args) -> int:
    names = {a: r.name for a, r in RegisterMap.load().by_address().items()}
    addrs = parse_int_list(args.regs)
    client, _link = connect(args)
    try:
        res = modbus.read_addresses(client, addrs, args.fc, args.block_size)
    finally:
        client.close()
    for a in addrs:
        r = res[a]
        if r.ok:
            print(f"  0x{a:04X}  {r.value:>6}  {hx(r.value)}  {C.DIM}{names.get(a, '')}{C.RESET}")
        else:
            print(f"  {C.DIM}0x{a:04X}  {r.status}: {r.detail}  {names.get(a, '')}{C.RESET}")
    return 0


def cmd_scan(args) -> int:
    names = {a: r.name for a, r in RegisterMap.load().by_address().items()}
    ranges = parse_ranges(args.range) if args.range else DEFAULT_SCAN_RANGES
    client, link = connect(args)
    prefix = out_prefix(args, "scan")
    results: list[modbus.ReadResult] = []
    complete = False
    try:
        for start, end in ranges:
            found = 0

            def progress(block_start, n, block):
                nonlocal found
                found += sum(r.ok for r in block)
                print(f"\r  0x{start:04X}-0x{end:04X}: at 0x{block_start + n - 1:04X}, {found} readable   ",
                      end="", flush=True)

            print(f"{C.BOLD}Scanning 0x{start:04X}-0x{end:04X}{C.RESET} (FC{args.fc:02d}, block size {args.block_size})")
            t0 = time.time()
            part = modbus.read_range(client, start, end - start + 1, args.fc, args.block_size, on_block=progress)
            results.extend(part)
            print(f"\r  0x{start:04X}-0x{end:04X}: done in {time.time() - t0:.1f} s{' ' * 20}")
            print_range_summary(part, names)
        complete = True
    except KeyboardInterrupt:
        print(f"\n{C.YELLOW}Aborted - saving partial result.{C.RESET}")
    except serial.SerialException as exc:
        print(f"\n{C.RED}Port error: {exc} - saving partial result.{C.RESET}")
    finally:
        client.close()
        if not args.no_save:
            save_scan(prefix, link, args, results, complete)
    return 0 if complete else 1


def print_range_summary(part: list[modbus.ReadResult], names: dict[int, str]) -> None:
    counts: dict[str, int] = {}
    for r in part:
        counts[r.status] = counts.get(r.status, 0) + 1
    details = sorted({r.detail for r in part if r.status == "exception"})
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
          + (f"  {C.DIM}({'; '.join(details)}){C.RESET}" if details else ""))
    for r in part:
        if r.ok:
            colour = C.DIM if r.value == 0 else ""
            print(f"    {colour}0x{r.address:04X}  {r.value:>6}  {hx(r.value)}  {names.get(r.address, '')}{C.RESET}")
    print()


def cmd_watch(args) -> int:
    names = {a: r.name for a, r in RegisterMap.load().by_address().items()}
    addrs = parse_int_list(args.regs) if args.regs else sorted(names)
    client, _link = connect(args)
    log_rows: list[list] = []
    prev: dict[int, tuple[str, int | None]] = {}
    cycles = 0
    print(f"{C.BOLD}Watch{C.RESET}: {len(addrs)} registers every {args.interval} s. "
          "Operate the device; Ctrl+C stops.\n")
    try:
        while True:
            t0 = time.time()
            snap = modbus.read_addresses(client, addrs, args.fc, args.block_size)
            stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            for a in addrs:
                r, name = snap[a], names.get(a, "")
                key = (r.status, r.value)
                if a not in prev:
                    shown = f"{r.value:>6}  {hx(r.value)}" if r.ok else r.status
                    print(f"{C.DIM}{stamp}{C.RESET}  0x{a:04X}  {shown}  {C.DIM}{name}{C.RESET}")
                    log_rows.append([now_iso(r.timestamp), hx(a), a, "initial", "", r.value, hx(r.value), r.status])
                elif key != prev[a]:
                    old = prev[a][1]
                    delta = f"  d {r.value - old:+d}" if r.ok and old is not None else ""
                    old_txt = f"{old} ({hx(old)})" if old is not None else prev[a][0]
                    new_txt = f"{r.value} ({hx(r.value)})" if r.ok else r.status
                    print(f"{stamp}  {C.INVERSE}{C.YELLOW}0x{a:04X}{C.RESET}  {old_txt} -> "
                          f"{C.BOLD}{new_txt}{C.RESET}{delta}  {C.CYAN}{name}{C.RESET}")
                    log_rows.append([now_iso(r.timestamp), hx(a), a, "change", "" if old is None else old,
                                     r.value, hx(r.value), r.status])
                prev[a] = key
            if cycles == 0:
                print(f"{C.DIM}--- only changes from here on ---{C.RESET}")
            cycles += 1
            time.sleep(max(0.0, args.interval - (time.time() - t0)))
    except KeyboardInterrupt:
        print(f"\nStopped after {cycles} cycles.")
    except serial.SerialException as exc:
        print(f"\n{C.RED}Port error: {exc}{C.RESET}")
    finally:
        client.close()
        if not args.no_save:
            path = out_prefix(args, "watch").with_suffix(".csv")
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["timestamp", "address_hex", "address_dec", "event", "old_raw_dec", "raw_dec", "raw_hex", "status"])
                w.writerows(log_rows)
            print(f"Saved: {path}")
    return 0


# --------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("connection")
    g.add_argument("--port", help="serial port, e.g. COM8 or /dev/ttyUSB0 (default: search all)")
    g.add_argument("--baud", type=int, help="fixed baud rate (default: try --bauds)")
    g.add_argument("--bauds", default=",".join(map(str, modbus.DEFAULT_BAUDS)),
                   help="baud rates to try (default: %(default)s)")
    g.add_argument("--addr", default="1", help="slave address(es): 1 | 1-5 | 1,3 (default: %(default)s)")
    g.add_argument("--timeout", type=float, default=modbus.DEFAULT_TIMEOUT, help="response timeout in s (default: %(default)s)")
    g.add_argument("--fc", type=int, choices=(3, 4), default=3,
                   help="read function code: 3 = holding, 4 = input registers (default: %(default)s)")
    g.add_argument("--block-size", type=int, default=modbus.DEFAULT_BLOCK_SIZE,
                   help="registers per block read (default: %(default)s; the device rejects more than 4)")
    g.add_argument("--no-color", action="store_true", help="no ANSI colours")

    out = argparse.ArgumentParser(add_help=False)
    out.add_argument("--out", help="output path without extension (default: scan_results/<command>_<time>)")
    out.add_argument("--no-save", action="store_true", help="do not save anything")

    p = argparse.ArgumentParser(prog="python -m skypsu.scan",
                                description="Read-only Modbus RTU register scanner for the SKY TOPPOWER STP2S3010E22",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Commands:" + __doc__.split("Commands:")[1])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("find", parents=[common], help="search the device")
    s.add_argument("--all", action="store_true", help="keep searching after the first hit")
    s.set_defaults(func=cmd_find)

    s = sub.add_parser("ident", parents=[common], help="read all known registers and check the identity")
    s.set_defaults(func=cmd_ident)

    s = sub.add_parser("read", parents=[common], help="read registers once")
    s.add_argument("--regs", required=True, help="e.g. 0x0010-0x0013,0x0030,0x9999")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("scan", parents=[common, out], help="scan address ranges")
    s.add_argument("--range", action="append",
                   help="range(s) start-end, repeatable or comma separated (default: "
                        + ", ".join(f"0x{a:04X}-0x{b:04X}" for a, b in DEFAULT_SCAN_RANGES) + ")")
    # The device only validates the start address and returns phantom values behind real registers
    s.set_defaults(func=cmd_scan, block_size=1)

    s = sub.add_parser("watch", parents=[common, out], help="poll registers, highlight changes")
    s.add_argument("--regs", help="registers to watch (default: all registers of register_map.yaml)")
    s.add_argument("--interval", type=float, default=0.3, help="poll interval in s (default: %(default)s)")
    s.set_defaults(func=cmd_watch)
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    setup_colors(not args.no_color)
    if not 1 <= args.block_size <= modbus.MAX_REGISTERS_PER_READ:
        raise SystemExit(f"--block-size must be between 1 and {modbus.MAX_REGISTERS_PER_READ}")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
