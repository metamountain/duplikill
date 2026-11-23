"""Duplikill main window: start -> search -> overview (net) -> review, black and white, pixel font."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback

import numpy as np
from PySide6.QtCore import QEvent, QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QKeySequence, QLinearGradient, QPainter, QShortcut
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QStackedWidget, QWidget

from . import __version__, czkawka, pixelfont, report, session, verify
from .actions import Mover
from .binview import BinView
from .index import Photo, data_dir, human
from .nebula import Nebula
from .review import AT_END, PER_GROUP, Review
from .thumbs import Thumbs

WHITE, GREY = QColor(255, 255, 255), QColor(150, 150, 150)
SETTINGS = os.path.join(data_dir(), "settings.json")
DEFAULTS = {"action": "move",          # move | trash | delete
            "target_mode": "central",  # central: <drive>\\Duplikill\\<path> | folder: <scan>\\Duplikill\\<path> | custom
            "target": "",              # the folder for target_mode custom
            "fire": PER_GROUP, "pan": "middle", "max_diff": 12, "hash_size": 8,
            "last_root": "", "window": "fullscreen",         # window: fullscreen | window
            "excluded": [],            # folders never scanned (settings, big-folder screen, best-of question)
            "bestof_kept": []}         # looked like best-of folders, the user wants them scanned anyway
SLIDER_MAX = 17                         # hash size 8: user-tested 2026-11-22, above 17 unrelated images get grouped


def load_settings() -> dict:
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            saved = json.load(fh)
        if "hash_size" not in saved:          # old scale (hash size 16): start fresh
            saved.pop("max_diff", None)
        s.update(saved)
    except (OSError, ValueError):
        pass
    return s


def save_settings(s: dict):
    with open(SETTINGS, "w", encoding="utf-8") as fh:
        json.dump(s, fh, indent=1, ensure_ascii=False)


class PixelScreen(QWidget):
    """Black screen with centred pixel-font lines; subclasses fill lines()."""
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.blink = QTimer(self, interval=500, timeout=self.update)
        self.blink.start()
        self._t_in = 0.0                                   # fly-in animation start
        self._anim = QTimer(self, interval=16, timeout=self._anim_step)

    FLY_IN = 0.3                                           # seconds

    def showEvent(self, e):
        self._t_in = time.time()
        self._anim.start()
        super().showEvent(e)

    def _anim_step(self):
        if time.time() - self._t_in > self.FLY_IN:
            self._anim.stop()
        self.update()

    def _fly_offset(self) -> int:
        u = min(1.0, (time.time() - self._t_in) / self.FLY_IN)
        return int(round((1 - u) ** 3 * self.height()))   # ease-out cubic, whole pixels

    def lines(self) -> list[str]:
        return []

    def _paint_bg(self, qp: QPainter):
        bg = QLinearGradient(0, 0, 0, self.height())
        bg.setColorAt(0, QColor(20, 20, 20))
        bg.setColorAt(1, QColor(0, 0, 0))
        qp.fillRect(self.rect(), bg)

    def _draw_cursor(self, qp: QPainter, x: int, y: int, ts: int):
        if int(time.time() * 2) % 2 == 0:
            qp.drawImage(QPoint(x, y), pixelfont.render([" "], ts, WHITE, WHITE))

    def paintEvent(self, e):
        qp = QPainter(self)
        self._paint_bg(qp)
        lines = self.lines()
        if not lines:
            return
        cols = max(len(l) for l in lines)
        ts = max(2, int(0.6 * min(self.width(), self.height() * 1.6) / (cols * 8)))
        img = pixelfont.render(lines, ts)
        tl = QPoint((self.width() - img.width()) // 2, (self.height() - img.height()) // 2 + self._fly_offset())
        qp.drawImage(tl, img)
        self._draw_cursor(qp, tl.x(), tl.y() + img.height(), ts)
        self._geo = (tl.x(), tl.y(), img.width(), 8 * ts)

    def line_at(self, e) -> int:
        """Index of the text line under the mouse, -1 outside."""
        geo = getattr(self, "_geo", None)
        if geo is None:
            return -1
        x0, y0, w, lh = geo
        x, y = e.position().x(), e.position().y()
        if not x0 <= x <= x0 + w or y < y0:
            return -1
        return int((y - y0) // lh)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()


class PixelSlider(QWidget):
    """STRICT <######......> LOOSE - drag or wheel; emits changed(value) after a short pause."""
    changed = Signal(int)

    def __init__(self, parent, value: int, maximum: int = SLIDER_MAX):
        super().__init__(parent)
        self.value, self.maximum = value, maximum
        self.busy = False
        self.cells = 24
        self._debounce = QTimer(self, singleShot=True, interval=150,
                                timeout=lambda: self.changed.emit(self.value))
        self.setCursor(Qt.SizeHorCursor)

    def ts(self) -> int:
        top = self.window()
        return max(2, min(top.width(), top.height()) // 450)

    def _lines(self) -> list[str]:
        filled = round(self.value / self.maximum * self.cells)
        bar = "#" * filled + "." * (self.cells - filled)
        return [f"STRICT <{bar}> {'BUSY.' if self.busy else 'LOOSE'}"]     # no number: the bar says it

    def place(self, left_cells: int = 0, right_cells: int = 0):
        """Centred at the bottom, but never over the buttons left (left_cells) and right (right_cells)."""
        ts = self.ts()
        par = self.parentWidget()
        cell = 8 * ts
        room = par.width() // cell - left_cells - right_cells - 4
        self.cells = max(8, min(24, room - len("STRICT <> LOOSE")))
        w, h = len(self._lines()[0]) * cell, cell
        x = max((par.width() - w) // 2, (left_cells + 2) * cell)
        x = min(x, par.width() - (right_cells + 2) * cell - w)
        self.setGeometry(x, par.height() - h - 3 * ts, w, h)

    def _bar_x(self) -> tuple[int, int]:
        ts = self.ts()
        start = len("STRICT <") * 8 * ts
        return start, start + self.cells * 8 * ts

    def set_value(self, v: int):
        v = max(0, min(self.maximum, int(v)))
        if v != self.value:
            self.value = v
            self.update()
            self._debounce.start()

    def paintEvent(self, e):
        qp = QPainter(self)
        qp.drawImage(QPoint(0, 0), pixelfont.render(self._lines(), self.ts(), WHITE, QColor(0, 0, 0, 255)))

    def _from_x(self, x: float):
        a, b = self._bar_x()
        self.set_value(round((x - a) / max(1, b - a) * self.maximum))

    def mousePressEvent(self, e):
        self._from_x(e.position().x())

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton:
            self._from_x(e.position().x())

    def wheelEvent(self, e):
        self.set_value(self.value + (1 if e.angleDelta().y() > 0 else -1))


class PixelButton(QWidget):
    """Pixel-font text that can be clicked (white on black, no frame)."""
    clicked = Signal()

    def __init__(self, parent, text: str):
        super().__init__(parent)
        self.text = text
        self.setCursor(Qt.PointingHandCursor)

    def ts(self) -> int:
        top = self.window()
        return max(2, min(top.width(), top.height()) // 450)

    def place_bottom_left(self, x_cells: int = 0):
        ts = self.ts()
        w, h = len(self.text) * 8 * ts, 8 * ts
        par = self.parentWidget()
        self.setGeometry(3 * ts + x_cells * 8 * ts, par.height() - h - 3 * ts, w, h)

    def place_bottom_right(self, x_cells: int = 0):
        ts = self.ts()
        w, h = len(self.text) * 8 * ts, 8 * ts
        par = self.parentWidget()
        self.setGeometry(par.width() - w - 3 * ts - x_cells * 8 * ts, par.height() - h - 3 * ts, w, h)

    def paintEvent(self, e):
        QPainter(self).drawImage(QPoint(0, 0), pixelfont.render([self.text], self.ts(), WHITE, QColor(0, 0, 0, 255)))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()


def dark_title_bar(widget: QWidget):
    """Windows 10/11: dark grey title bar and frame for the normal window mode."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)          # immersive dark mode
        grey = ctypes.c_int(0x00202020)                                    # COLORREF 0x00BBGGRR
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(grey), 4)        # caption colour
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(grey), 4)        # border colour
        white = ctypes.c_int(0x00FFFFFF)
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(white), 4)       # caption text
    except Exception:
        pass


class StartScreen(PixelScreen):
    """Logo DUPLIKILL: its font pixels fly in from everywhere and snap into place, then intro_done."""
    intro_done = Signal()
    LOGO = "DUPLIKILL"
    INTRO = 0.7                     # seconds until the logo stands
    HOLD = 0.25                     # then a short pause before the folder dialog

    def __init__(self):
        super().__init__()
        self._intro_t0 = None
        self._intro_fired = False
        bm = pixelfont.bitmap([self.LOGO])
        ys, xs = np.nonzero(bm)
        self._px = np.stack([xs, ys], 1)                         # font pixels of the logo
        self._logo_size = (bm.shape[1], bm.shape[0])
        rng = np.random.default_rng()
        self._from = rng.random((len(xs), 2)) * 2 - 1             # start: anywhere around the screen
        self._delay = rng.random(len(xs)) * 0.35                  # not all at once

    def play_intro(self):
        self._intro_t0 = time.time()
        self._intro_fired = False
        self._anim.start()

    def _anim_step(self):
        if self._intro_t0 is not None:
            el = time.time() - self._intro_t0
            if el > self.INTRO + self.HOLD and not self._intro_fired:
                self._intro_fired = True
                self._anim.stop()
                self.intro_done.emit()
            self.update()
            return
        super()._anim_step()

    def lines(self):
        return ["CLICK OR ENTER:", "OPEN FOLDER", "", "READY."]

    def paintEvent(self, e):
        qp = QPainter(self)
        self._paint_bg(qp)
        W, H = self.width(), self.height()
        lw, lh = self._logo_size
        big = max(4, int(W * 0.7 / lw))                           # whole screen pixels per font pixel
        x0, y0 = (W - lw * big) // 2, int(H * 0.38) - lh * big // 2
        el = 9.9 if self._intro_t0 is None else time.time() - self._intro_t0
        u = np.clip((el - self._delay) / (self.INTRO - 0.35), 0, 1)
        k = 1 - (1 - u) ** 3                                       # ease-out: fast, then snaps in
        tx = x0 + self._px[:, 0] * big
        ty = y0 + self._px[:, 1] * big
        fx = W / 2 + self._from[:, 0] * W
        fy = H / 2 + self._from[:, 1] * H
        xs = np.rint(fx + (tx - fx) * k).astype(int)
        ys = np.rint(fy + (ty - fy) * k).astype(int)
        for x, y in zip(xs, ys):
            qp.fillRect(int(x), int(y), big, big, WHITE)
        self.logo_bottom = y0 + lh * big                           # the folder dialog opens below this
        self.logo_width = lw * big
        if el < self.INTRO:
            return
        lines = self.lines()
        ts = max(2, big // 3)
        img = pixelfont.render(lines, ts)
        tl = QPoint((W - img.width()) // 2, y0 + lh * big + 6 * ts)
        qp.drawImage(tl, img)
        self._draw_cursor(qp, tl.x(), tl.y() + img.height(), ts)


WARN_SECONDS = 90        # a scan longer than this is announced first (big folder: offer subfolders)


class SearchJob(QThread):
    counted = Signal(int, float)
    ask = Signal(int, float, float, list)    # images, seconds, cached share, [(subfolder, images, seconds)]
    done = Signal(list)
    failed = Signal(str)
    cancelled = Signal()
    restored = Signal(object)                # saved session, still up to date: no scan needed

    def __init__(self, root: str, options: czkawka.Options, sigs: verify.SigCache):
        super().__init__()
        self.root, self.options, self.sigs = root, options, sigs
        self.stop = threading.Event()
        self.go = threading.Event()
        self.confirm = True
        self.paths: list[str] | None = None
        self.saved: dict | None = None           # session.load() result to check against the folder
        self.fp: dict | None = None
        self.copies: list | None = None          # folder pairs with exact copies (full scans only)

    def _find_copies(self, groups):
        from . import copies
        try:
            self.copies = copies.find(groups, self.paths or [], stop=self.stop)
        except Exception as ex:                  # a merge offer is a bonus, never a reason to fail the scan
            print("copies:", ex, file=sys.stderr)
            self.copies = []

    def run(self):
        try:
            if self.confirm:
                paths = czkawka.list_images(self.root, self.options.excluded, self.stop)
                self.paths = paths
                if self.stop.is_set():
                    raise czkawka.Cancelled()
                self.fp = session.fingerprint(paths)
                if self.saved is not None and self.saved.get("fingerprint") == self.fp:
                    self._find_copies(self.saved["groups"])
                    self.restored.emit(self.saved)           # same images as last time: groups as saved
                    return
                n = len(paths)
                frac = czkawka.cached_fraction(paths, self.options)
                eta = czkawka.estimate_seconds(n, frac)
                if eta > WARN_SECONDS:
                    top_subs = czkawka.subfolders(self.root, paths)[:9]
                    by_top: dict[str, list[str]] = {name: [] for name, _ in top_subs}
                    root_prefix = self.root.rstrip("\\/") + os.sep
                    for p in paths:
                        if p.startswith(root_prefix):
                            rel = p[len(root_prefix):]
                            sep_idx = rel.find(os.sep)
                            if sep_idx > 0:
                                top = rel[:sep_idx]
                                bucket = by_top.get(top)
                                if bucket is not None:
                                    bucket.append(p)
                    sub_lists = [by_top[name] for name, _ in top_subs]
                    sub_fracs = czkawka.cached_fractions(sub_lists, self.options)
                    subs = [
                        (name, k, czkawka.estimate_seconds(k, sf))
                        for (name, k), sf in zip(top_subs, sub_fracs)
                    ]
                    self.ask.emit(n, eta, frac, subs)
                    self.go.wait()
                    if self.stop.is_set():
                        raise czkawka.Cancelled()
                self.counted.emit(n, eta)
            candidates = czkawka.search(self.root, self.options, self.stop)
            groups = verify.refine(candidates, self.sigs, self.stop)          # drop look-alikes
            if self.confirm and not self.stop.is_set():
                self._find_copies(groups)
            if self.stop.is_set():
                raise czkawka.Cancelled()
            self.done.emit(groups)
        except czkawka.Cancelled:
            self.cancelled.emit()
        except Exception as ex:  # shown to the user
            self.failed.emit(f"{ex}\n{traceback.format_exc()}")


def _dur(sec: float) -> str:
    return f"{max(1, round(sec / 60))} MIN" if sec >= 90 else f"{max(1, round(sec))} S"


class SearchScreen(PixelScreen):
    chosen = Signal(object)          # big-folder warning: "go" | "other" | subfolder index | ("x", index)

    def __init__(self):
        super().__init__()
        self.root, self.n, self.eta, self.t0, self.msg = "", -1, 0.0, 0.0, ""
        self.warn = None
        self._first_sub = -1

    def start(self, root: str):
        self.root, self.n, self.eta, self.t0, self.msg = root, -1, 0.0, time.time(), ""
        self.warn = None

    def lines(self):
        if self.warn is not None:
            n, eta, frac, subs = self.warn
            name = os.path.basename(self.root.rstrip("\\/")) or self.root
            out = ["WARNING: BIG FOLDER", "", name, "", f"{n} IMAGES", f"SCAN TAKES ABOUT {_dur(eta)}"]
            if frac > 0.02:
                out.append(f"({frac * 100:.0f} % ALREADY KNOWN)")
            out += ["", "ENTER: GO ON, SCAN WHOLE FOLDER"]
            self._first_sub = -1
            if subs:
                out += ["", "OR A SUBFOLDER (CLICK OR KEY 1-9),", "RIGHT CLICK / SHIFT+1-9: EXCLUDE IT", ""]
                w = min(22, max(len(pixelfont.plain(s[0])) for s in subs))
                self._first_sub = len(out)
                out += [f"{i + 1} {pixelfont.plain(nm)[:w].ljust(w)} {k:6d}  {_dur(sec)}"
                        for i, (nm, k, sec) in enumerate(subs)]
            out += ["", "ESC: CHOOSE ANOTHER FOLDER"]
            return out
        el = time.time() - self.t0
        out = ["SEARCHING", "", os.path.basename(self.root.rstrip("\\/")) or self.root, ""]
        if self.n < 0:
            out.append("COUNTING IMAGES ...")
        else:
            frac = min(0.95, el / max(1.0, self.eta))
            bar = 20
            out += [f"{self.n} IMAGES", "#" * int(frac * bar) + "." * (bar - int(frac * bar))]
        out += ["", f"{int(el) // 60:02d}:{int(el) % 60:02d}", "", "ESC: CANCEL"]
        if self.msg:
            out += ["", self.msg[:40]]
        return out

    def mousePressEvent(self, e):
        if self.warn is None or self._first_sub < 0:
            return
        k = self.line_at(e) - self._first_sub
        if 0 <= k < len(self.warn[3]):
            self.chosen.emit(("x", k) if e.button() == Qt.RightButton else k)


class BestOfScreen(PixelScreen):
    """After a scan: 'this folder holds mostly copies of other folders - a best-of? exclude it?'"""
    answered = Signal(str)            # "exclude" | "keep" | "later"

    def __init__(self):
        super().__init__()
        self.item, self.pos, self.total = ("", 0, 0), 0, 0

    def ask(self, item: tuple[str, int, int], pos: int, total: int):
        self.item, self.pos, self.total = item, pos, total
        self.update()

    def lines(self):
        folder, c, n = self.item
        name = pixelfont.plain(os.path.basename(folder.rstrip("\\/")) or folder).upper()[:36]
        out = ["BEST-OF FOLDER?", "", name, "", f"{c} OF {n} IMAGES ARE ALSO", "IN OTHER FOLDERS.", "",
               "X: EXCLUDE IT, DUPLICATES THERE ARE FINE", "ENTER: KEEP SCANNING IT",
               "ESC: DECIDE LATER"]
        if self.total > 1:
            out += ["", f"({self.pos + 1} OF {self.total})"]
        return out

    def mousePressEvent(self, e):
        line = self.line_at(e)
        answer = {7: "exclude", 8: "keep", 9: "later"}.get(line)
        if answer and e.button() == Qt.LeftButton:
            self.answered.emit(answer)


def _short(folder: str, w: int = 30) -> str:
    return pixelfont.plain(os.path.basename(folder.rstrip("\\/")) or folder).upper()[:w]


class MergeScreen(PixelScreen):
    """After a scan: 'these two folders share N exact copies - merge them?' (one key instead of N groups)."""
    answered = Signal(str)            # "merge" | "flip" | "bestof" | "later"

    def __init__(self):
        super().__init__()
        self.m, self.pos, self.total = None, 0, 0
        self._keys: dict[int, str] = {}

    def ask(self, m, pos: int, total: int):
        self.m, self.pos, self.total = m, pos, total
        self.update()

    def lines(self):
        m = self.m
        if m is None:
            return []
        k, o = _short(m.keep), _short(m.other)
        w = max(len(k), len(o))
        out = ["EXACT COPIES - MERGE?", "", f"{m.count} PICTURES ARE IN BOTH FOLDERS:", "",
               f"{k.ljust(w)}  {m.count} OF {m.n_keep}", f"{o.ljust(w)}  {m.count} OF {m.n_other}", ""]
        if m.swaps:
            out += [f"{m.swaps} COPIES IN {o} HAVE MORE TAGS:", f"THEY MOVE INTO {k}, TAGS STAY.", ""]
        first = len(out)
        out += [f"ENTER: KEEP THEM IN {k}", f"       REMOVE {m.count} COPIES FROM {o}",
                f"T: THE OTHER WAY ROUND", "B: BEST-OF, KEEP BOTH (EXCLUDE FOLDER)", "ESC: DECIDE LATER"]
        self._keys = {first: "merge", first + 1: "merge", first + 2: "flip", first + 3: "bestof", first + 4: "later"}
        if self.total > 1:
            out += ["", f"({self.pos + 1} OF {self.total})"]
        return out

    def mousePressEvent(self, e):
        answer = self._keys.get(self.line_at(e))
        if answer and e.button() == Qt.LeftButton:
            self.answered.emit(answer)


class SettingsScreen(PixelScreen):
    """Fixed-grid pixel settings screen with grey help lines; never shifts or rescales when cycling."""
    changed = Signal(str)                  # the key that changed
    COLS = 48
    N_LINES = 28
    MAX_EX = 4                             # excluded folders listed (the rest as "+ N MORE")

    CHOICES = {
        "window": [("fullscreen", "FULLSCREEN"), ("window", "WINDOW")],
        "action": [("move", "MOVE"), ("trash", "RECYCLE BIN"), ("delete", "DELETE FOREVER")],
        "target_mode": [("central", "CENTRAL"), ("folder", "IN SCANNED FOLDER"), ("custom", "OWN FOLDER")],
        "fire": [(PER_GROUP, "EACH GROUP"), (AT_END, "AT THE END")],
        "pan": [("middle", "MIDDLE BUTTON"), ("right", "RIGHT BUTTON")],
    }
    HELP = {
        "window": {
            "fullscreen": "BORDERLESS FULLSCREEN (F11 TOGGLES)",
            "window": "RESIZABLE DARK WINDOW (F11 TOGGLES)",
        },
        "action": {
            "move": "FAST MOVE + XMP SIDECARS, CTRL+Z UNDO",
            "trash": "WINDOWS RECYCLE BIN + XMP SIDECARS",
            "delete": "PERMANENTLY DELETE FILES (ASKS ONCE)",
        },
        "fire": {
            PER_GROUP: "DEL REMOVES MARKED & OPENS NEXT GROUP",
            AT_END: "MARK FIRST, PRESS DEL IN OVERVIEW",
        },
        "pan": {
            "middle": "MIDDLE DRAG = PAN, RIGHT HOLD = BLINK",
            "right": "RIGHT DRAG = PAN, MIDDLE HOLD = BLINK",
        },
    }
    ROWS = [
        ("window", "WINDOW"),
        ("action", "REMOVE BY"),
        ("target_mode", "MOVE TO"),
        ("fire", "DELETE"),
        ("pan", "MOUSE PAN"),
    ]

    def __init__(self, settings: dict):
        super().__init__()
        self.settings = settings
        self._geo = None
        self._stats_cache: tuple[int, int] | None = None
        self._ex_line = -99
        self._bin = (0, 0)

    def refresh_bin(self):
        items = Mover().bin_items()
        self._bin = (len(items), sum(i["size"] for i in items))
        self.update()

    def showEvent(self, e):
        self._stats_cache = Mover().stats()
        self.refresh_bin()
        super().showEvent(e)

    def _value(self, key: str) -> str:
        return dict(self.CHOICES[key]).get(self.settings.get(key), "?")

    def _where(self) -> str:
        if self.settings.get("action", "move") != "move":
            return "(ONLY USED WHEN REMOVE BY = MOVE)"
        m = self.settings.get("target_mode", "central")
        if m == "central":
            return "<DRIVE>:\\DUPLIKILL-BIN\\<ORIGINAL PATH>"
        if m == "folder":
            return "<SCANNED FOLDER>\\DUPLIKILL-BIN\\<PATH>"
        return ("FOLDER: " + (self.settings.get("target") or "(NONE)").upper())[:44]

    def _help(self, key: str) -> str:
        if key == "target_mode":
            return self._where()
        cur = self.settings.get(key)
        return self.HELP.get(key, {}).get(cur, "")

    def _rows_with_color(self) -> list[tuple[str, QColor]]:
        if self._stats_cache is None:
            self._stats_cache = Mover().stats()
        n_moved, b_moved = self._stats_cache
        st = report.load_stats()
        out: list[tuple[str, QColor]] = [("SETTINGS", WHITE), ("", WHITE)]
        for n, (key, label) in enumerate(self.ROWS, 1):
            out.append((f"{n} {label.ljust(11)} {self._value(key)}"[:self.COLS].ljust(self.COLS), WHITE))
            out.append((f"  {self._help(key)}"[:self.COLS].ljust(self.COLS), GREY))
        ex = self.settings.get("excluded", [])
        self._ex_line = len(out)
        out.append((f"{len(self.ROWS) + 1} {'EXCLUDED'.ljust(11)} {len(ex)} FOLDER(S)".ljust(self.COLS), WHITE))
        out.append((("  CLICK: ADD A FOLDER" + ("   CLICK ONE BELOW: REMOVE" if ex else "")).ljust(self.COLS), GREY))
        for d in ex[:self.MAX_EX]:
            t = pixelfont.plain(d).upper()
            t = t if len(t) <= self.COLS - 6 else "..." + t[-(self.COLS - 9):]
            out.append((f"  [X] {t}".ljust(self.COLS), GREY))
        if len(ex) > self.MAX_EX:
            out.append((f"  + {len(ex) - self.MAX_EX} MORE (SETTINGS.JSON)".ljust(self.COLS), GREY))
        out += [
            ("", WHITE),
            ("CLICK=CROSS  DEL/BACKSPACE=DELETE CROSSED".ljust(self.COLS), GREY),
            ("SPACE/DOWN=NEXT GROUP  UP=PREVIOUS  WHEEL=ZOOM".ljust(self.COLS), GREY),
            (f"FREED SO FAR: {st['images']} IMAGES, {human(max(0, st['bytes']))}"[:self.COLS].ljust(self.COLS), WHITE),
            ("  CLICK: SHARE THESE NUMBERS (GITHUB)   F8: BUG".ljust(self.COLS), GREY),
            (f"BIN: {self._bin[0]} IMAGES, {human(self._bin[1])}"[:self.COLS].ljust(self.COLS), WHITE),
            ("  CLICK OR B: LOOK AT THEM, RESTORE SINGLE ONES".ljust(self.COLS), GREY),
            ("", WHITE),
            (f"KEY 1-{len(self.ROWS) + 1} / CLICK: CHANGE   F1: HELP   ESC: BACK"[:self.COLS].ljust(self.COLS), WHITE),
        ]
        return out

    def lines(self) -> list[str]:
        return [text.ljust(self.COLS) for text, _ in self._rows_with_color()]

    def paintEvent(self, e):
        qp = QPainter(self)
        self._paint_bg(qp)
        rows = self._rows_with_color()
        ts = max(2, int(0.75 * min(self.width() / (self.COLS * 8), self.height() / (self.N_LINES * 8))))
        lh = 8 * ts
        w = self.COLS * lh
        h = len(rows) * lh
        x0 = (self.width() - w) // 2
        y0 = (self.height() - h) // 2 + self._fly_offset()
        for idx, (text, color) in enumerate(rows):
            if text.strip():
                img = pixelfont.render([text.ljust(self.COLS)], ts, color)
                qp.drawImage(QPoint(x0, y0 + idx * lh), img)
        self._draw_cursor(qp, x0, y0 + h, ts)
        self._geo = (x0, y0, w, lh)

    def cycle(self, n: int):
        """Next choice of row n (1-based)."""
        if n == len(self.ROWS) + 1:
            self.changed.emit("excluded_add")
            return
        if not 1 <= n <= len(self.ROWS):
            return
        key = self.ROWS[n - 1][0]
        opts = [v for v, _ in self.CHOICES[key]]
        cur = self.settings.get(key)
        self.settings[key] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
        self.changed.emit(key)
        self.update()

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or self._geo is None:
            return
        x0, top, w, lh = self._geo
        if not (x0 <= e.position().x() <= x0 + w):
            return
        line = int((e.position().y() - top) // lh)
        if 2 <= line < 2 + 2 * len(self.ROWS):
            self.cycle((line - 2) // 2 + 1)
        elif line in (len(self._rows_with_color()) - 4, len(self._rows_with_color()) - 3):
            self.changed.emit("open_bin")
        elif line in (len(self._rows_with_color()) - 6, len(self._rows_with_color()) - 5):
            self.changed.emit("share_stats")
        elif line in (self._ex_line, self._ex_line + 1):
            self.cycle(len(self.ROWS) + 1)
        elif 0 <= line - self._ex_line - 2 < min(self.MAX_EX, len(self.settings.get("excluded", []))):
            self.changed.emit(f"excluded_del:{line - self._ex_line - 2}")


class HelpScreen(PixelScreen):
    """F1 help: the core keys first in double size, then the full reference in white/grey."""
    COLS = 52
    N_LINES = 30                            # core lines count twice

    CORE = [                                # max 26 characters: drawn at double size
        "CLICK IMAGE: CROSS IT",
        "DEL / BACKSPACE: DELETE",
        "   ALL CROSSED, NEXT GROUP",
        "SPACE / DOWN: NEXT GROUP",
        "UP: PREVIOUS GROUP",
    ]
    ITEMS = [
        ("", WHITE),
        ("REVIEW (IMAGE COMPARE)", WHITE),
        ("RIGHT / PAGE DOWN    ", "NEXT GROUP (LIKE SPACE)"),
        ("LEFT / PAGE UP       ", "PREVIOUS GROUP (LIKE UP)"),
        ("SKULL ON AN IMAGE    ", "REMOVE THIS ONE IMAGE AT ONCE"),
        ("ONLY / KEEP BEST     ", "CROSS OUT ALL OTHER IMAGES"),
        ("MOUSE WHEEL / 0      ", "ZOOM ALL AT CURSOR / FIT"),
        ("MIDDLE / RIGHT MOUSE ", "DRAG = PAN, HOLD = BLINK"),
        ("TAB / SIDE BUTTONS   ", "ONE IMAGE ALONE / NEXT ONE"),
        ("CTRL+Z               ", "UNDO LAST DELETE"),
        ("ESC                  ", "BACK TO THE OVERVIEW"),
        ("", WHITE),
        ("OVERVIEW & GENERAL", WHITE),
        ("CLICK GROUP / ENTER  ", "OPEN GROUP"),
        ("STRICT <###..> LOOSE ", "SIMILARITY, SEARCHES AT ONCE"),
        ("S / F11 / F1         ", "SETTINGS / FULLSCREEN / HELP"),
        ("B                    ", "BIN: SEE + RESTORE REMOVED ONES"),
        ("U / F8               ", "INSTALL UPDATE / REPORT A BUG"),
        ("ESC (2X ON START)    ", "ONE STEP BACK (QUIT ON START)"),
        ("", WHITE),
        ("F1 / ESC / CLICK: BACK", WHITE),
    ]

    def lines(self) -> list[str]:
        out = [l.ljust(self.COLS) for l in self.CORE]
        for item in self.ITEMS:
            if isinstance(item[1], QColor):
                out.append(item[0].ljust(self.COLS))
            else:
                out.append((item[0] + item[1])[:self.COLS].ljust(self.COLS))
        return out

    def paintEvent(self, e):
        qp = QPainter(self)
        self._paint_bg(qp)
        ts = max(1, int(0.85 * min(self.width() / (self.COLS * 8), self.height() / (self.N_LINES * 8))))
        lh = 8 * ts
        w = self.COLS * lh
        h = (2 * len(self.CORE) + len(self.ITEMS)) * lh
        x0 = (self.width() - w) // 2
        y0 = (self.height() - h) // 2 + self._fly_offset()
        for idx, text in enumerate(self.CORE):              # the core: double size, white
            qp.drawImage(QPoint(x0, y0 + idx * 2 * lh), pixelfont.render([text], 2 * ts, WHITE))
        y1 = y0 + 2 * len(self.CORE) * lh
        for idx, item in enumerate(self.ITEMS):
            y = y1 + idx * lh
            if isinstance(item[1], QColor):
                text, col = item
                if text.strip():
                    qp.drawImage(QPoint(x0, y), pixelfont.render([text.ljust(self.COLS)], ts, col))
            else:
                k_txt, desc = item
                qp.drawImage(QPoint(x0, y), pixelfont.render([k_txt], ts, WHITE))
                qp.drawImage(QPoint(x0 + len(k_txt) * lh, y),
                             pixelfont.render([desc[:self.COLS - len(k_txt)].ljust(self.COLS - len(k_txt))], ts, GREY))
        self._draw_cursor(qp, x0, y0 + h, ts)


class Main(QMainWindow):
    update_found = Signal(object)          # updater.check() result, from a background thread
    update_progress = Signal(int, int, str)  # bytes done, total, installer path when finished ("" before)
    def __init__(self, dry_run: bool = False):
        super().__init__()
        self.setWindowTitle("Duplikill")
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        icon = os.path.join(base, "assets", "duplikill.png")
        if os.path.exists(icon):
            self.setWindowIcon(QIcon(icon))
        self.settings = load_settings()
        self.dry_run = dry_run
        self.thumbs = Thumbs()
        self.sigs = verify.SigCache()
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.settings["max_diff"] = min(SLIDER_MAX, int(self.settings["max_diff"]))
        self.slider = PixelSlider(self.stack, self.settings["max_diff"])
        self.slider.changed.connect(self._strictness)
        self.slider.hide()
        self.stack.currentChanged.connect(self._screen_changed)
        self.start = StartScreen()
        self.search = SearchScreen()
        self.search.chosen.connect(self._answer)
        self.settings_screen = SettingsScreen(self.settings)
        self.settings_screen.changed.connect(self._setting_changed)
        self.bestof_screen = BestOfScreen()
        self.bestof_screen.answered.connect(self._bestof_answer)
        self._bestof: list[tuple[str, int, int]] = []
        self._bestof_excluded = False
        self.merge_screen = MergeScreen()
        self.merge_screen.answered.connect(self._merge_answer)
        self._merges: list = []
        self._merge_n = 0
        self._rescan_after = False               # merged or excluded something: search the folder again
        self.help_screen = HelpScreen()
        self.help_screen.clicked.connect(self.close_help)
        self.stack.addWidget(self.start)
        self.stack.addWidget(self.search)
        self.stack.addWidget(self.settings_screen)
        self.stack.addWidget(self.help_screen)
        self.stack.addWidget(self.bestof_screen)
        self.stack.addWidget(self.merge_screen)
        self.bin_view = BinView(self.thumbs, Mover())
        self.bin_view.back.connect(self.close_bin)
        self.bin_view.restored.connect(self._restored)
        self.stack.addWidget(self.bin_view)
        self._bin_from = None
        self._bin_changed = False
        self._settings_from = None
        self._help_from = None
        self._delete_ok = False                  # 'delete forever' confirmed once per session
        self._hint_done = False
        self.start.clicked.connect(self.pick)
        self.start.intro_done.connect(self.pick)
        self.nebula: Nebula | None = None
        self.review: Review | None = None
        self.job: SearchJob | None = None
        self.history: list[tuple] = []   # (group index, moved pairs, (index, photo) for a skull or None)
        QShortcut(QKeySequence("Ctrl+Z"), self, self.undo)
        self.open_btn = PixelButton(self.stack, "OPEN FOLDER")
        self.open_btn.clicked.connect(self.pick)
        self.close_btn = PixelButton(self.stack, "CLOSE FOLDER")
        self.close_btn.clicked.connect(self.close_folder)
        self.open_btn.hide()
        self.close_btn.hide()
        self.settings_btn = PixelButton(self.stack, "SETTINGS")
        self.settings_btn.clicked.connect(self.open_settings)
        self.help_btn = PixelButton(self.stack, "F1 HELP")
        self.help_btn.clicked.connect(self.open_help)
        self.fire_all_btn = PixelButton(self.stack, "")
        self.fire_all_btn.clicked.connect(self.fire_all)
        self.fire_all_btn.hide()
        self.bug_btn = PixelButton(self.stack, "F8: REPORT BUG")   # after an error
        self.bug_btn.clicked.connect(self.report_bug)
        self.bug_btn.hide()
        self.update_btn = PixelButton(self.stack, "")        # appears when GitHub has a newer release
        self.update_btn.clicked.connect(self.start_update)
        self.update_btn.hide()
        self._update: dict | None = None
        self.update_found.connect(self._update_found)
        self.update_progress.connect(self._update_progress)
        if self.settings.get("check_updates", True) and getattr(sys, "frozen", False):
            threading.Thread(target=self._check_update, daemon=True).start()
        self.setFocusPolicy(Qt.StrongFocus)
        self._last_esc = 0.0
        QApplication.instance().installEventFilter(self)

    # ------------------------------------------------------------ search
    DIALOG_TITLE = "Duplikill - open folder"

    def pick(self):
        """Windows folder dialog, moved to the middle right below the logo (or the middle of the window)."""
        top = self.mapToGlobal(QPoint(0, 0)).y()
        if self.stack.currentWidget() is self.start and getattr(self.start, "logo_bottom", None):
            y = self.start.mapToGlobal(QPoint(0, self.start.logo_bottom)).y() + 24
            w = self.start.logo_width
        else:
            y = top + self.height() // 4
            w = int(self.width() * 0.7)
        h = max(360, top + self.height() - y - 40)              # fits into the window below the logo
        x = self.mapToGlobal(QPoint(self.width() // 2, 0)).x()
        self._dialog_at, self._dialog_tries = (x, y, w, h), 0
        self._dialog_timer = QTimer(self, interval=15, timeout=self._move_dialog)
        self._dialog_timer.start()
        root = QFileDialog.getExistingDirectory(self, self.DIALOG_TITLE, self.settings.get("last_root") or "")
        self._dialog_timer.stop()
        if root:
            self.run_search(os.path.normpath(root))

    def _move_dialog(self):
        """Runs while the native dialog is open: finds it by its title and centres it at _dialog_at."""
        self._dialog_tries += 1
        if self._dialog_tries > 200 or sys.platform != "win32":
            self._dialog_timer.stop()
            return
        import ctypes
        u32 = ctypes.windll.user32
        hwnd = u32.FindWindowW(None, self.DIALOG_TITLE)
        if not hwnd:
            return
        dpr = self.devicePixelRatioF()                        # Qt works in logical pixels, Windows in physical
        x, y, w, h = self._dialog_at
        u32.SetWindowPos(hwnd, 0, int((x - w / 2) * dpr), int(y * dpr), int(w * dpr), int(h * dpr), 0x0004)  # NOZORDER
        self._dialog_timer.stop()

    def run_search(self, root: str, quiet: bool = False):
        """quiet = new strictness: the overview stays, only the slider shows that it is searching."""
        self.root = root
        self.settings["last_root"] = root
        save_settings(self.settings)
        if self.job is not None and self.job.isRunning():
            self.job.stop.set()
            self.job.go.set()
            self.job.wait()
        if quiet:
            self.slider.busy = True
            self.slider.update()
        else:
            self.search.start(root)
            self.stack.setCurrentWidget(self.search)
        from .actions import auto_root
        drive = os.path.splitdrive(os.path.abspath(root))[0] + os.sep
        never = [auto_root(root), os.path.join(root, "Duplikill-Bin"), os.path.join(root, "Duplikill"),
                 os.path.join(drive, "Duplikill")] + \
            ([self.settings["target"]] if self.settings.get("target") else []) + list(self.settings.get("excluded", []))
        self.job = SearchJob(root, czkawka.Options(max_diff=int(self.settings["max_diff"]),
                                                   hash_size=int(self.settings["hash_size"]),
                                                   excluded=[d for d in never if os.path.isdir(d)]), self.sigs)
        if quiet:
            saved = session.load(root, int(self.settings["max_diff"]), int(self.settings["hash_size"]))
            if saved is not None and saved.get("fingerprint") == getattr(self, "_fp", None):
                self.slider.busy = False
                self._found(saved["groups"], saved)              # this strictness was saved: no scan
                return
        else:
            self.job.saved = session.load(root, int(self.settings["max_diff"]), int(self.settings["hash_size"]))
        self.job.restored.connect(lambda saved: self._found(saved["groups"], saved))
        self.job.counted.connect(self._counted)
        self.job.confirm = not quiet
        self.job.ask.connect(self._ask)
        self.job.done.connect(self._found)
        self.job.failed.connect(self._failed)
        self.job.cancelled.connect(lambda: None if quiet else self.stack.setCurrentWidget(self.start))
        self.job.start()

    def cancel(self):
        if self.job is not None:
            self.job.stop.set()
            self.job.go.set()

    # ------------------------------------------------------------ update
    def _check_update(self):
        from . import __version__, updater
        info = updater.check(__version__)
        if info:
            self.update_found.emit(info)

    def _update_found(self, info: dict):
        self._update = info
        self.update_btn.text = f"U: UPDATE TO V{info['version']}"
        self._screen_changed(0)

    def start_update(self):
        """Download Duplikill-Setup.exe in the background, then run it and quit (it starts Duplikill again)."""
        if not self._update or getattr(self, "_updating", False):
            return
        self._updating = True
        info = self._update

        def work():
            from . import updater
            try:
                path = updater.download(info["url"], updater.update_dir(),
                                        lambda d, t: self.update_progress.emit(d, t, ""))
                self.update_progress.emit(1, 1, path)
            except Exception as ex:                      # shown on the button, the app keeps running
                self.update_progress.emit(-1, 0, str(ex))
        threading.Thread(target=work, daemon=True).start()

    def _update_progress(self, done: int, total: int, path: str):
        if done < 0:
            self._updating = False
            self.update_btn.text = "UPDATE FAILED - U: TRY AGAIN"
        elif path:
            from . import updater
            self.save_session()
            updater.run_installer(path)
            self.close()
            return
        else:
            pct = int(100 * done / total) if total else 0
            self.update_btn.text = f"DOWNLOADING UPDATE {pct:3d} %"
        self._screen_changed(0)
        self.update_btn.update()

    # ------------------------------------------------------------ bug report & statistics
    def open_url(self, url: str):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(url))

    def report_bug(self):
        """F8: prefilled GitHub issue in the browser; the user reads it and decides whether to submit."""
        self.open_url(report.bug_url(__version__, report.last_crash()))
        report.clear_crash()
        self.bug_btn.hide()

    def crashed(self):
        self.bug_btn.setVisible(True)
        ts = self.bug_btn.ts()
        w = len(self.bug_btn.text) * 8 * ts
        self.bug_btn.setGeometry(self.stack.width() - w - 3 * ts, 3 * ts, w, 8 * ts)
        self.bug_btn.raise_()

    def _count(self, photos, action: str, group_done: bool, sign: int = 1):
        st = report.load_stats()
        n, b = len(photos), sum(p.size for p in photos)
        (report.count_removed if sign > 0 else report.count_undo)(st, n, b, action, group_done)
        try:
            report.save_stats(st)
        except OSError:
            pass

    # ------------------------------------------------------------ saved results
    def save_session(self):
        """Open groups, crosses and position, so reopening the folder needs no scan (if nothing changed)."""
        if self.dry_run or self.review is None or not getattr(self, "root", None) or getattr(self, "_fp", None) is None:
            return
        rv = self.review
        keep = [g for g in self.groups if g.key not in rv.done]
        pos = sum(1 for g in self.groups[:max(0, rv.i)] if g.key not in rv.done)
        marks = {g.key: [g.photos[j].path for j in sorted(rv.marks.get(g.key, ())) if j < len(g.photos)] for g in keep}
        try:
            session.save(self.root, int(self.settings["max_diff"]), int(self.settings["hash_size"]), self._fp,
                         keep, {k: v for k, v in marks.items() if v}, [], min(pos, max(0, len(keep) - 1)))
        except OSError as ex:
            print("session not saved:", ex, file=sys.stderr)

    def _fp_change(self, photos, sign: int):
        """Removed (-1) or restored (+1) files change the folder; keep the saved fingerprint in step."""
        fp = getattr(self, "_fp", None)
        if fp is not None:
            self._fp = session.adjust(fp, photos, sign)

    def closeEvent(self, e):
        self.save_session()
        if self.job is not None and self.job.isRunning():
            self.job.stop.set()
            self.job.go.set()
            self.job.wait(2000)
        super().closeEvent(e)

    def _counted(self, n, eta):
        self.search.n, self.search.eta = n, eta
        self.search.warn = None
        self.search.t0 = time.time()

    def _ask(self, n, eta, frac, subs):
        self.search.warn = (n, eta, frac, subs)
        self.search.update()

    def _answer(self, choice):
        """Big-folder warning: 'go' = scan anyway, 'other' = folder dialog, int = that subfolder."""
        job, subs = self.job, self.search.warn[3]
        self.search.warn = None
        if choice == "go":
            job.go.set()
            return
        job.stop.set()
        job.go.set()
        job.wait()
        if choice == "other":
            self.pick()
        elif isinstance(choice, tuple):                       # ("x", i): exclude that subfolder, count again
            self.exclude(os.path.join(self.root, subs[choice[1]][0]))
            self.run_search(self.root)
        else:
            self.run_search(os.path.join(self.root, subs[choice][0]))

    @staticmethod
    def _digit(e) -> int:
        """0-8 for the keys 1-9 (also with Shift, which turns them into !"... on a German keyboard), else -1."""
        code = e.nativeVirtualKey()
        if 0x31 <= code <= 0x39:
            return code - 0x31
        if Qt.Key_1 <= e.key() <= Qt.Key_9:
            return e.key() - Qt.Key_1
        return -1

    def exclude(self, folder: str):
        ex = self.settings.setdefault("excluded", [])
        folder = os.path.normpath(folder)
        if all(os.path.normcase(folder) != os.path.normcase(x) for x in ex):
            ex.append(folder)
        save_settings(self.settings)

    def _failed(self, msg):
        print(msg, file=sys.stderr)
        self.search.msg = "ERROR: " + msg.splitlines()[0]

    def close_folder(self):
        """Groups fly away, then back to the start screen (moved files stay moved)."""
        for b in (self.slider, self.open_btn, self.close_btn):
            b.hide()
        if self.nebula is not None and self.stack.currentWidget() is self.nebula:
            self.nebula.fly_away(self._close_now)
        else:
            self._close_now()

    def _close_now(self):
        self.save_session()
        if self.job is not None and self.job.isRunning():
            self.job.stop.set()
            self.job.go.set()
            self.job.wait()
        self.stack.setCurrentWidget(self.start)
        for w in (self.nebula, self.review):
            if w is not None:
                self.stack.removeWidget(w)
                w.deleteLater()
        self.nebula = self.review = None
        self.root = None
        self.history.clear()
        self._screen_changed(0)
        self.start.play_intro()                  # logo builds up, then the folder dialog

    def _strictness(self, v: int):
        self.settings["max_diff"] = v
        if getattr(self, "root", None):
            self.run_search(self.root, quiet=True)

    DOUBLE_ESC = 0.45                                  # seconds

    def eventFilter(self, obj, e):
        """F1 opens/closes help anywhere; Esc steps back screen by screen (double Esc on start quits)."""
        if e.type() == QEvent.KeyPress and not e.isAutoRepeat():
            if e.key() == Qt.Key_F8:
                self.report_bug()
                return True
            if e.key() == Qt.Key_F1:
                if self.stack.currentWidget() is self.help_screen:
                    self.close_help()
                elif self.stack.currentWidget() is not self.search:
                    self.open_help()
                return True
            if e.key() == Qt.Key_Escape:
                if self.stack.currentWidget() is self.help_screen:
                    self.close_help()
                    return True
                if self.stack.currentWidget() is not self.start:   # elsewhere Esc only steps back
                    self._last_esc = 0.0
                    return False
                now = time.time()
                if now - self._last_esc < self.DOUBLE_ESC:
                    self.close()
                    return True
                self._last_esc = now
        return False

    def apply_window_mode(self):
        if self.settings.get("window") == "window":
            self.showNormal()
            dark_title_bar(self)
        else:
            self.showFullScreen()

    def keyPressEvent(self, e):
        """Keys the screens do not handle themselves (the review handles its own Esc: back to the net)."""
        k = e.key()
        cur = self.stack.currentWidget()
        if k == Qt.Key_F11:
            self.settings["window"] = "window" if self.isFullScreen() else "fullscreen"
            save_settings(self.settings)
            self.apply_window_mode()
        elif cur is self.search and self.search.warn is not None:
            subs = self.search.warn[3]
            if k in (Qt.Key_Return, Qt.Key_Enter):
                self._answer("go")
            elif k == Qt.Key_Escape:
                self._answer("other")
            else:
                n = self._digit(e)
                if 0 <= n < len(subs):
                    self._answer(("x", n) if e.modifiers() & Qt.ShiftModifier else n)
        elif cur is self.merge_screen:
            answer = {Qt.Key_Return: "merge", Qt.Key_Enter: "merge", Qt.Key_T: "flip", Qt.Key_B: "bestof",
                      Qt.Key_Escape: "later"}.get(k)
            if answer:
                self._merge_answer(answer)
        elif cur is self.bestof_screen:
            if k == Qt.Key_X:
                self._bestof_answer("exclude")
            elif k in (Qt.Key_Return, Qt.Key_Enter):
                self._bestof_answer("keep")
            elif k == Qt.Key_Escape:
                self._bestof_answer("later")
        elif cur is self.help_screen:
            if k in (Qt.Key_Escape, Qt.Key_F1, Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
                self.close_help()
        elif cur is self.settings_screen:
            if k == Qt.Key_Escape:
                self.close_settings()
            elif Qt.Key_1 <= k <= Qt.Key_9:
                self.settings_screen.cycle(k - Qt.Key_0)
        elif k == Qt.Key_U and cur in (self.start, self.nebula) and self._update:
            self.start_update()
        elif cur is self.bin_view:
            self.bin_view.keyPressEvent(e)
        elif k == Qt.Key_B and cur in (self.start, self.nebula, self.settings_screen):
            self.open_bin()
        elif k == Qt.Key_S and cur in (self.start, self.nebula):
            self.open_settings()
        elif k in (Qt.Key_Delete, Qt.Key_Backspace) and cur is self.nebula:
            self.fire_all()
        elif k in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter) and cur is self.nebula and self.nebula is not None:
            cur_i = self.review.i if self.review is not None else -1
            alive = self.nebula.alive
            idx = cur_i if 0 <= cur_i < len(alive) and alive[cur_i] else next((i for i, a in enumerate(alive) if a), -1)
            if idx >= 0:
                self.open_group(idx)
        elif cur is self.review and self.review is not None:
            self.review.keyPressEvent(e)
        elif k == Qt.Key_Escape:                      # one level back; double Esc = quit (eventFilter)
            if cur is self.search:
                self.cancel()
            elif cur is self.nebula:
                self.close_folder()
        elif k in (Qt.Key_Return, Qt.Key_Enter) and cur is self.start:
            self.pick()
        else:
            super().keyPressEvent(e)

    def _screen_changed(self, _):
        if not hasattr(self, "fire_all_btn"):      # still building the window
            return
        neb = getattr(self, "nebula", None)
        on = neb is not None and self.stack.currentWidget() is neb
        right_w = len(self.settings_btn.text) + len(self.help_btn.text) + 3
        self.slider.setVisible(on)
        if on:
            self.slider.place(len(self.open_btn.text) + len(self.close_btn.text) + 3, right_w)
            self.slider.raise_()
        for b, x in ((self.open_btn, 0), (self.close_btn, len(self.open_btn.text) + 3)):
            b.setVisible(on)
            if on:
                b.place_bottom_left(x)
                b.raise_()
        cur = self.stack.currentWidget()
        show_corner = cur in (self.start, neb) and cur is not None
        self.update_btn.setVisible(show_corner and self._update is not None)
        if self.update_btn.isVisible():
            ts = self.update_btn.ts()
            self.update_btn.setGeometry(3 * ts, 3 * ts, len(self.update_btn.text) * 8 * ts, 8 * ts)
            self.update_btn.raise_()
        self.settings_btn.setVisible(show_corner)
        self.help_btn.setVisible(show_corner)
        if show_corner:
            self.settings_btn.place_bottom_right(0)
            self.settings_btn.raise_()
            self.help_btn.place_bottom_right(len(self.settings_btn.text) + 3)
            self.help_btn.raise_()
        self._update_fire_all()
        (cur if cur is self.review else self).setFocus()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.slider.isVisible():
            right_w = len(self.settings_btn.text) + len(self.help_btn.text) + 3
            self.slider.place(len(self.open_btn.text) + len(self.close_btn.text) + 3, right_w)
        if self.open_btn.isVisible():
            self.open_btn.place_bottom_left(0)
            self.close_btn.place_bottom_left(len(self.open_btn.text) + 3)
        QTimer.singleShot(0, lambda: self._screen_changed(0))   # after the stack has its new size

    # ------------------------------------------------------------ settings & help
    def open_help(self):
        cur = self.stack.currentWidget()
        if cur is not self.help_screen:
            self._help_from = cur
            self.stack.setCurrentWidget(self.help_screen)

    def close_help(self):
        back = self._help_from if self._help_from is not None else self.start
        if back is self.help_screen or back is None:
            back = self.start
        self.stack.setCurrentWidget(back)

    def open_bin(self):
        cur = self.stack.currentWidget()
        if cur is not self.bin_view:
            self._bin_from = cur
            self.bin_view.reload()
            self.stack.setCurrentWidget(self.bin_view)
            self.bin_view.setFocus()

    def close_bin(self):
        back = self._bin_from if self._bin_from in (self.start, self.nebula, self.settings_screen) else self.start
        self.stack.setCurrentWidget(back)
        if back is self.settings_screen:
            self.settings_screen.refresh_bin()
        if self._bin_changed and getattr(self, "root", None) and self.nebula is not None:
            self._bin_changed = False
            self.run_search(self.root, quiet=True)      # restored images may form groups again

    def _restored(self, paths: list[str]):
        self._bin_changed = True
        sizes = []
        for p in paths:
            try:
                sizes.append(os.path.getsize(p))
            except OSError:
                pass
        st = report.load_stats()
        report.count_undo(st, len(sizes), sum(sizes), "move", False)
        try:
            report.save_stats(st)
        except OSError:
            pass

    def open_settings(self):
        self._ex_before = list(self.settings.get("excluded", []))
        self._settings_from = self.stack.currentWidget()
        self.stack.setCurrentWidget(self.settings_screen)

    def close_settings(self):
        back = self._settings_from if self._settings_from is not None else self.start
        if back not in (self.start, self.nebula) or back is None:
            back = self.start
        self.stack.setCurrentWidget(back)
        changed = self.settings.get("excluded", []) != getattr(self, "_ex_before", None)
        if back is self.nebula and getattr(self, "root", None) and changed:
            self.run_search(self.root, quiet=True)       # exclusions changed: search again without them

    def _setting_changed(self, key: str):
        s = self.settings
        if key == "excluded_add":
            d = QFileDialog.getExistingDirectory(self, "Duplikill - exclude a folder from scanning",
                                                 getattr(self, "root", None) or s.get("last_root") or "")
            if d:
                self.exclude(d)
            self.settings_screen.update()
            return
        if key == "open_bin":
            self.open_bin()
            return
        if key == "share_stats":
            self.open_url(report.stats_url(__version__, report.load_stats()))
            return
        if key.startswith("excluded_del:"):
            ex = s.get("excluded", [])
            k = int(key.split(":")[1])
            if 0 <= k < len(ex):
                ex.pop(k)
            save_settings(s)
            self.settings_screen.update()
            return
        if key == "target_mode" and s["target_mode"] == "custom":
            d = QFileDialog.getExistingDirectory(self, "Folder for removed images", s.get("target") or "")
            if d:
                s["target"] = os.path.normpath(d)
            elif not s.get("target"):
                s["target_mode"] = "central"
        if key == "window":
            self.apply_window_mode()
        if key == "fire" and self.review is not None:
            self.review.mode = s["fire"]
        if key == "pan" and self.review is not None:
            self.review.pan = s["pan"]
        if key == "action":
            self._delete_ok = False
        save_settings(s)
        self.settings_screen.update()

    def _found(self, groups, saved: dict | None = None):
        if saved is None and getattr(self, "groups", None) is not None and self.review is not None \
                and not getattr(self, "_skip_save", False):
            self.save_session()                         # slider: keep the old strictness's crosses
        self.slider.busy = False
        if saved is not None:
            self._fp = saved["fingerprint"]
        elif self.job is not None and self.job.fp is not None:
            self._fp = self.job.fp
        # group steps point at the old groups; folder merges of this folder stay undoable across rescans
        self.history = [h for h in self.history if h[0] is None and h[2][2] == self.root]
        for w in (self.nebula, self.review):
            if w is not None:
                self.stack.removeWidget(w)
                w.deleteLater()
        self.groups = groups
        self.nebula = Nebula(groups, self.thumbs, root=self.root)
        self.review = Review(groups, self.thumbs)
        self.review.mode = self.settings["fire"]
        self.review.pan = self.settings.get("pan", "middle")
        self.stack.addWidget(self.nebula)
        self.stack.addWidget(self.review)
        self.nebula.group_clicked.connect(self.open_group)
        self.review.back.connect(self.overview)
        self.review.delete_requested.connect(self.remove)
        self.review.kill_requested.connect(self.kill_one)
        self.review.marks_changed.connect(self._update_fire_all)
        if saved is not None:
            self.review.marks = {k: set(v) for k, v in saved["marks"].items()}
            self.review.i = saved["position"]
        self.overview()
        paths = self.job.paths if self.job is not None and saved is None else None
        if paths and not self.dry_run:
            st = report.load_stats()
            st["scans"] += 1
            st["scanned_images"] += len(paths)
            try:
                report.save_stats(st)
            except OSError:
                pass
        self._skip_save = False
        job = self.job
        full = job is not None and job.confirm and job.paths is not None and (saved is None or job.saved is saved)
        if full:                                        # a full scan or reopen (not the slider)
            from . import bestof
            s = self.settings
            self._merges = list(job.copies or [])
            self._merge_n, self._rescan_after = len(self._merges), False
            in_merge = [f for m in self._merges for f in (m.keep, m.other)]   # asked about already
            self._bestof = [b for b in bestof.candidates(groups, job.paths, s.get("excluded", []) +
                                                         s.get("bestof_kept", []) + in_merge)]
            self._bestof_n, self._bestof_excluded = len(self._bestof), False
            self._next_question()

    def _next_question(self):
        """Merge offers first, then best-of folders, then the overview (rescan if something changed)."""
        if self._merges:
            self.merge_screen.ask(self._merges[0], self._merge_n - len(self._merges), self._merge_n)
            self.stack.setCurrentWidget(self.merge_screen)
        elif self._bestof:
            self.bestof_screen.ask(self._bestof[0], self._bestof_n - len(self._bestof), self._bestof_n)
            self.stack.setCurrentWidget(self.bestof_screen)
        else:
            self.overview()
            if self._rescan_after or self._bestof_excluded:   # same folder again (cache: quick)
                self._rescan_after = self._bestof_excluded = False
                self._skip_save = True                          # the old groups are out of date
                session.forget(self.root, int(self.settings["max_diff"]), int(self.settings["hash_size"]))
                self.run_search(self.root, quiet=True)

    def _merge_answer(self, answer: str):
        if not self._merges:
            return
        if answer == "flip":
            self._merges[0] = self._merges[0].flipped()
            self.merge_screen.ask(self._merges[0], self.merge_screen.pos, self._merge_n)
            return
        m = self._merges.pop(0)
        if answer == "later":
            self._merges = []
        elif answer == "bestof":
            self.exclude(m.album)
            self._rescan_after = True
        elif answer == "merge" and self.do_merge(m):
            self._rescan_after = True
        self._next_question()

    def do_merge(self, m) -> bool:
        """One step for the whole folder pair; Ctrl+Z brings everything back (MOVE mode)."""
        from .copies import rename_with_sidecars
        remove, renames = m.plan()
        if not remove or not self._confirm_delete(len(remove)):
            return False
        key = f"merge:{time.time():.3f}"
        if self.dry_run:
            print("WOULD MERGE:", m.other, "->", m.keep, "remove", len(remove), "rename", len(renames))
            return False
        known = {p.path: p for g in self.groups for p in g.photos}
        gone = []
        for p in remove:
            ph = known.get(p)
            if ph is None:
                try:
                    st = os.stat(p)
                    ph = Photo(path=p, size=st.st_size, mtime=st.st_mtime)
                except OSError:
                    continue
            gone.append(ph)
        action = self.settings["action"]
        if action == "move":
            moved, _ = self._mover().merge(remove, renames, key)
        else:
            moved = self._act(remove, key)
            for src, dst in renames:
                try:
                    rename_with_sidecars(src, dst)
                except OSError as ex:
                    print("merge: skipped", src, ex, file=sys.stderr)
        done = {os.path.normcase(s) for s, _ in moved}
        gone = [p for p in gone if os.path.normcase(p.path) in done]
        self._fp_change(gone, -1)
        self._count(gone, action, False)
        self.history.append((None, moved, (key, gone, self.root), action))
        return True

    def _bestof_answer(self, answer: str):
        if not self._bestof:
            return
        folder = self._bestof.pop(0)[0]
        if answer == "exclude":
            self.exclude(folder)
            self._bestof_excluded = True
        elif answer == "keep":
            self.settings.setdefault("bestof_kept", []).append(folder)
            save_settings(self.settings)
        elif answer == "later":
            self._bestof = []
        self._next_question()

    # ------------------------------------------------------------ review
    def overview(self):
        self.stack.setCurrentWidget(self.nebula)
        self._update_fire_all()

    def _update_fire_all(self):
        """'At the end' mode: in the overview a command deletes every cross of every group at once."""
        rv, neb = self.review, getattr(self, "nebula", None)
        todo = rv.all_marked() if rv is not None and self.settings.get("fire") == AT_END else []
        on = bool(todo) and neb is not None and self.stack.currentWidget() is neb
        self.fire_all_btn.setVisible(on)
        if on:
            n = sum(len(p) for _, p in todo)
            free = sum(p.size for i, paths in todo for p in self.groups[i].photos if p.path in set(paths))
            self.fire_all_btn.text = f"DEL: {n} X DELETE ALL  {human(free)}"
            ts = self.fire_all_btn.ts()
            w, h = len(self.fire_all_btn.text) * 8 * ts, 8 * ts
            self.fire_all_btn.setGeometry((self.stack.width() - w) // 2, 3 * ts, w, h)
            self.fire_all_btn.raise_()
            self.fire_all_btn.update()

    def fire_all(self):
        if self.review is None or self.settings.get("fire") != AT_END:
            return
        todo = self.review.all_marked()
        if not todo or not self._confirm_delete(sum(len(p) for _, p in todo)):
            return
        for i, paths in todo:
            self._remove_group(i, paths)
        if self.nebula is not None:
            self.nebula.recount()
        self._update_fire_all()

    def _confirm_delete(self, n: int) -> bool:
        """'Delete forever' is asked once per session; moving and the recycle bin are not asked."""
        if self.settings["action"] != "delete" or self._delete_ok or self.dry_run:
            return True
        from PySide6.QtWidgets import QMessageBox
        r = QMessageBox.question(self, "Duplikill", f"Delete {n} file(s) FOREVER?\n"
                                 "This cannot be undone. You will not be asked again in this session.",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        self._delete_ok = r == QMessageBox.Yes
        return self._delete_ok

    def open_group(self, i: int):
        self.review.show_group(i)
        self.stack.setCurrentWidget(self.review)
        self.review.setFocus()
        if not self._hint_done:                     # once per start: the core keys blink big
            self._hint_done = True
            self.review.show_hint()

    def _remove_group(self, i: int, paths: list[str]):
        g = self.groups[i]
        if self.dry_run:
            print("WOULD REMOVE:", paths)
            moved = [(p, "") for p in paths]
        else:
            moved = self._act(paths, g.key)
        gone = {m[0] for m in moved}
        self._fp_change([p for p in g.photos if p.path in gone], -1)
        if not self.dry_run:
            self._count([p for p in g.photos if p.path in gone], self.settings["action"], True)
        self.history.append((i, moved, None, self.settings["action"]))
        self.review.done.add(g.key)
        self.nebula.hide(i)

    def remove(self, i: int, paths: list[str]):
        if not self._confirm_delete(len(paths)):
            return
        self._remove_group(i, paths)
        before = self.review.i
        self.review.step(1)
        if self.review.i == before:               # nothing further on: try backwards, else overview
            self.review.step(-1)
            if self.review.i == before:
                self.overview()

    def kill_one(self, i: int, path: str):
        """Skull: this one picture goes right away; the group stays open while 2 or more are left."""
        g = self.groups[i]
        photo = next(p for p in g.photos if p.path == path)
        if not self._confirm_delete(1):
            return
        if self.dry_run:
            print("WOULD REMOVE:", [path])
            moved = [(path, "")]
        else:
            moved = self._act([path], g.key)
        self._fp_change([photo], -1)
        if not self.dry_run:
            self._count([photo], self.settings["action"], len(g.photos) <= 2)
        j = self.review.remove_photo(path)
        self.history.append((i, moved, (j, photo), self.settings["action"]))
        if len(g.photos) < 2:                       # nothing left to compare
            self.review.done.add(g.key)
            self.nebula.hide(i)
            before = self.review.i
            self.review.step(1)
            if self.review.i == before:
                self.review.step(-1)
                if self.review.i == before:
                    self.overview()
        if self.nebula is not None:
            self.nebula.recount()

    def _mover(self) -> Mover:
        s = self.settings
        return Mover(s.get("target", ""), s.get("target_mode", "central"), getattr(self, "root", "") or "")

    def _act(self, paths: list[str], key: str) -> list[tuple[str, str]]:
        action = self.settings["action"]
        if action == "move":
            return self._mover().move(paths, key)
        from .actions import sidecars
        if action == "trash":
            from send2trash import send2trash
            for p in paths:
                for f in [p, *sidecars(p)]:
                    send2trash(f)
            return [(p, "") for p in paths]
        if action == "delete":
            for p in paths:
                for f in [p, *sidecars(p)]:
                    os.remove(f)
            return [(p, "") for p in paths]
        raise RuntimeError(f"unknown action {action!r}")

    def undo(self):
        if not self.history:
            return
        if self.history[-1][3] != "move" and not self.dry_run:
            return                                  # recycle bin / deleted: nothing to bring back here
        i, moved, single, action = self.history.pop()
        if i is None:                               # a folder merge: everything back, then search again
            key, gone, _ = single
            self._mover().undo_last(key)
            self._fp_change(gone, +1)
            self._count(gone, action, False, -1)
            self._skip_save = True
            session.forget(self.root, int(self.settings["max_diff"]), int(self.settings["hash_size"]))
            self.run_search(self.root, quiet=True)
            return
        if not self.dry_run:
            self._mover().undo_last()
        g = self.groups[i]
        back = {m[0] for m in moved}
        restored = [single[1]] if single is not None else [p for p in g.photos if p.path in back]
        self._fp_change(restored, +1)
        if not self.dry_run:
            self._count(restored, action, single is None or len(g.photos) < 2, -1)
        if single is not None:                      # skull: put the one picture back in its place
            self.review.restore_photo(i, *single)
        else:
            self.review.marks.pop(g.key, None)
        self.review.done.discard(g.key)
        self.nebula.alive[i] = True
        self.nebula.recount()
        self.open_group(i)


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    win: list = []

    def hook(et, ev, tb):                       # log anonymised, keep running, offer F8
        sys.__excepthook__(et, ev, tb)
        try:
            report.write_crash(et, ev, tb)
            if win:
                win[0].crashed()
        except Exception:
            pass
    sys.excepthook = hook
    dry = "--dry-run" in argv
    w = Main(dry_run=dry)
    win.append(w)
    if report.last_crash():                     # the last run ended with an error
        QTimer.singleShot(500, w.crashed)
    w.resize(1600, 950)
    w.apply_window_mode()
    roots = [a for a in argv[1:] if not a.startswith("--")]
    if roots:
        w.run_search(os.path.normpath(roots[0]))
    else:
        w.start.play_intro()
    sys.exit(app.exec())
