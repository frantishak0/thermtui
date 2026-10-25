#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
thermtui — минималистичный TUI-монитор температур с логом текущей сессии.

Особенности (минимум влияния на систему):
  * данные берутся напрямую из sysfs (/sys/...), без вызова sensors/lm-sensors;
  * один поток, один опрос раз в N секунд (по умолчанию 2 с);
  * нет busy-loop: между опросами процесс спит в getch() с таймаутом;
  * лог пишется в tmpfs ($XDG_RUNTIME_DIR), а не на диск;
  * лог живёт только пока живёт сессия: удаляется при выходе и при старте
    подчищаются логи прошлых сессий.

Клавиши:
  q / Esc      выход
  ← / →        прокрутка графика по времени (влево — назад)
  Home / End   в начало / к «живому» краю
  Space        пауза опроса
  + / -        интервал опроса
  1..7         вкл/выкл серию на графике
  r            сброс min/max сессии

Запуск:
  ./thermtui.py                 # TUI
  ./thermtui.py --once          # разовый вывод значений (без TUI)
  ./thermtui.py --duration 60   # TUI сам закроется через 60 с
"""

from __future__ import annotations

import argparse
import curses
import glob
import locale
import os
import signal
import sys
import time
from collections import deque

# ---------------------------------------------------------------- sysfs helpers

HWMON = "/sys/class/hwmon"
THERMAL = "/sys/class/thermal"
CPUCFREQ = "/sys/devices/system/cpu"


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> int | None:
    txt = _read_text(path)
    if not txt:
        return None
    try:
        return int(txt.split()[0])
    except (ValueError, IndexError):
        return None


def find_hwmon(name: str) -> str | None:
    for d in glob.glob(HWMON + "/hwmon*"):
        if _read_text(os.path.join(d, "name")) == name:
            return d
    return None


class Series:
    """Один источник температуры либо производный показатель."""

    unit = "°C"

    def __init__(self, sid, name, path, warn, crit, color, group="temp"):
        self.id = sid
        self.name = name
        self.path = path
        self.warn = warn
        self.crit = crit
        self.color = color
        self.group = group
        self.on = True
        self.marker = "o"

    def read(self):
        if not self.path:
            return None
        raw = _read_int(self.path)
        return None if raw is None else raw / 1000.0


def discover_series() -> list[Series]:
    """Собрать список температурных датчиков, которые реально есть в системе."""
    out: list[Series] = []
    colors = [2, 6, 5, 3, 4, 7]
    markers = ["o", "x", "+", "*", "#", "&", "@", "%", "$"]

    core = find_hwmon("coretemp")
    if core:
        labels = {}
        for inp in sorted(glob.glob(os.path.join(core, "temp*_input"))):
            base = inp[: -len("_input")]
            lab = _read_text(base + "_label") or os.path.basename(base)
            labels[base] = lab
        for base, lab in labels.items():
            if lab.lower().startswith("package"):
                sid, name, warn, crit = "cpu_pkg", "CPU пакет", 80, 95
            else:
                sid = "cpu_" + lab.lower().replace(" ", "")
                name = lab.replace("Core", "Ядро")
                warn, crit = 80, 95
            out.append(Series(sid, name, base + "_input", warn, crit, 2))

    # ACPI-зона (обычно датчик рядом с чипсетом/корпусом)
    acpi = find_hwmon("acpitz_0")
    if acpi and os.path.exists(os.path.join(acpi, "temp1_input")):
        out.append(Series("acpi", "ACPI", acpi + "/temp1_input", 70, 85, 3))

    # NVMe
    nvme = find_hwmon("nvme")
    if nvme:
        for inp in sorted(glob.glob(os.path.join(nvme, "temp*_input"))):
            base = inp[: -len("_input")]
            lab = _read_text(base + "_label") or "Composite"
            if lab.lower().startswith("composite"):
                out.append(Series("nvme", "NVMe", inp, 60, 80, 5))
            break  # остальные сенсоры SSD малоинформативны

    # thermal zones: TCPU — самый близкий к кристаллу ACPI-датчик
    for zone in sorted(glob.glob(THERMAL + "/thermal_zone*")):
        ztype = _read_text(os.path.join(zone, "type")) or ""
        if ztype.upper() == "TCPU":
            out.append(Series("tcpu", "TCPU", zone + "/temp", 80, 95, 6))
        elif ztype.lower() == "acpitz":
            continue  # уже есть через hwmon
        elif ztype.startswith("x86_pkg") or ztype.startswith("INT3400"):
            continue  # дубли пакета CPU / виртуальная зона
        elif ztype:
            out.append(
                Series("z_" + ztype.lower(), ztype, zone + "/temp", 80, 95, 4)
            )

    for i, s in enumerate(out):
        color = colors[i % len(colors)]
        if s.id in ("cpu_pkg", "tcpu", "nvme", "acpi"):
            color = {"cpu_pkg": 2, "tcpu": 6, "nvme": 5, "acpi": 3}[s.id]
        s.color = color
        s.marker = markers[i % len(markers)]
        s.index = i + 1
        s.on = s.id in ("cpu_pkg", "tcpu", "nvme")   # по умолчанию — главное
    return out


def cpu_freqs() -> list[int]:
    freqs = []
    for path in glob.glob(CPUCFREQ + "/cpu[0-9]*/cpufreq/scaling_cur_freq"):
        v = _read_int(path)
        if v:
            freqs.append(v // 1000)
    return freqs


def battery_power_w() -> float | None:
    for d in glob.glob("/sys/class/power_supply/BAT*"):
        val = _read_int(os.path.join(d, "power_now"))
        if val:
            return val / 1e6
    return None


def cpu_total_jiffies() -> tuple[int, int]:
    txt = _read_text("/proc/stat")
    if not txt:
        return (0, 0)
    parts = txt.splitlines()[0].split()[1:]
    vals = [int(p) for p in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def proc_cpu_seconds(pid: int = 0) -> float:
    """utime+stime процесса в секундах (для показа собственного расхода)."""
    txt = _read_text(f"/proc/{pid}/stat")
    if not txt:
        return 0.0
    try:
        rest = txt[txt.rindex(")") + 2 :].split()
        return (int(rest[11]) + int(rest[12])) / os.sysconf("SC_CLK_TCK")
    except (ValueError, IndexError):
        return 0.0


# --------------------------------------------------------------- session store

class Session:
    """Кольцевой буфер сэмплов + CSV-лог, живущий только текущую сессию."""

    def __init__(self, series: list[Series], maxlen: int = 20000):
        self.series = series
        self.samples: deque[dict] = deque(maxlen=maxlen)
        self.min: dict[str, float] = {}
        self.max: dict[str, float] = {}
        self.freq_min = self.freq_max = None
        self.load_min = self.load_max = None

    def reset_stats(self):
        self.min.clear()
        self.max.clear()
        self.freq_min = self.freq_max = None
        self.load_min = self.load_max = None

    def add(self, sample: dict):
        self.samples.append(sample)
        for s in self.series:
            v = sample.get(s.id)
            if v is None:
                continue
            if s.id not in self.min or v < self.min[s.id]:
                self.min[s.id] = v
            if s.id not in self.max or v > self.max[s.id]:
                self.max[s.id] = v
        f = sample.get("freq")
        if f:
            self.freq_min = f if self.freq_min is None else min(self.freq_min, f)
            self.freq_max = f if self.freq_max is None else max(self.freq_max, f)
        ld = sample.get("load")
        if ld is not None:
            self.load_min = ld if self.load_min is None else min(self.load_min, ld)
            self.load_max = ld if self.load_max is None else max(self.load_max, ld)


class CsvLog:
    """Лог текущей сессии в tmpfs; после выхода остаётся, но при следующем
    запуске старые логи удаляются (истории не накапливаем)."""

    def __init__(self, series: list[Series], path: str | None = None,
                 keep: bool = False):
        self.series = series
        self.keep = keep
        self.dir = path or os.path.join(
            os.environ.get("XDG_RUNTIME_DIR") or "/tmp", f"thermtui-{os.getuid()}"
        )
        self.path = None
        self.fh = None
        if path and path.endswith(".csv"):
            self.dir = os.path.dirname(path) or "."
            self._open(os.path.basename(path))
        else:
            try:
                os.makedirs(self.dir, mode=0o700, exist_ok=True)
                self._cleanup_old()
                stamp = time.strftime("%Y%m%d-%H%M%S")
                self._open(f"session-{stamp}-{os.getpid()}.csv")
            except OSError as exc:
                print(f"warning: лог недоступен: {exc}", file=sys.stderr)

    def _cleanup_old(self):
        if self.keep:
            return
        for f in glob.glob(os.path.join(self.dir, "session-*.csv")):
            try:
                os.unlink(f)
            except OSError:
                pass

    def _open(self, name: str):
        self.path = os.path.join(self.dir, name)
        self.fh = open(self.path, "w", encoding="utf-8", buffering=1)
        cols = ["epoch", "iso_time"] + [s.id for s in self.series]
        cols += ["freq_mhz", "load_pct", "power_w"]
        self.fh.write(",".join(cols) + "\n")

    def write(self, sample: dict):
        if not self.fh:
            return
        row = [f"{sample['t']:.3f}", time.strftime("%Y-%m-%d %H:%M:%S",
                                                   time.localtime(sample["t"]))]
        for s in self.series:
            v = sample.get(s.id)
            row.append("" if v is None else f"{v:.1f}")
        row.append(f"{sample.get('freq') or 0}")
        row.append(f"{sample.get('load') or 0:.1f}")
        row.append(f"{sample.get('pw') or 0:.2f}")
        self.fh.write(",".join(row) + "\n")

    def close(self):
        if self.fh:
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None


# --------------------------------------------------------------------- drawing

GLYPHS_UTF8 = {
    "spark": " ▁▂▃▄▅▆▇█",
    "vline": "•",
    "hfill": "─",
    "up": "↑",
    "down": "↓",
    "flat": "·",
    "dot": "•",
    "deg": "°C",
}
GLYPHS_ASCII = {
    "spark": " .:-=+*#",
    "vline": ".",
    "hfill": "-",
    "up": "^",
    "down": "v",
    "flat": "-",
    "dot": "*",
    "deg": "C",
}


def utf8_ok() -> bool:
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in enc


class App:
    def __init__(self, args, series, session, log):
        self.args = args
        self.series = series
        self.session = session
        self.log = log
        self.interval = args.interval
        self.paused = False
        self.frozen_end = None   # абсолютный индекс правого края в режиме скролла
        self.follow = True
        self.running = True
        self.start = time.monotonic()
        self.next_sample = self.start
        self.prev_total, self.prev_idle = cpu_total_jiffies()
        self.tool_cpu0 = proc_cpu_seconds()
        self.tool_cpu_pct = 0.0
        self.g = GLYPHS_UTF8 if (utf8_ok() and not args.ascii) else GLYPHS_ASCII

    # --- сбор данных -------------------------------------------------------
    def sample(self):
        now = time.monotonic()
        smp = {"t": time.time(), "mono": now}
        for s in self.series:
            smp[s.id] = s.read()

        freqs = cpu_freqs()
        smp["freq"] = max(freqs) if freqs else None
        smp["freqs"] = freqs
        smp["pw"] = battery_power_w()

        total, idle = cpu_total_jiffies()
        dt = total - self.prev_total
        di = idle - self.prev_idle
        smp["load"] = 100.0 * (dt - di) / dt if dt > 0 else None
        self.prev_total, self.prev_idle = total, idle

        tc = proc_cpu_seconds()
        elapsed = max(now - self.start, 1e-6)
        self.tool_cpu_pct = 100.0 * (tc - self.tool_cpu0) / elapsed
        self.tool_cpu0 = tc

        self.session.add(smp)
        self.log.write(smp)
        if self.follow:
            self.frozen_end = None

    # --- вспомогательное рисование ----------------------------------------
    def put(self, scr, y, x, text, attr=0):
        h, w = scr.getmaxyx()
        if y < 0 or y >= h or x >= w - 1:
            return
        try:
            scr.addstr(y, x, text[: max(0, w - 1 - x)], attr)
        except curses.error:
            pass

    def temp_attr(self, s, v):
        if v is None:
            return curses.A_DIM
        if v >= s.crit:
            return curses.color_pair(3) | curses.A_BOLD
        if v >= s.warn:
            return curses.color_pair(2) | curses.A_BOLD
        return curses.color_pair(1)

    def sparkline(self, values, width):
        if not values:
            return " " * width
        vals = list(values)[-width:]
        lo, hi = min(vals), max(vals)
        chars = self.g["spark"]
        if hi - lo < 0.5:
            return chars[-1 if lo > 0 else 0] * len(vals)
        out = []
        for v in vals:
            idx = int((v - lo) / (hi - lo) * (len(chars) - 1) + 0.5)
            out.append(chars[max(0, min(len(chars) - 1, idx))])
        return "".join(out).rjust(width)

    def bar(self, value, warn, crit, width):
        if value is None:
            return " " * width
        frac = max(0.0, min(1.0, value / max(crit, 1)))
        filled = int(round(frac * width))
        return "█" * filled + "·" * (width - filled) if utf8_ok() else \
            "#" * filled + "." * (width - filled)

    # --- отрисовка ---------------------------------------------------------
    def draw(self, scr):
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 18 or w < 64:
            self.put(scr, 0, 0, "Терминал слишком мал (нужно ≥64x18)", curses.A_BOLD)
            scr.refresh()
            return

        self.cols = w
        samples = list(self.session.samples)
        last = samples[-1] if samples else {}

        # --- заголовок
        state = "ПАУЗА" if self.paused else ("LIVE" if self.follow else "СКРОЛЛ")
        uptime = int(time.monotonic() - self.start)
        if w < 76:
            hdr = (f" thermtui│{state}│{self.interval}c│"
                   f"{uptime // 60:02d}:{uptime % 60:02d}│{len(samples)} сэмп.")
        else:
            hdr = (f" thermtui │ {state} │ интервал {self.interval} c │ "
                   f"сессия {uptime // 60:02d}:{uptime % 60:02d} │ "
                   f"сэмплов {len(samples)}")
        load_now = last.get("load")
        if h < 27 and load_now is not None:
            hdr += (f" │ загрузка {load_now:4.1f}%" if w >= 76
                    else f"│нагр {load_now:4.1f}%")
        self.put(scr, 0, 0, hdr[: w - 1].ljust(w - 1),
                 curses.color_pair(4) | curses.A_BOLD)
        if self.log.path:
            self.put(scr, 1, 0, f" лог: {self.log.path}", curses.A_DIM)

        # --- таблица датчиков
        y = 2
        self.put(scr, y, 1, "датчик", curses.A_UNDERLINE)
        self.put(scr, y, 18, "сейчас", curses.A_UNDERLINE)
        self.put(scr, y, 27, "загрузка", curses.A_UNDERLINE)
        self.put(scr, y, 40, "min", curses.A_UNDERLINE)
        self.put(scr, y, 47, "max", curses.A_UNDERLINE)
        self.put(scr, y, 54, "динамика", curses.A_UNDERLINE)
        y += 1

        for s in self.series:
            vals = [sm.get(s.id) for sm in samples if sm.get(s.id) is not None]
            cur = vals[-1] if vals else None
            key = f"[{s.index}]" if s.on else f" {s.index} "
            name = f"{key} {s.name}"[:16]
            self.put(scr, y, 1, name, curses.A_BOLD if s.on else curses.A_DIM)
            cur_s = "—" if cur is None else f"{cur:.1f}{self.g['deg']}"
            self.put(scr, y, 18, cur_s.rjust(7), self.temp_attr(s, cur))
            self.put(scr, y, 27, self.bar(cur, s.warn, s.crit, 11),
                     self.temp_attr(s, cur))
            lo = self.session.min.get(s.id)
            hi = self.session.max.get(s.id)
            self.put(scr, y, 40, ("—" if lo is None else f"{lo:.1f}").rjust(5))
            self.put(scr, y, 47, ("—" if hi is None else f"{hi:.1f}").rjust(5))
            self.put(scr, y, 54, self.sparkline(vals, 14),
                     curses.color_pair(s.color))
            arrow = self.g["flat"]
            if len(vals) >= 2:
                d = vals[-1] - vals[-2]
                arrow = self.g["up"] if d > 0.3 else (
                    self.g["down"] if d < -0.3 else self.g["flat"])
            self.put(scr, y, 69, arrow)
            y += 1

        # --- прочие показатели (в компактном режиме уезжают в заголовок)
        freq = last.get("freq")
        freqs = last.get("freqs") or []
        load = last.get("load")
        pw = last.get("pw")
        compact = h < 27
        if not compact:
            y += 1
            self.put(scr, y, 1, "прочее", curses.A_UNDERLINE)
            y += 1
            f_txt = "—" if not freq else (
                " / ".join(f"{f}" for f in freqs) + " МГц")
            self.put(scr, y, 1, "частота ядер", curses.A_DIM)
            self.put(scr, y, 18, f_txt)
            if self.session.freq_min:
                self.put(scr, y, 40, f"{self.session.freq_min}".rjust(5))
                self.put(scr, y, 47, f"{self.session.freq_max}".rjust(5))
            y += 1
            self.put(scr, y, 1, "нагрузка CPU", curses.A_DIM)
            if load is not None:
                self.put(scr, y, 18, f"{load:5.1f}%")
                self.put(scr, y, 27, self.bar(load, 60, 100, 11),
                         curses.color_pair(1))
            else:
                self.put(scr, y, 18, "—")
            y += 1
            self.put(scr, y, 1, "питание", curses.A_DIM)
            self.put(scr, y, 18, "—" if pw is None else f"{pw:.2f} Вт")
            self.put(scr, y, 33,
                     f"расход самой утилиты: {self.tool_cpu_pct:.1f}% CPU",
                     curses.A_DIM)
            y += 1
            self.put(scr, y, 1, "RAM", curses.A_DIM)
            self.put(scr, y, 18, "нет датчика в системе (LPDDR, распаяна)",
                     curses.A_DIM)

        # --- график
        gy = y + 2
        plot_top = gy + 1
        plot_bot = h - 5          # ниже: ось, время, легенда, футер
        plot_h = plot_bot - plot_top + 1
        if plot_h < 3:
            self.put(scr, gy, 1, "нет места для графика — увеличьте окно",
                     curses.A_DIM)
            self.footer(scr)
            scr.refresh()
            return

        active = [s for s in self.series if s.on]
        label_w = 6
        plot_x = label_w
        plot_w = w - 1 - plot_x
        if plot_w < 10:
            self.footer(scr)
            scr.refresh()
            return

        total = len(samples)
        win = min(plot_w, total)
        if self.follow or self.frozen_end is None:
            end = total
        else:
            end = min(self.frozen_end, total)
            if end < win:
                end = win
        start = max(0, end - win)
        view = samples[start:end]
        self.view = (start, end, total)

        header = f"окно графика: {win} сэмп. × {self.interval} c"
        if view:
            span = view[-1]["mono"] - view[0]["mono"]
            header += f" ≈ {span:.0f} c"
        self.put(scr, gy, 1, header, curses.A_BOLD)
        self.put(scr, gy, w - 22,
                 ("← назад  вперёд →" if not self.follow else "LIVE"),
                 curses.color_pair(4))

        # вертикальный диапазон: «круглые» деления
        allvals = [sm.get(s.id) for s in active for sm in view
                   if sm.get(s.id) is not None]
        if allvals:
            vlo, vhi = min(allvals), max(allvals)
        else:
            vlo, vhi = 30.0, 60.0
        max_lines = max(2, min(6, plot_h // 2 + 1))
        if max_lines <= 2 or plot_h <= 4:
            lo, hi = int(vlo), int(-(-vhi // 1))
            if hi - lo < 2:
                hi = lo + 2
            step_y = hi - lo
        else:
            lo, hi, step_y = self.nice_scale(vlo - 1, vhi + 1, max_lines)

        def row_of(v):
            return plot_bot - int(round((v - lo) / (hi - lo) * (plot_h - 1)))

        # сетка + подписи оси Y
        val = lo
        dot = "·" if utf8_ok() else "."
        grid_row = (dot + " " * 7) * plot_w
        while val <= hi + 1e-9:
            r = row_of(val)
            self.put(scr, r, 0, f"{val:5.0f}", curses.A_DIM)
            if lo < val < hi:
                self.put(scr, r, plot_x + 1, grid_row[: plot_w - 2], curses.A_DIM)
            val += step_y
        # вертикальная ось
        for r in range(plot_top, plot_bot + 1):
            self.put(scr, r, plot_x - 1, "│", curses.A_DIM)

        # серии (пока данных мало — растягиваем их на всю ширину)
        n = len(view)
        if n <= 1:
            xs = [plot_x] * n
        elif n < plot_w:
            xs = [plot_x + round(i * (plot_w - 2) / (n - 1)) for i in range(n)]
        else:
            xs = [plot_x + i for i in range(n)]
        for s in active:
            attr = curses.color_pair(s.color)
            prev = None
            for x, sm in zip(xs, view):
                v = sm.get(s.id)
                if v is None:
                    prev = None
                    continue
                r = row_of(v)
                if prev is not None:
                    px, pr = prev
                    dx = max(1, x - px)
                    steps = max(dx, abs(r - pr))
                    for k in range(1, steps + 1):
                        xx = px + round((x - px) * k / steps)
                        yy = pr + round((r - pr) * k / steps)
                        self.put(scr, yy, xx, self.g["vline"], attr)
                prev = (x, r)
            if prev is not None:
                self.put(scr, prev[1], prev[0], s.marker,
                         attr | curses.A_BOLD)

        # ось X: шкала времени
        ax_y = plot_bot + 1
        self.put(scr, ax_y, plot_x, self.g["hfill"] * (plot_w - 1),
                 curses.A_DIM)
        if view:
            dur = view[-1]["mono"] - view[0]["mono"]
            long_span = dur > 900
            fmt = "%H:%M" if long_span else "%H:%M:%S"
            label_len = 5 if long_span else 8
            step = max(label_len + 3, plot_w // 5)
            for tick in range(plot_x + 2, plot_x + plot_w - 1, step):
                i = round((tick - plot_x) * (n - 1) / max(1, xs[-1] - xs[0]))                     if n > 1 else 0
                i = max(0, min(n - 1, i))
                ts = time.strftime(fmt, time.localtime(view[i]["t"]))
                x = min(tick, w - 1 - label_len)
                self.put(scr, ax_y + 1, x, ts, curses.A_DIM)
                self.put(scr, ax_y, x + min(label_len, 8) // 2, "┬",
                         curses.A_DIM)

        # легенда
        ly = h - 2
        lx = 1
        for s in active:
            tag = f" {s.marker} {s.name} "
            if lx + len(tag) >= w - 1:
                break
            self.put(scr, ly, lx, tag,
                     curses.color_pair(s.color) | curses.A_BOLD)
            lx += len(tag) + 1
        if not active:
            self.put(scr, ly, 1, "все серии выключены (1..7)", curses.A_DIM)

        self.footer(scr)
        scr.refresh()

    def nice_scale(self, lo, hi, max_lines=6):
        """Круглые границы и шаг так, чтобы подписи влезли в max_lines строк."""
        lo = max(0.0, lo)
        if hi - lo < 4:
            mid = (lo + hi) / 2
            lo, hi = mid - 2, mid + 2
        candidates = [1, 2, 5, 10, 20, 25, 50, 100]
        ideal = (hi - lo) / 4.0
        step = min(candidates, key=lambda c: abs(c - ideal))
        for cand in candidates[candidates.index(step):]:
            lo2 = int(lo // cand * cand)
            hi2 = lo2 + int(-(-(hi - lo2) // cand) * cand)
            if (hi2 - lo2) / cand + 1 <= max_lines and hi2 - lo2 >= 2 * cand:
                return lo2, hi2, cand
        step = candidates[-1]
        lo2 = int(lo // step * step)
        hi2 = lo2 + int(-(-(hi - lo2) // step) * step)
        return lo2, hi2, step

    def footer(self, scr):
        h, w = scr.getmaxyx()
        txt = ("q выход  ←/→ скролл  Home/End  Space пауза  "
               "+/- интервал  1..7 серии  r сброс")
        if w < 90:
            txt = "q выход ←/→ скролл Space пауза +/- интервал 1..7 серии"
        self.put(scr, h - 1, 0, txt.ljust(w - 1)[: w - 1],
                 curses.color_pair(4) | curses.A_REVERSE)

    # --- главный цикл ------------------------------------------------------
    def run(self, scr):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        scr.nodelay(False)
        scr.keypad(True)
        if curses.has_colors() and not self.args.no_color:
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            curses.init_pair(1, curses.COLOR_GREEN, bg)
            curses.init_pair(2, curses.COLOR_YELLOW, bg)
            curses.init_pair(3, curses.COLOR_RED, bg)
            curses.init_pair(4, curses.COLOR_CYAN, bg)
            curses.init_pair(5, curses.COLOR_MAGENTA, bg)
            curses.init_pair(6, curses.COLOR_BLUE, bg)
            curses.init_pair(7, curses.COLOR_WHITE, bg)

        self.sample()
        self.next_sample = time.monotonic() + self.interval

        while self.running:
            now = time.monotonic()
            if not self.paused and now >= self.next_sample:
                self.sample()
                # без накопления дрейфа, но и без «догоняющих» пачек опросов
                self.next_sample += self.interval
                if self.next_sample < now:
                    self.next_sample = now + self.interval
                self.draw(scr)

            if self.args.duration and now - self.start >= self.args.duration:
                break

            wait = 0.1 if self.paused else max(0.0, self.next_sample - now)
            wait = min(wait, 1.0)
            scr.timeout(int(wait * 1000) if wait > 0 else 0)
            try:
                ch = scr.getch()
            except curses.error:
                ch = -1
            if ch != -1:
                self.handle(ch, scr)

    def handle(self, ch, scr):
        if ch in (ord("q"), 27):
            self.running = False
        elif ch == curses.KEY_LEFT:
            total = len(self.session.samples)
            end = total if self.follow else (self.frozen_end or total)
            self.frozen_end = max(self.shift(), end - self.shift())
            self.follow = self.frozen_end >= total
        elif ch == curses.KEY_RIGHT:
            total = len(self.session.samples)
            end = total if self.follow else (self.frozen_end or total)
            new_end = end + self.shift()
            if new_end >= total:
                self.follow = True
                self.frozen_end = None
            else:
                self.frozen_end = new_end
                self.follow = False
        elif ch == curses.KEY_HOME:
            self.frozen_end = max(self.shift(), 1)
            self.follow = False
        elif ch == curses.KEY_END:
            self.frozen_end = None
            self.follow = True
        elif ch == ord(" "):
            self.paused = not self.paused
            self.next_sample = time.monotonic() + self.interval
        elif ch in (ord("+"), ord("=")):
            self.interval = min(60, self.interval + 1)
            self.next_sample = time.monotonic() + self.interval
        elif ch in (ord("-"), ord("_")):
            self.interval = max(1, self.interval - 1)
            self.next_sample = time.monotonic() + self.interval
        elif ch == ord("r"):
            self.session.reset_stats()
        elif ord("1") <= ch <= ord("7"):
            idx = ch - ord("0")
            for s in self.series:
                if s.index == idx:
                    s.on = not s.on
        elif ch == curses.KEY_RESIZE:
            pass
        self.draw(scr)

    def shift(self):
        return max(1, (getattr(self, "cols", 100) - 7) // 4)


# ------------------------------------------------------------------------ main

def headless(series, log, interval):
    """Разовый вывод — для скриптов и проверки без TUI."""
    prev_total, prev_idle = cpu_total_jiffies()
    time.sleep(0.2)
    total, idle = cpu_total_jiffies()
    dt = total - prev_total
    load = 100.0 * (dt - (idle - prev_idle)) / dt if dt > 0 else 0.0
    print(f"{'датчик':24} {'°C':>7}  warn/crit")
    for s in series:
        v = s.read()
        txt = "   —" if v is None else f"{v:7.1f}"
        print(f"{s.name:24} {txt}  {s.warn}/{s.crit}")
    freqs = cpu_freqs()
    print(f"{'частота':24} {' / '.join(str(f) for f in freqs)} МГц")
    print(f"{'нагрузка CPU':24} {load:6.1f} %")
    pw = battery_power_w()
    if pw:
        print(f"{'питание':24} {pw:6.2f} Вт")
    print(f"{'RAM':24}    нет датчика в системе")
    if log.path:
        smp = {"t": time.time(), "load": load,
               "freq": max(cpu_freqs() or [0]), "pw": pw}
        for s in series:
            smp[s.id] = s.read()
        log.write(smp)
        print(f"лог: {log.path}")


def main() -> int:
    ap = argparse.ArgumentParser(description="TUI-монитор температур (CPU/SSD)")
    ap.add_argument("--interval", type=int, default=2,
                    help="интервал опроса, секунд (по умолчанию 2)")
    ap.add_argument("--duration", type=int, default=0,
                    help="автовыход через N секунд (0 — до нажатия q)")
    ap.add_argument("--once", action="store_true", help="разовый вывод без TUI")
    ap.add_argument("--log-file", default=None,
                    help="свой путь к CSV-логу (по умолчанию tmpfs)")
    ap.add_argument("--keep-log", action="store_true",
                    help="не удалять логи прошлых сессий при старте")
    ap.add_argument("--ascii", action="store_true", help="только ASCII-символы")
    ap.add_argument("--no-color", action="store_true", help="без цвета")
    args = ap.parse_args()

    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass

    series = discover_series()
    if not series:
        print("Не найдено ни одного датчика температуры.", file=sys.stderr)
        return 1
    session = Session(series)
    log = CsvLog(series, args.log_file, args.keep_log)

    def restore(signum, frame):
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, restore)
        except (ValueError, OSError):
            pass

    try:
        if args.once:
            headless(series, log, args.interval)
            return 0
        app = App(args, series, session, log)
        curses.wrapper(app.run)
        return 0
    except KeyboardInterrupt:
        return 0
    except curses.error as exc:
        print(f"Не удалось запустить TUI: {exc}\n"
              f"Нужен интерактивный терминал с поддержкой curses "
              f"(TERM={os.environ.get('TERM', '—')}).\n"
              f"Для скриптов используйте --once.", file=sys.stderr)
        return 1
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
