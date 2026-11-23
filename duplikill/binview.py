"""BIN: every removed image as a thumbnail, newest first. Click = mark, R = back to where it was.

The bin is a normal folder (<drive>:\\Duplikill-Bin), so it can also be looked at or deleted in Explorer;
images deleted there are simply not listed. Restoring uses the move journal, also days later.
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

from . import pixelfont
from .actions import Mover
from .index import human

WHITE, GREY, BLACK_BG = QColor(255, 255, 255), QColor(150, 150, 150), QColor(0, 0, 0, 200)
CELL = 260                   # target cell width in pixels
THUMB = 320
GAP = 2


class BinView(QWidget):
    back = Signal()
    restored = Signal(list)              # restored original paths

    def __init__(self, thumbs, mover: Mover, parent=None):
        super().__init__(parent)
        self.thumbs, self.mover = thumbs, mover
        self.items: list[dict] = []
        self.marked: set[int] = set()
        self.scroll = 0.0
        self.hover = -1
        self.rects: list[QRectF] = []
        self.msg, self.msg_until = "", 0.0
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        thumbs.ready.connect(lambda _p: self.update())

    def reload(self):
        self.items = self.mover.bin_items()
        self.marked.clear()
        self.scroll = 0.0
        self.update()

    # ------------------------------------------------------------ layout
    def _ts(self) -> int:
        return max(2, min(self.width(), self.height()) // 450)

    def _grid(self) -> tuple[int, float, float, float]:
        """columns, cell width, cell height, top of the grid"""
        cols = max(1, self.width() // CELL)
        cw = (self.width() - GAP * (cols - 1)) / cols
        return cols, cw, cw * 0.75, 8 * self._ts() * 3

    def _max_scroll(self) -> float:
        cols, _, ch, top = self._grid()
        rows = (len(self.items) + cols - 1) // cols
        return max(0.0, top + rows * (ch + GAP) - self.height() + 8 * self._ts() * 3)

    # ------------------------------------------------------------ paint
    def paintEvent(self, e):
        qp = QPainter(self)
        qp.fillRect(self.rect(), QColor(0, 0, 0))
        ts = self._ts()
        cols, cw, ch, top = self._grid()
        self.rects = [QRectF()] * len(self.items)
        first = max(0, int((self.scroll - top) // (ch + GAP)) * cols)
        for k in range(first, len(self.items)):
            r, c = divmod(k, cols)
            y = top + r * (ch + GAP) - self.scroll
            if y > self.height():
                break
            cell = QRectF(c * (cw + GAP), y, cw, ch)
            self.rects[k] = cell
            it = self.items[k]
            img = self.thumbs.get(it["dst"], THUMB)
            if img is not None and not img.isNull():
                s = min(cw / img.width(), ch / img.height())
                w, h = img.width() * s, img.height() * s
                qp.setRenderHint(QPainter.SmoothPixmapTransform, True)
                qp.drawImage(QRectF(cell.center().x() - w / 2, cell.center().y() - h / 2, w, h), img)
            folder = os.path.basename(os.path.dirname(it["src"]))
            if k in self.marked:
                qp.fillRect(cell, QColor(0, 0, 0, 120))
                lab = pixelfont.render([" RESTORE "], ts, QColor(0, 0, 0), WHITE)
                qp.drawImage(QPoint(int(cell.center().x() - lab.width() / 2), int(cell.center().y() - lab.height() / 2)), lab)
            cap = pixelfont.render([f"{folder}"[:int(cw // (8 * ts))]], ts, WHITE if k == self.hover else GREY, BLACK_BG)
            qp.drawImage(QPoint(int(cell.left()), int(cell.bottom()) - cap.height()), cap)
        # header and footer (on top of the grid)
        n, b = len(self.items), sum(i["size"] for i in self.items)
        head = f"BIN: {n} IMAGES, {human(b)}" if n else "BIN IS EMPTY"
        if self.marked:
            head += f"   {len(self.marked)} MARKED"
        qp.fillRect(0, 0, self.width(), top, QColor(0, 0, 0))
        qp.drawImage(QPoint(3 * ts, 8 * ts), pixelfont.render([head], ts, WHITE))
        foot = "CLICK: MARK   R: RESTORE MARKED   O: OPEN FOLDER   ESC: BACK"
        if time.time() < self.msg_until:
            foot = self.msg
        fimg = pixelfont.render([foot], ts, WHITE, QColor(0, 0, 0, 230))
        qp.drawImage(QPoint((self.width() - fimg.width()) // 2, self.height() - fimg.height() - 3 * ts), fimg)

    # ------------------------------------------------------------ input
    def _hit(self, pos) -> int:
        for k, r in enumerate(self.rects):
            if not r.isNull() and r.contains(pos):
                return k
        return -1

    def mousePressEvent(self, e):
        k = self._hit(e.position())
        if e.button() == Qt.LeftButton and k >= 0:
            self.marked ^= {k}
            self.update()

    def mouseMoveEvent(self, e):
        k = self._hit(e.position())
        if k != self.hover:
            self.hover = k
            self.update()

    def wheelEvent(self, e):
        self.scroll = min(self._max_scroll(), max(0.0, self.scroll - e.angleDelta().y()))
        self.update()

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key_Escape:
            self.back.emit()
        elif k == Qt.Key_R:
            self.restore_marked()
        elif k == Qt.Key_O:
            self.open_folder()
        elif k in (Qt.Key_Space, Qt.Key_PageDown, Qt.Key_Down):
            self.scroll = min(self._max_scroll(), self.scroll + self.height() * (0.8 if k != Qt.Key_Down else 0.25))
            self.update()
        elif k in (Qt.Key_PageUp, Qt.Key_Up):
            self.scroll = max(0.0, self.scroll - self.height() * (0.8 if k == Qt.Key_PageUp else 0.25))
            self.update()

    def _say(self, text: str):
        self.msg, self.msg_until = text, time.time() + 3
        self.update()

    def restore_marked(self):
        if not self.marked:
            self._say("CLICK IMAGES FIRST, THEN R")
            return
        srcs = [self.items[k]["src"] for k in sorted(self.marked)]
        done = self.mover.restore(srcs)
        self.reload()
        self._say(f"{len(done)} IMAGE(S) BACK IN PLACE" + ("" if len(done) == len(srcs) else
                                                       f", {len(srcs) - len(done)} NOT (ORIGINAL NAME TAKEN?)"))
        if done:
            self.restored.emit(done)

    def open_folder(self):
        it = self.items[self.hover] if 0 <= self.hover < len(self.items) else (self.items[0] if self.items else None)
        if it is None:
            return
        d = os.path.dirname(it["dst"])
        if os.path.isdir(d):
            os.startfile(d)
