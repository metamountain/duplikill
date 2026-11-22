"""Reviewing one group: all images edge to edge, black and white, no frames - an image compare.

Wheel        = zoom all images together into the same spot (pixel exact from 100 %, no smoothing)
Middle drag  = move the section in all images        Esc = back to the overview
Left click   = toggle the delete cross               Del / Backspace / LOESCHEN button = delete them, next
Hold right   = blink compare: every cell shows the next image of the group, same spot and zoom
Tab / mouse side buttons = one image alone / flip to the next one
Space / Right arrow = next group, Left arrow = previous
0 / Home = fit
Ctrl+Z = undo (handled by the window)
"""
from __future__ import annotations

import os

from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from . import pixelfont
from .groups import Group

THUMB = 560
WHITE = QColor(255, 255, 255)
GREY = QColor(150, 150, 150)
BLACK_BG = QColor(0, 0, 0, 200)
PER_GROUP, AT_END = "group", "end"       # when the delete command comes (setting)
GAP = 2                                   # black seam between images, no other margin
MAX_ZOOM = 64.0


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


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
    done = Signal(str, QImage)


_POOL = ThreadPoolExecutor(max_workers=4)
FULL_BUDGET = 1500 * 2 ** 20     # bytes for decoded originals of one group; big groups get scaled-down copies


