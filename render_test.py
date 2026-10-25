#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Оффлайн-проверка вёрстки: рисуем TUI в фейковый экран и печатаем кадры."""
import curses
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import thermtui as T

curses.color_pair = lambda n: n
curses.curs_set = lambda n: None
curses.has_colors = lambda: False
curses.A_DIM = 0
curses.A_BOLD = 0
curses.A_UNDERLINE = 0
curses.A_REVERSE = 0


class FakeScr:
    def __init__(self, h, w, keys):
        self.h, self.w = h, w
        self.keys = list(keys)          # [(номер вызова getch, клавиша), ...]
        self.calls = 0
        self.frames = 0
        self.erase()

    def erase(self):
        self.grid = [[" "] * (self.w - 1) for _ in range(self.h)]

    def getmaxyx(self):
        return self.h, self.w

    def addstr(self, y, x, s, attr=0):
        if y < 0 or y >= self.h:
            return
        for i, ch in enumerate(s):
            xx = x + i
            if xx >= self.w - 1:
                break
            self.grid[y][xx] = ch

    def refresh(self):
        self.frames += 1

    def timeout(self, ms):
        pass

    def nodelay(self, flag):
        pass

    def keypad(self, flag):
        pass

    def getch(self):
        self.calls += 1
        time.sleep(0.03)
        for i, (at, key) in enumerate(self.keys):
            if self.calls >= at:
                self.keys.pop(i)
                self.calls = 0
                return key
        return -1

    def curs_set(self, n):
        pass

    def dump(self):
        return "\n".join("".join(r).rstrip() for r in self.grid)


def prefill(sess, series, n=200, span=400.0):
    """Синтетическая история, чтобы проверить прокрутку и график."""
    import math
    now = time.time()
    for i in range(n):
        smp = {"t": now - span + span * i / n,
               "mono": time.monotonic() - span + span * i / n,
               "freq": 1900, "freqs": [1900, 1900], "load": 50.0, "pw": 8.0}
        for j, s in enumerate(series):
            base = {"cpu_pkg": 55, "nvme": 32, "tcpu": 56, "acpi": 48}.get(s.id, 40)
            smp[s.id] = base + 6 * math.sin(i / 7 + j) + 2 * math.cos(i / 3)
        sess.add(smp)


def run(h, w, keys, note, npre=0):
    series = T.discover_series()
    sess = T.Session(series)
    if npre:
        prefill(sess, series, npre)
    log = T.CsvLog(series, "/tmp/opencode/test-render.csv", keep=True)
    args = types.SimpleNamespace(interval=1, duration=0, once=False, ascii=False,
                                 no_color=False)
    app = T.App(args, series, sess, log)
    app.start = time.monotonic()
    scr = FakeScr(h, w, keys)
    try:
        app.run(scr)
    finally:
        log.close()
    print(f"=== {w}x{h} — {note} ===")
    print(scr.dump())
    print(f"--- сэмплов {len(sess.samples)}, кадров {scr.frames}\n")


if __name__ == "__main__":
    run(32, 100, [(40, ord("q"))], "live, 100x32")
    run(24, 80, [(30, curses.KEY_LEFT), (60, curses.KEY_LEFT),
                 (90, ord("q"))], "прокрутка влево, 80x24", npre=240)
    run(20, 66, [(20, ord("3")), (40, ord("q"))], "компакт, серия 3 выкл, 66x20")
