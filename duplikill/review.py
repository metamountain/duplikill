"""Reviewing one group: all images edge to edge, black and white, no frames - an image compare.

Wheel        = zoom all images together into the same spot (pixel exact from 100 %, no smoothing)
Middle drag  = move the section in all images        Esc = back to the overview
Left click   = toggle the delete cross               Del / Backspace / n X DEL button = delete them, next
Hold right   = blink compare: every cell shows the next image of the group, same spot and zoom
Tab / mouse side buttons = one image alone / flip to the next one
Space / Right arrow = next group, Left arrow = previous
0 / Home = fit
Ctrl+Z = undo (handled by the window)
"""
from __future__ import annotations

import os
import time

from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from . import pixelfont
from .groups import Group, pick_best
from .index import human, register_heif

register_heif()

THUMB = 560
BLINK = 1.0                               # seconds on, seconds off: the BEST label blinks slowly
WHITE = QColor(255, 255, 255)
GREY = QColor(150, 150, 150)
BLACK_BG = QColor(0, 0, 0, 200)
PER_GROUP, AT_END = "group", "end"       # when the delete command comes (setting)
GAP = 2                                   # black seam between images, no other margin
MAX_ZOOM = 64.0


def layout(n: int, w: float, h: float, aspect: float = 1.5, gap: float = GAP) -> tuple[int, int, float]:
    """(cols, rows, cell width) that makes the images as big as possible while all fit."""
    best = (1, n, 0.0)
    for cols in range(1, n + 1):
        rows = -(-n // cols)
        cw = min((w - gap * (cols - 1)) / cols, ((h - gap * (rows - 1)) / rows) * aspect)
        if cw > best[2]:
            best = (cols, rows, cw)
    return best


def cells(n: int, w: int, h: int) -> list[QRectF]:
    """Cells filling the whole widget edge to edge (last row centred)."""
    cols, rows, _ = layout(n, w, h)
    cw = (w - GAP * (cols - 1)) / cols
    ch = (h - GAP * (rows - 1)) / rows
    out = []
    for j in range(n):
        r, c = divmod(j, cols)
        n_row = min(cols, n - r * cols)
        x = (cols - n_row) * (cw + GAP) / 2 + c * (cw + GAP)
        out.append(QRectF(x, r * (ch + GAP), cw, ch))
    return out


class _Loaded(QObject):
    done = Signal(str, object)             # path, [original, 1/2, 1/4, ...] (mip levels)


MIP_MIN = 400                            # smallest level width


def mips(img: QImage) -> list[QImage]:
    """Original plus halves: shrinking from the next bigger level keeps the preview smooth (no jaggies)."""
    out = [img]
    while not img.isNull() and img.width() // 2 >= MIP_MIN:
        img = img.scaled(img.width() // 2, img.height() // 2, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        out.append(img)
    return out


def pick_level(levels: list[QImage], width: float) -> QImage:
    """Smallest level that is still at least as wide as it is drawn."""
    for lv in reversed(levels):
        if lv.width() >= width:
            return lv
    return levels[0]


_POOL = ThreadPoolExecutor(max_workers=4)
FULL_BUDGET = 1500 * 2 ** 20     # bytes for decoded originals of one group; big groups get scaled-down copies


def _read(path: str, max_pixels: int) -> QImage:
    r = QImageReader(path)
    r.setAutoTransform(True)                     # same orientation as the thumbnails
    size = r.size()
    if size.isValid() and size.width() * size.height() > max_pixels:
        f = (max_pixels / (size.width() * size.height())) ** 0.5
        r.setScaledSize(size * f)
    img = r.read()
    if not img.isNull():
        return img
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im.convert("RGBA"))
            if im.width * im.height > max_pixels:
                f = (max_pixels / (im.width * im.height)) ** 0.5
                im = im.resize((max(1, int(im.width * f)), max(1, int(im.height * f))), Image.BILINEAR)
            return QImage(im.tobytes(), im.width, im.height, im.width * 4, QImage.Format_RGBA8888).copy()
    except Exception:
        return QImage()


class Review(QWidget):
    delete_requested = Signal(int, list)     # group index, paths to remove
    back = Signal()                          # to the overview
    moved = Signal(int)                      # now showing group i
    kill_requested = Signal(int, str)        # group index, one path: the skull on an image
    marks_changed = Signal()

    def __init__(self, groups: list[Group], thumbs, parent=None):
        super().__init__(parent)
        self.groups, self.thumbs = groups, thumbs
        self.marks: dict[str, set[int]] = {}           # group key -> marked image indices
        self.done: set[str] = set()                    # groups already handled
        self.mode = PER_GROUP
        self.pan = "middle"                            # "middle" | "right"
        self.i = 0
        self.kill_rect = QRectF()
        self.kill_j = -1
        self.kill_rects: dict[int, QRectF] = {}
        self.only_rects: dict[int, QRectF] = {}
        self.fire_rect = QRectF()
        self.keep_best_rect = QRectF()
        self.prev_rect = QRectF()
        self.next_rect = QRectF()
        self.hub_rect = QRectF()
        self.hover = -1
        self.solo: int | None = None                   # one image alone
        self.swap = 0                                  # 1 while the right button is held (blink)
        self.zoom = 1.0                                # 1 = whole image fits its cell
        self.center = (0.5, 0.5)                       # shown spot, in image fractions (same for all)
        self.rects: list[QRectF] = []
        self.pix: dict[str, QPixmap] = {}
        self.full: dict[str, QImage] = {}              # originals of the current group, loaded on zoom
        self.levels: dict[str, list[QImage]] = {}      # their mip levels (original first)
        self._loading: set[str] = set()
        self._loaded = _Loaded()
        self._loaded.done.connect(self._full_ready)
        self._press: QPointF | None = None
        self._dragged = False
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        thumbs.ready.connect(self._ready)
        self._blink = QTimer(self, interval=int(BLINK * 1000), timeout=self._blink_tick)
        self._blink.start()
        self.hint_until = 0.0
        self._hint_timer = QTimer(self, interval=60, timeout=self._hint_tick)

    HINT = ["CLICK IMAGE: CROSS IT", "DEL / BACKSPACE: DELETE", "SPACE / DOWN: NEXT GROUP", "UP: PREVIOUS GROUP"]
    HINT_SECONDS = 1.6

    def show_hint(self):
        """New users: the core keys blink big in the middle for a moment."""
        self.hint_until = time.time() + self.HINT_SECONDS
        self._hint_timer.start()

    def _hint_tick(self):
        if time.time() > self.hint_until:
            self._hint_timer.stop()
        self.update()

    def _draw_hint(self, qp: QPainter, ts: int):
        if time.time() > self.hint_until or int(time.time() * 4) % 2:       # blinks 2x per second
            return
        img = pixelfont.render(self.HINT, 2 * ts, WHITE, QColor(0, 0, 0, 235))
        pad = 4 * ts
        x, y = (self.width() - img.width()) // 2, (self.height() - img.height()) // 2
        qp.fillRect(x - pad, y - pad, img.width() + 2 * pad, img.height() + 2 * pad, QColor(0, 0, 0, 235))
        qp.drawImage(QPoint(x, y), img)

    def _blink_tick(self):
        if self.isVisible() and self.group is not None and len(self.group.photos) > 1:
            self.update()

    # ------------------------------------------------------------ state
    @property
    def group(self) -> Group | None:
        return self.groups[self.i] if 0 <= self.i < len(self.groups) else None

    def marked(self, g: Group | None = None) -> set[int]:
        g = g or self.group
        return self.marks.setdefault(g.key, set()) if g else set()

    def reset_view(self):
        self.zoom, self.center = 1.0, (0.5, 0.5)
        self.update()

    def show_group(self, i: int):
        if not self.groups:
            return
        self.i = max(0, min(i, len(self.groups) - 1))
        self.solo = None
        self.hover = -1
        self.full.clear()
        self.levels.clear()
        self.pix.clear()                    # only the current group's pixmaps stay in memory
        self.reset_view()
        for k in range(self.i, min(len(self.groups), self.i + 4)):     # preload ahead
            for p in self.groups[k].photos:
                self.thumbs.request(p.path, THUMB, priority=5 if k == self.i else 1)
        self.moved.emit(self.i)

    def step(self, d: int, wrap: bool = False):
        """Next/previous group that is not done yet."""
        n = len(self.groups)
        if not n:
            return
        j = self.i
        for _ in range(n):
            j += d
            if not 0 <= j < n:
                if not wrap:
                    return
                j %= n
            if self.groups[j].key not in self.done and j != self.i:
                self.show_group(j)
                return

    def toggle(self, j: int):
        g = self.group
        if g is None or not 0 <= j < len(g.photos):
            return
        m = self.marked()
        if j in m:
            m.discard(j)
        elif len(g.photos) == 2 and len(m) == 1:    # 2-image pair: 1-click swap to the other photo
            m.clear()
            m.add(j)
        elif len(m) < len(g.photos) - 1:            # one image always stays
            m.add(j)
        self.marks_changed.emit()
        self.update()

    def keep_only(self, j: int):
        """Cross out all other images in this group (or clear if already only j)."""
        g = self.group
        if g is None or not 0 <= j < len(g.photos) or len(g.photos) < 2:
            return
        m = self.marked()
        others = {k for k in range(len(g.photos)) if k != j}
        if m == others:
            m.clear()
        else:
            m.clear()
            m.update(others)
        self.marks_changed.emit()
        self.update()

    def keep_best(self):
        """Cross out all images except g.best."""
        g = self.group
        if g is None or len(g.photos) < 2:
            return
        best = g.best if 0 <= g.best < len(g.photos) else 0
        m = self.marked()
        m.clear()
        m.update(k for k in range(len(g.photos)) if k != best)
        self.marks_changed.emit()
        self.update()

    def fire(self):
        """The delete command for this group."""
        g = self.group
        m = self.marked()
        if g is None or not m:
            return
        self.delete_requested.emit(self.i, [g.photos[j].path for j in sorted(m)])

    def flip(self, d: int = 1):
        """Blink compare: show one image alone; again -> the next one, same spot, same zoom."""
        g = self.group
        if g is None:
            return
        if self.solo is None:
            self.solo = self.hover if self.hover >= 0 else 0
        else:
            self.solo = (self.solo + d) % len(g.photos)
        self.update()

    def all_marked(self) -> list[tuple[int, list[str]]]:
        """For the 'at the end' mode: every group with crosses."""
        out = []
        for i, g in enumerate(self.groups):
            m = self.marks.get(g.key)
            if m and g.key not in self.done:
                out.append((i, [g.photos[j].path for j in sorted(m)]))
        return out

    # ------------------------------------------------------------ images
    def _ready(self, path):
        self.pix.pop(path, None)
        g = self.group
        if g and any(p.path == path for p in g.photos):
            self.update()

    def _thumb(self, path: str) -> QPixmap | None:
        pm = self.pix.get(path)
        if pm is None:
            img = self.thumbs.get(path, THUMB)
            if img is None or img.isNull():
                return None
            pm = self.pix[path] = QPixmap.fromImage(img)
        return pm

    def _original(self, path: str) -> QImage | None:
        """The original, if loaded; otherwise it is loaded in the background (thumbnail shown meanwhile)."""
        img = self.full.get(path)
        if img is not None:
            return None if img.isNull() else self.levels.get(path, [img])
        if path not in self._loading:
            self._loading.add(path)
            sig = self._loaded.done
            n = len(self.group.photos) if self.group else 1
            budget = FULL_BUDGET * 3 // 4 // 4 // max(1, n)  # pixels per image (4 bytes each, + 1/3 for the mips)

            def _task():
                decoded = _read(path, budget)
                try:
                    sig.emit(path, mips(decoded))
                except RuntimeError:
                    pass

            _POOL.submit(_task)
        return None

    def _full_ready(self, path: str, levels: list):
        img = levels[0]
        self._loading.discard(path)
        g = self.group
        if g and any(p.path == path for p in g.photos):      # still the same group
            self.full[path] = img
            self.levels[path] = levels
            self.update()

    def _dims(self, p, thumb: QPixmap | None) -> tuple[float, float]:
        """Size of the original in pixels, the right way up."""
        img = self.full.get(p.path)
        w, h = p.width or 0, p.height or 0
        if img is not None and not img.isNull():
            if not w or not h:
                return img.width(), img.height()
            if (img.width() >= img.height()) != (w >= h):   # EXIF rotated
                w, h = h, w
            return w, h                                      # the original's size, even for a scaled copy
        if thumb is not None and not thumb.isNull():
            if not w or not h:
                return thumb.width(), thumb.height()
            if (thumb.width() >= thumb.height()) != (w >= h):          # EXIF rotated
                w, h = h, w
        return (w or 1500), (h or 1000)

    # ------------------------------------------------------------ geometry
    def _visible(self) -> list[int]:
        g = self.group
        if g is None:
            return []
        return [self.solo] if self.solo is not None else list(range(len(g.photos)))

    def _view(self, cell: QRectF, iw: float, ih: float) -> tuple[float, QRectF]:
        """(screen pixels per original pixel, source rect in original pixels) for one cell."""
        s = min(cell.width() / iw, cell.height() / ih) * self.zoom
        sw, sh = cell.width() / s, cell.height() / s
        cx, cy = self.center[0] * iw, self.center[1] * ih
        return s, QRectF(cx - sw / 2, cy - sh / 2, sw, sh)

    def _clamp_center(self):
        """Keep the section inside the image (centre when the image is smaller than the cell)."""
        half = 0.5 / self.zoom
        cx = min(max(self.center[0], half), 1 - half) if half < 0.5 else 0.5
        cy = min(max(self.center[1], half), 1 - half) if half < 0.5 else 0.5
        self.center = (cx, cy)

    # ------------------------------------------------------------ paint
    def _ts(self) -> int:
        return max(2, min(self.width(), self.height()) // 450)

    def _hub_anchor(self, n_vis: int, w: int, h: int) -> tuple[float, float, int, int]:
        """Return (hub_cx, hub_cy, cols, rows) snapped to the grid seam closest to (w/2, h/2)."""
        cols, rows, _ = layout(n_vis, w, h)
        if n_vis <= 1:
            return w / 2, -1.0, cols, rows
        cw = (w - GAP * (cols - 1)) / cols
        ch = (h - GAP * (rows - 1)) / rows
        v_seams = [c * (cw + GAP) - GAP / 2 for c in range(1, cols)]
        h_seams = [r * (ch + GAP) - GAP / 2 for r in range(1, rows)]
        if rows % 2 == 0:
            return w / 2, h / 2, cols, rows
        if rows == 1 and cols % 2 == 0:
            return w / 2, h / 2, cols, rows
        if rows >= 2:
            cy = min(h_seams, key=lambda y: abs(y - h / 2))
            return w / 2, cy, cols, rows
        cx = min(v_seams, key=lambda x: abs(x - w / 2))
        return cx, h / 2, cols, rows

    def paintEvent(self, e):
        self.kill_rect = QRectF()
        self.kill_rects.clear()
        self.only_rects.clear()
        self.fire_rect = QRectF()
        self.keep_best_rect = QRectF()
        self.prev_rect = QRectF()
        self.next_rect = QRectF()
        self.hub_rect = QRectF()
        qp = QPainter(self)
        qp.fillRect(self.rect(), QColor(0, 0, 0))
        g = self.group
        if g is None:
            return
        vis = self._visible()
        rects = cells(len(vis), self.width(), self.height())
        cols_vis, _, _ = layout(len(vis), self.width(), self.height())
        self.rects = [QRectF() for _ in g.photos]
        m = self.marked()
        ts = self._ts()
        for j0, cell in zip(vis, rects):
            self.rects[j0] = cell
            j = (j0 + self.swap) % len(g.photos)        # held blink button: the next image in this cell
            p = g.photos[j]
            thumb = self._thumb(p.path)
            iw, ih = self._dims(p, thumb)
            s, _ = self._view(cell, iw, ih)
            qp.save()
            qp.setClipRect(cell)
            img_rect = QRectF(cell.center().x() - iw * s / 2 + (0.5 - self.center[0]) * iw * s,
                              cell.center().y() - ih * s / 2 + (0.5 - self.center[1]) * ih * s, iw * s, ih * s)
            need_full = thumb is None or s * iw > thumb.width() * 1.05
            levels = self._original(p.path) if need_full else None
            if levels is not None:
                lv = pick_level(levels, iw * s * self.devicePixelRatioF())
                qp.setRenderHint(QPainter.SmoothPixmapTransform, s < 1.0)    # from 100 %: real pixels
                qp.drawImage(img_rect, lv, QRectF(lv.rect()))
            elif thumb is not None:
                qp.setRenderHint(QPainter.SmoothPixmapTransform, True)
                qp.drawPixmap(img_rect, thumb, QRectF(thumb.rect()))
            if j in m:
                self._cross(qp, img_rect.intersected(cell))
            folder = os.path.basename(os.path.dirname(p.path))
            zoom_txt = f"   {s * 100:.0f} %" if self.zoom > 1.0 else ""
            best_txt = ("BEST  " if int(time.time() / BLINK) % 2 == 0 else "      ") if j == g.best else ""
            cap = pixelfont.render([f"{best_txt}{int(iw)}X{int(ih)}  {human(p.size)}{zoom_txt}", folder],
                                   ts, GREY if j in m else WHITE, BLACK_BG)
            top_row = len(vis) > cols_vis and cell.center().y() < self.height() / 2
            cap_y = int(cell.top()) if top_row else int(cell.bottom()) - cap.height()
            qp.drawImage(QPoint(int(cell.left()), cap_y), cap)
            qp.restore()
        self._status(qp, g, ts, vis)
        if self.swap == 0 and len(g.photos) > 1 and len(vis) > 1:
            self._draw_cell_switches(qp, g, ts, vis, rects)
        self._draw_hint(qp, ts)                         # on top of everything

    def _cross(self, qp: QPainter, rect: QRectF):
        """Image dimmed, a big X over it in thin white lines."""
        qp.fillRect(rect, QColor(0, 0, 0, 150))
        qp.setRenderHint(QPainter.Antialiasing)
        d = min(rect.width(), rect.height())
        pen = QPen(WHITE, max(1.5, d * 0.006))
        pen.setCapStyle(Qt.FlatCap)
        qp.setPen(pen)
        side = d * 0.6
        r = QRectF(rect.center().x() - side / 2, rect.center().y() - side / 2, side, side)
        qp.drawLine(r.topLeft(), r.bottomRight())
        qp.drawLine(r.topRight(), r.bottomLeft())
        qp.setRenderHint(QPainter.Antialiasing, False)

    def _draw_cell_switches(self, qp: QPainter, g: Group, ts: int, vis: list[int], rects: list[QRectF]):
        """Per-cell switch pill at the inner seam edge closest to the Central Hub (never at top-left or photo center)."""
        W, H = self.width(), self.height()
        hub_cx, hub_cy, _, rows = self._hub_anchor(len(vis), W, H)
        pad = ts * 2
        hub_guard = self.hub_rect.adjusted(-pad * 2, -pad * 2, pad * 2, pad * 2)
        n = len(g.photos)
        m = self.marked()
        skull_icon = pixelfont.render_bits(pixelfont.icon("skull"), ts * 2, WHITE, QColor(0, 0, 0, 0))
        sk_w = skull_icon.width() + 2 * pad
        sk_h = skull_icon.height() + 2 * pad
        only_img = pixelfont.render(["ONLY"], ts, WHITE, QColor(0, 0, 0, 0)) if n >= 3 else None
        on_w = (only_img.width() + 2 * pad) if only_img is not None else 0
        sw_w = sk_w + (GAP + on_w if on_w else 0)
        sw_h = sk_h

        for j0, cell in zip(vis, rects):
            xmin = cell.left() + pad + sw_w / 2
            xmax = cell.right() - pad - sw_w / 2
            ymin = cell.top() + pad + sw_h / 2
            ymax = cell.bottom() - pad - sw_h / 2
            px = min(max(hub_cx, xmin), xmax)
            py = min(max(hub_cy, ymin), ymax)
            r = QRectF(px - sw_w / 2, py - sw_h / 2, sw_w, sw_h)
            if r.intersects(hub_guard):
                if rows == 1:
                    above_y = hub_guard.top() - sw_h / 2
                    py = above_y if above_y >= ymin else min(ymax, hub_guard.bottom() + sw_h / 2)
                elif cell.center().y() < hub_cy:
                    py = max(ymin, hub_guard.top() - sw_h / 2)
                else:
                    py = min(ymax, hub_guard.bottom() + sw_h / 2)
                r = QRectF(px - sw_w / 2, py - sw_h / 2, sw_w, sw_h)

            bg = QColor(0, 0, 0, 215 if j0 == self.hover else 165)
            cur_x = r.left()
            if only_img is not None:
                is_only = (len(m) == n - 1 and j0 not in m)
                o_rect = QRectF(cur_x, r.top(), on_w, sw_h)
                qp.fillRect(o_rect, WHITE if is_only else bg)
                o_fg = QColor(0, 0, 0) if is_only else (WHITE if j0 == self.hover else GREY)
                o_render = pixelfont.render(["ONLY"], ts, o_fg, QColor(0, 0, 0, 0))
                qp.drawImage(QPoint(int(o_rect.left() + pad), int(o_rect.top() + (sw_h - o_render.height()) / 2)), o_render)
                self.only_rects[j0] = o_rect
                cur_x += on_w + GAP

            k_rect = QRectF(cur_x, r.top(), sk_w, sw_h)
            qp.fillRect(k_rect, bg)
            sk_fg = WHITE if j0 == self.hover else GREY
            sk_render = pixelfont.render_bits(pixelfont.icon("skull"), ts * 2, sk_fg, QColor(0, 0, 0, 0))
            qp.drawImage(QPoint(int(k_rect.left() + pad), int(k_rect.top() + pad)), sk_render)
            self.kill_rects[j0] = k_rect

        target_j = self.hover if self.hover in self.kill_rects else (vis[0] if vis else -1)
        if target_j in self.kill_rects:
            self.kill_rect = self.kill_rects[target_j]
            self.kill_j = target_j

    def remove_photo(self, path: str) -> int:
        """Drops one picture from the current group (after the skull). Returns its old index."""
        g = self.group
        j = next(k for k, p in enumerate(g.photos) if p.path == path)
        del g.photos[j]
        g.best = pick_best(g) if g.photos else 0
        m = {k - (k > j) for k in self.marked() if k != j}
        if g.photos and len(m) >= len(g.photos):
            m.discard(g.best if 0 <= g.best < len(g.photos) else 0)
        self.marks[g.key] = m
        if self.solo is not None:
            self.solo = min(self.solo, len(g.photos) - 1) if g.photos else None
        self.hover = -1
        self.marks_changed.emit()
        self.update()
        return j

    def restore_photo(self, i: int, j: int, photo):
        g = self.groups[i]
        g.photos.insert(j, photo)
        g.best = pick_best(g)
        self.marks[g.key] = {k + (k >= j) for k in self.marks.get(g.key, set())}

    def _status(self, qp: QPainter, g: Group, ts: int, vis: list[int]):
        """Central Hub right at the seam intersection (W/2, H/2): status + nav on Row 1, action on Row 2."""
        W, H = self.width(), self.height()
        hub_cx, hub_cy, _, _ = self._hub_anchor(len(vis), W, H)
        m = self.marked()
        left = len([x for x in self.groups if x.key not in self.done])
        free = sum(g.photos[j].size for j in m)
        pad = ts * 2

        nav_text = f" <  {self.i + 1}/{len(self.groups)}  {len(g.photos)} IMG  {left}L  > "
        row1 = pixelfont.render([nav_text], ts, WHITE, QColor(0, 0, 0, 220))
        w1, h1 = row1.width(), row1.height() + pad

        row2 = None
        action_kind = ""
        if m and self.mode == PER_GROUP:
            label = f"{len(m)} X DEL  {human(free)} "
            raw2 = pixelfont.render_bits(pixelfont.with_icon("trash", label), ts, QColor(0, 0, 0), WHITE)
            row2 = QImage(raw2.width() + pad, raw2.height() + pad, QImage.Format_RGBA8888)
            row2.fill(WHITE)
            p2 = QPainter(row2)
            p2.drawImage(QPoint(pad // 2, pad // 2), raw2)
            p2.end()
            action_kind = "fire"
        elif m and self.mode == AT_END:
            label = f" {len(m)} X MARKED  NEXT > "
            raw2 = pixelfont.render([label], ts, QColor(0, 0, 0), WHITE)
            row2 = QImage(raw2.width() + pad, raw2.height() + pad, QImage.Format_RGBA8888)
            row2.fill(WHITE)
            p2 = QPainter(row2)
            p2.drawImage(QPoint(pad // 2, pad // 2), raw2)
            p2.end()
            action_kind = "next_end"
        elif not m and len(g.photos) >= 3 and len(vis) >= 3:
            raw2 = pixelfont.render([" KEEP BEST "], ts, WHITE, QColor(0, 0, 0, 220))
            row2 = QImage(raw2.width() + pad, raw2.height() + pad, QImage.Format_RGBA8888)
            row2.fill(QColor(0, 0, 0, 220))
            p2 = QPainter(row2)
            p2.drawImage(QPoint(pad // 2, pad // 2), raw2)
            p2.end()
            action_kind = "keep_best"

        w2 = row2.width() if row2 is not None else 0
        h2 = row2.height() if row2 is not None else 0
        hub_w = max(w1, w2)
        hub_h = h1 + (GAP + h2 if h2 else 0)
        if hub_cy < 0:
            hub_x = (W - hub_w) / 2
            hub_y = pad
        else:
            hub_x = min(max(pad, hub_cx - hub_w / 2), W - hub_w - pad)
            hub_y = min(max(pad, hub_cy - hub_h / 2), H - hub_h - pad)
        self.hub_rect = QRectF(hub_x, hub_y, hub_w, hub_h)

        r1_x = int(hub_x + (hub_w - w1) / 2)
        r1_y = int(hub_y)
        qp.fillRect(QRectF(r1_x, r1_y, w1, h1), QColor(0, 0, 0, 220))
        qp.drawImage(QPoint(r1_x, r1_y + pad // 2), row1)
        char_w = 8 * ts
        self.prev_rect = QRectF(r1_x, r1_y, char_w * 3, h1)
        self.next_rect = QRectF(r1_x + w1 - char_w * 3, r1_y, char_w * 3, h1)

        if row2 is not None:
            r2_x = int(hub_x + (hub_w - w2) / 2)
            r2_y = int(hub_y + h1 + GAP)
            qp.drawImage(QPoint(r2_x, r2_y), row2)
            r2_rect = QRectF(r2_x, r2_y, w2, h2)
            if action_kind == "fire":
                self.fire_rect = r2_rect
            elif action_kind == "keep_best":
                self.keep_best_rect = r2_rect
            elif action_kind == "next_end":
                self.next_rect = r2_rect

    # ------------------------------------------------------------ input
    def _hit(self, pt: QPointF) -> int:
        for j, r in enumerate(self.rects):
            if not r.isNull() and r.contains(pt):
                return j
        return -1

    def wheelEvent(self, e):
        j = self._hit(e.position())
        if j < 0 and self.rects:
            j = min((k for k, r in enumerate(self.rects) if not r.isNull()),
                    key=lambda k: (self.rects[k].center() - e.position()).manhattanLength(), default=-1)
        g = self.group
        if j < 0 or g is None:
            return
        cell = self.rects[j]
        p = g.photos[j]
        iw, ih = self._dims(p, self._thumb(p.path))
        s, src = self._view(cell, iw, ih)
        # image point under the mouse, as a fraction - it must stay under the mouse
        fx = (src.left() + (e.position().x() - cell.left()) / s) / iw
        fy = (src.top() + (e.position().y() - cell.top()) / s) / ih
        f = 1.25 ** (e.angleDelta().y() / 120)
        self.zoom = min(MAX_ZOOM, max(1.0, self.zoom * f))
        s2, src2 = self._view(cell, iw, ih)
        cx = fx - ((e.position().x() - cell.left()) / s2 - src2.width() / 2) / iw
        cy = fy - ((e.position().y() - cell.top()) / s2 - src2.height() / 2) / ih
        self.center = (cx, cy)
        self._clamp_center()
        self.update()

    def _pan_btn(self):
        return Qt.RightButton if self.pan == "right" else Qt.MiddleButton

    def _blink_btn(self):
        return Qt.MiddleButton if self.pan == "right" else Qt.RightButton

    def _hit_kill(self, pt: QPointF) -> int:
        for j, r in self.kill_rects.items():
            if not r.isNull() and r.contains(pt):
                return j
        if not self.kill_rect.isNull() and self.kill_rect.contains(pt):
            return self.kill_j
        return -1

    def _hit_only(self, pt: QPointF) -> int:
        for j, r in self.only_rects.items():
            if not r.isNull() and r.contains(pt):
                return j
        return -1

    def mousePressEvent(self, e):
        b = e.button()
        pt = e.position()
        if b in (Qt.MiddleButton, Qt.RightButton):
            self._press, self._dragged = pt, False
            if b == self._blink_btn():
                self.swap = 1
                self.update()
        elif b == Qt.LeftButton:
            g = self.group
            kj = self._hit_kill(pt)
            oj = self._hit_only(pt)
            if g is not None and len(g.photos) > 1 and kj >= 0:
                self.kill_requested.emit(self.i, g.photos[kj].path)
            elif g is not None and len(g.photos) >= 3 and oj >= 0:
                self.keep_only(oj)
            elif not self.fire_rect.isNull() and self.fire_rect.contains(pt):
                self.fire()
            elif not self.keep_best_rect.isNull() and self.keep_best_rect.contains(pt):
                self.keep_best()
            elif not self.prev_rect.isNull() and self.prev_rect.contains(pt):
                self.step(-1, wrap=True)
            elif not self.next_rect.isNull() and self.next_rect.contains(pt):
                self.step(1, wrap=True)
            else:
                self.toggle(self._hit(pt))
        elif b in (Qt.BackButton, Qt.ForwardButton):
            self.flip(-1 if b == Qt.BackButton else 1)

    def mouseMoveEvent(self, e):
        h = self._hit(e.position())
        if h != self.hover:
            self.hover = h
            self.update()                        # the skull follows the mouse
        if self._press is None:
            return
        d = e.position() - self._press
        if not self._dragged and d.manhattanLength() > 3:
            self._dragged = True
            if self.swap != 0:
                self.swap = 0
        g = self.group
        if g is None or not self.rects:
            return
        cell_idx = self.hover if 0 <= self.hover < len(self.rects) and not self.rects[self.hover].isNull() else (
            self.solo if self.solo is not None and 0 <= self.solo < len(self.rects) else
            next((k for k, r in enumerate(self.rects) if not r.isNull()), -1)
        )
        if cell_idx < 0 or self.rects[cell_idx].isNull():
            return
        p = g.photos[cell_idx]
        iw, ih = self._dims(p, self._thumb(p.path))
        s, _ = self._view(self.rects[cell_idx], iw, ih)
        self.center = (self.center[0] - d.x() / s / iw, self.center[1] - d.y() / s / ih)
        self._clamp_center()
        self._press = e.position()
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() in (Qt.MiddleButton, Qt.RightButton):
            self._press = None
            self._dragged = False
            if self.swap != 0:
                self.swap = 0
                self.update()

    def mouseDoubleClickEvent(self, e):
        pt = e.position()
        if self._hit_kill(pt) >= 0:
            return
        self.mousePressEvent(e)          # fast clicking toggles or fires KEEP BEST -> DELETE

    def focusNextPrevChild(self, next_: bool) -> bool:
        return False                     # Tab belongs to the blink compare

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key_Space, Qt.Key_Right, Qt.Key_Down, Qt.Key_PageDown):
            self.step(-1 if e.modifiers() & Qt.ShiftModifier else 1, wrap=(k == Qt.Key_Space))
        elif k in (Qt.Key_Left, Qt.Key_Up, Qt.Key_PageUp):
            self.step(-1)
        elif k in (Qt.Key_Tab, Qt.Key_Backtab):
            self.flip(-1 if k == Qt.Key_Backtab else 1)
        elif k in (Qt.Key_Delete, Qt.Key_Backspace):
            if self.mode == PER_GROUP:
                self.fire()
        elif k in (Qt.Key_0, Qt.Key_Home):
            self.reset_view()
        elif k == Qt.Key_Escape:
            self.back.emit()
        else:
            super().keyPressEvent(e)