def _read(path: str, max_pixels: int) -> QImage:
    r = QImageReader(path)
    r.setAutoTransform(True)                     # same orientation as the thumbnails
    size = r.size()
    if size.isValid() and size.width() * size.height() > max_pixels:
        f = (max_pixels / (size.width() * size.height())) ** 0.5
        r.setScaledSize(size * f)
    return r.read()


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
        self.i = 0
        self.hover = -1
        self.solo: int | None = None                   # one image alone
        self.swap = 0                                  # 1 while the right button is held (blink)
        self.zoom = 1.0                                # 1 = whole image fits its cell
        self.center = (0.5, 0.5)                       # shown spot, in image fractions (same for all)
        self.rects: list[QRectF] = []
        self.pix: dict[str, QPixmap] = {}
        self.full: dict[str, QImage] = {}              # originals of the current group, loaded on zoom
        self._loading: set[str] = set()
        self._loaded = _Loaded()
        self._loaded.done.connect(self._full_ready)
        self._press: QPointF | None = None
        self._dragged = False
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        thumbs.ready.connect(self._ready)

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
        self.pix.clear()                    # only the current group's pixmaps stay in memory
        self.reset_view()
        for k in range(self.i, min(len(self.groups), self.i + 4)):     # preload ahead
            for p in self.groups[k].photos:
                self.thumbs.request(p.path, THUMB, priority=5 if k == self.i else 1)
        self.moved.emit(self.i)

    def step(self, d: int):
        """Next/previous group that is not done yet."""
        n = len(self.groups)
        j = self.i
        for _ in range(n):
            j += d
            if not 0 <= j < n:
                return
            if self.groups[j].key not in self.done:
                self.show_group(j)
                return

    def toggle(self, j: int):
        g = self.group
        if g is None or not 0 <= j < len(g.photos):
            return
        m = self.marked()
        if j in m:
            m.discard(j)
        elif len(m) < len(g.photos) - 1:            # one image always stays
            m.add(j)
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
        if img is None and path not in self._loading:
            self._loading.add(path)
            sig = self._loaded.done
            n = len(self.group.photos) if self.group else 1
            budget = FULL_BUDGET // 4 // max(1, n)           # pixels per image (4 bytes each)
            _POOL.submit(lambda: sig.emit(path, _read(path, budget)))
        return None if img is None or img.isNull() else img

    def _full_ready(self, path: str, img: QImage):
        self._loading.discard(path)
        g = self.group
        if g and any(p.path == path for p in g.photos):      # still the same group
            self.full[path] = img
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

    def paintEvent(self, e):
        self.kill_rect = QRectF()
        qp = QPainter(self)
        qp.fillRect(self.rect(), QColor(0, 0, 0))
        g = self.group
        if g is None:
            return
        vis = self._visible()
        rects = cells(len(vis), self.width(), self.height())
        self.rects = [QRectF() for _ in g.photos]
        m = self.marked()
        ts = self._ts()
        for j0, cell in zip(vis, rects):
            self.rects[j0] = cell
            j = (j0 + self.swap) % len(g.photos)        # held right button: the next image in this cell
            p = g.photos[j]
            thumb = self._thumb(p.path)
            iw, ih = self._dims(p, thumb)
            s, src = self._view(cell, iw, ih)
            qp.save()
            qp.setClipRect(cell)
            # what the source rect covers on screen (may be smaller than the cell when zoomed out)
            img_rect = QRectF(cell.center().x() - iw * s / 2 + (0.5 - self.center[0]) * iw * s,
                              cell.center().y() - ih * s / 2 + (0.5 - self.center[1]) * ih * s, iw * s, ih * s)
            need_full = thumb is None or s * iw > thumb.width() * 1.05
            orig = self._original(p.path) if need_full and self.zoom > 1.0 else None
            if orig is not None:
                qp.setRenderHint(QPainter.SmoothPixmapTransform, s < 1.0)   # from 100 %: exact pixels
                qp.drawImage(img_rect, orig, QRectF(orig.rect()))
            elif thumb is not None:
                qp.setRenderHint(QPainter.SmoothPixmapTransform, True)
                qp.drawPixmap(img_rect, thumb, QRectF(thumb.rect()))
            if j in m:
                self._cross(qp, img_rect.intersected(cell))
            if j0 == self.hover and self.swap == 0 and len(g.photos) > 1:
                self._skull(qp, img_rect.intersected(cell), j, ts)
            folder = os.path.basename(os.path.dirname(p.path))
            zoom_txt = f"   {s * 100:.0f} %" if self.zoom > 1.0 else ""
            cap = pixelfont.render([f"{int(iw)}X{int(ih)}  {human(p.size)}{zoom_txt}", folder],
                                   ts, GREY if j in m else WHITE, BLACK_BG)
            qp.drawImage(QPoint(int(cell.left()), int(cell.bottom()) - cap.height()), cap)
            qp.restore()
        self._status(qp, g, ts)

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

    def _skull(self, qp: QPainter, rect: QRectF, j: int, ts: int):
        """Pixel skull top left on the image under the mouse: click = this image goes at once."""
        icon = pixelfont.render_bits(pixelfont.icon("skull"), ts * 3, WHITE, QColor(0, 0, 0, 170))
        pad = ts * 2
        x, y = int(rect.left()) + pad, int(rect.top()) + pad
        qp.fillRect(QRectF(x - pad, y - pad, icon.width() + 2 * pad, icon.height() + 2 * pad), QColor(0, 0, 0, 170))
        qp.drawImage(QPoint(x, y), icon)
        self.kill_rect = QRectF(x - pad, y - pad, icon.width() + 2 * pad, icon.height() + 2 * pad)
        self.kill_j = j

    def remove_photo(self, path: str) -> int:
        """Drops one picture from the current group (after the skull). Returns its old index."""
        g = self.group
        j = next(k for k, p in enumerate(g.photos) if p.path == path)
        del g.photos[j]
        m = self.marked()
        self.marks[g.key] = {k - (k > j) for k in m if k != j}
        if self.solo is not None:
            self.solo = min(self.solo, len(g.photos) - 1)
        from .groups import pick_best
        g.best = pick_best(g)
        self.hover = -1
        self.update()
        return j

    def restore_photo(self, i: int, j: int, photo):
        g = self.groups[i]
        g.photos.insert(j, photo)
        from .groups import pick_best
        g.best = pick_best(g)
        self.marks[g.key] = {k + (k >= j) for k in self.marks.get(g.key, set())}

    def _status(self, qp: QPainter, g: Group, ts: int):
        m = self.marked()
        left = len([x for x in self.groups if x.key not in self.done])
        free = sum(g.photos[j].size for j in m)
        text = (f"GROUP {self.i + 1}/{len(self.groups)}  {len(g.photos)} IMAGES  "
                f"{len(m)} X  {human(free)}  LEFT {left}")
        img = pixelfont.render([text], ts, WHITE, BLACK_BG)
        qp.drawImage(QPoint(self.width() - img.width(), 0), img)
        # the delete button: only while something is crossed out (per-group mode)
        self.fire_rect = QRectF()
        if m and self.mode == PER_GROUP:
            big = ts * 2
            label = f"{len(m)} X DELETE  {human(free)} "
            # inverted: black on white, pixel trash can in front
            btn = pixelfont.render_bits(pixelfont.with_icon("trash", label), big, QColor(0, 0, 0), WHITE)
            x = (self.width() - btn.width()) // 2
            y = int(self.height() * 0.88) - btn.height()            # above the captions
            qp.drawImage(QPoint(x, y), btn)
            self.fire_rect = QRectF(x, y, btn.width(), btn.height())

    # ------------------------------------------------------------ input
    def _hit(self, pt: QPointF) -> int:
        for j, r in enumerate(self.rects):
            if not r.isNull() and r.contains(pt):
                return j
        return -1

    def wheelEvent(self, e):
        j = self._hit(e.position())
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

    def mousePressEvent(self, e):
        b = e.button()
        if b == Qt.MiddleButton:
            self._press, self._dragged = e.position(), False
        elif b == Qt.LeftButton:
            g = self.group
            if (g is not None and len(g.photos) > 1 and self.hover >= 0
                    and getattr(self, "kill_rect", QRectF()).contains(e.position())):
                self.kill_requested.emit(self.i, g.photos[self.kill_j].path)
            elif getattr(self, "fire_rect", QRectF()).contains(e.position()):
                self.fire()
            else:
                self.toggle(self._hit(e.position()))
        elif b == Qt.RightButton:
            self.swap = 1
            self.update()
        elif b in (Qt.BackButton, Qt.ForwardButton):
            self.flip(-1 if b == Qt.BackButton else 1)

    def mouseMoveEvent(self, e):
        h = self._hit(e.position())
        if h != self.hover:
            self.hover = h
            self.update()                        # the skull follows the mouse
        if self._press is None or self.hover < 0:
            return
        d = e.position() - self._press
        g = self.group
        p = g.photos[self.hover]
        iw, ih = self._dims(p, self._thumb(p.path))
        s, _ = self._view(self.rects[self.hover], iw, ih)
        self.center = (self.center[0] - d.x() / s / iw, self.center[1] - d.y() / s / ih)
        self._clamp_center()
        self._press = e.position()
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MiddleButton:
            self._press = None
        elif e.button() == Qt.RightButton:
            self.swap = 0
            self.update()

    def mouseDoubleClickEvent(self, e):
        self.mousePressEvent(e)          # fast clicking just toggles again

    def focusNextPrevChild(self, next_: bool) -> bool:
        return False                     # Tab belongs to the blink compare

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key_Space, Qt.Key_Right, Qt.Key_Down, Qt.Key_PageDown):
            self.step(-1 if e.modifiers() & Qt.ShiftModifier else 1)
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
