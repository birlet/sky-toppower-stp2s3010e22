"""Live matplotlib chart: current (measured/limit) and voltage (measured/set) over time.

READ ONLY. Two charts with a shared time axis (one quantity per axis). Needs matplotlib.

Examples:
  python -m skypsu.plot                         # find the device, show the last 2 minutes
  python -m skypsu.plot --port COM8 --window 300 --csv log.csv
  python -m skypsu.plot --duration 10 --png snapshot.png   # headless: record 10 s, save an image
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import deque
from datetime import datetime

from . import modbus
from .psu import PsuError, SkyPSU

# Colours / chrome (light theme)
SURFACE, INK, INK_2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
C_ACTUAL, C_TARGET = "#2a78d6", "#eb6834"
DASHED = (0, (5, 3))


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="python -m skypsu.plot",
                                description="Live chart of current and voltage, measured and set (read only)")
    p.add_argument("--port", help="serial port (default: search)")
    p.add_argument("--interval", type=float, default=0.25, help="poll interval in s (default: %(default)s)")
    p.add_argument("--window", type=float, default=120, help="visible time span in s (default: %(default)s)")
    p.add_argument("--csv", help="also write the samples to a CSV file")
    p.add_argument("--duration", type=float, help="stop after this many seconds")
    p.add_argument("--png", help="save the chart as PNG when stopping")
    return p.parse_args(argv)


class Sampler:
    def __init__(self, args):
        self.psu = SkyPSU(port=args.port)
        self.psu.connect()
        maxlen = int(args.window / args.interval) + 10
        self.t, self.v, self.vs, self.i, self.is_ = (deque(maxlen=maxlen) for _ in range(5))
        self.output: bool | None = None
        self.errors = 0
        self.t0 = time.monotonic()
        self.csv_file = open(args.csv, "w", newline="", encoding="utf-8") if args.csv else None
        if self.csv_file:
            self.writer = csv.writer(self.csv_file, delimiter=";")
            self.writer.writerow(["timestamp", "t_s", "output", "v_meas_V", "v_set_V", "i_meas_A", "i_set_A"])

    def sample(self) -> None:
        try:
            r = self.psu.read("output", "voltage", "current", "set_voltage", "set_current")
        except (PsuError, modbus.ModbusError):
            self.errors += 1
            return
        t = time.monotonic() - self.t0
        self.output = bool(r["output"])
        for dq, val in ((self.t, t), (self.v, r["voltage"]), (self.i, r["current"]),
                        (self.vs, r["set_voltage"]), (self.is_, r["set_current"])):
            dq.append(val)
        if self.csv_file:
            self.writer.writerow([datetime.now().isoformat(timespec="milliseconds"), f"{t:.3f}", int(self.output),
                                  f"{r['voltage']:.2f}", f"{r['set_voltage']:.2f}",
                                  f"{r['current']:.3f}", f"{r['set_current']:.3f}"])
            self.csv_file.flush()

    def close(self) -> None:
        self.psu.close()
        if self.csv_file:
            self.csv_file.close()


def style_axis(ax, ylabel: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.set_ylabel(ylabel, color=INK_2, fontsize=10)


def main(argv=None) -> int:
    args = parse_args(argv)
    import matplotlib

    if args.duration and args.png:
        matplotlib.use("Agg")  # record without a window
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    s = Sampler(args)
    fig, (ax_i, ax_v) = plt.subplots(2, 1, sharex=True, figsize=(10, 6.5), facecolor=SURFACE,
                                     gridspec_kw={"hspace": 0.25})
    fig.canvas.manager.set_window_title("SKY TOPPOWER - current and voltage")
    style_axis(ax_i, "Current (A)")
    style_axis(ax_v, "Voltage (V)")
    ax_v.set_xlabel("Time (s)", color=INK_2, fontsize=10)

    # (key, axis, colour, dashed, label, format); the target line is drawn first so the measured one stays on top
    spec = [("is_", ax_i, C_TARGET, True, "Limit", "Limit {:.3f} A"), ("i", ax_i, C_ACTUAL, False, "Measured", "Measured {:.3f} A"),
            ("vs", ax_v, C_TARGET, True, "Set", "Set {:.2f} V"), ("v", ax_v, C_ACTUAL, False, "Measured", "Measured {:.2f} V")]
    lines = {k: ax.plot([], [], color=col, lw=2, ls=DASHED if dashed else "-",
                        drawstyle="steps-post" if dashed else "default", label=label)[0]
             for k, ax, col, dashed, label, _ in spec}
    for ax in (ax_i, ax_v):
        ax.legend(loc="upper left", frameon=False, ncol=2, fontsize=9, labelcolor=INK_2)
    # direct labels at the right end of the lines (ink colour, not series colour)
    labels = {k: ax.text(0, 0, "", fontsize=9, color=INK_2, va="center", ha="left", clip_on=False)
              for k, ax, *_ in spec}
    title = fig.suptitle("", x=0.125, ha="left", fontsize=12, color=INK)
    fig.subplots_adjust(right=0.86)

    def update(_frame):
        s.sample()
        if not s.t:
            return
        t = list(s.t)
        series = {"i": s.i, "is_": s.is_, "v": s.v, "vs": s.vs}
        for k, line in lines.items():
            line.set_data(t, list(series[k]))
        t_start = max(0.0, t[-1] - args.window)
        ax_v.set_xlim(t_start, max(t[-1], t_start + 10))
        for ax, keys, floor in ((ax_i, ("i", "is_"), 0.01), (ax_v, ("v", "vs"), 0.1)):
            top = max(max(series[keys[0]]), max(series[keys[1]]), floor) * 1.15
            ax.set_ylim(-0.04 * top, top)  # headroom below 0 so a line at 0 stays visible
        x_end = ax_v.get_xlim()[1]
        for k, _ax, _c, _d, _l, fmt in spec:
            labels[k].set_position((x_end, series[k][-1]))
            labels[k].set_text(" " + fmt.format(series[k][-1]))
        # keep the labels apart when measured and set are (almost) equal
        for meas, target, ax in (("i", "is_", ax_i), ("v", "vs", ax_v)):
            lo, hi = ax.get_ylim()
            if abs(series[meas][-1] - series[target][-1]) < 0.08 * (hi - lo):
                y = series[meas][-1]
                labels[target].set_position((x_end, y + 0.05 * (hi - lo)))
                labels[meas].set_position((x_end, y - 0.05 * (hi - lo)))
        state = {True: "ON", False: "OFF"}.get(s.output, "?")
        title.set_text(f"Output {state}   ·   {s.v[-1]:.2f} V   ·   {s.i[-1]:.3f} A"
                       + (f"   ·   {s.errors} read errors" if s.errors else ""))

    try:
        if args.duration and args.png:
            end = time.monotonic() + args.duration
            while time.monotonic() < end:
                update(None)
                time.sleep(args.interval)
        else:
            anim = FuncAnimation(fig, update, interval=int(args.interval * 1000), cache_frame_data=False)
            if args.duration:
                timer = fig.canvas.new_timer(interval=int(args.duration * 1000))
                timer.add_callback(plt.close, fig)
                timer.start()
            plt.show()
            del anim
    except KeyboardInterrupt:
        pass
    finally:
        if args.png:
            fig.savefig(args.png, dpi=110, facecolor=SURFACE)
            print(f"Saved: {args.png}")
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
