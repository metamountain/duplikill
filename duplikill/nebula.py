"""Overview as a floating net, black and white, minimal.

Every group is one tight block of thumbnails that floats as a unit; groups sharing a folder are joined
by thin straight lines. The more images, the smaller the thumbnails. In the middle the numbers in an
8x8 pixel font (pixelfont.py), enlarged only by whole numbers - no antialiasing.
"""
from __future__ import annotations

import math
import os
import random

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from . import pixelfont
from .clumps import Clumps
from .groups import Group

THUMB_SIZE = 192      # loaded thumbnail size (long edge)
ABSTRACT_GROUPS = 80   # more groups (or images) than this: pixel blocks instead of thumbnails,
ABSTRACT_IMAGES = 250  # real thumbnails only for the group under the mouse
REPULSION_EVERY = 3    # with many groups the pairwise forces are recomputed only every few steps
FPS = 30
WHITE = QColor(255, 255, 255)


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _grid(k: int) -> tuple[int, int]:
    cols = math.ceil(math.sqrt(k * 1.3))          # a bit wider than tall
    return cols, math.ceil(k / cols)


class Nebula(QWidget):
    group_clicked = Signal(int)

    def __init__(self, groups: list[Group], thumbs, parent=None, root: str = ""):
        super().__init__(parent)
        self.root_name = (os.path.basename(root.rstrip("\\/")) or root).upper()[:24]
        self.groups, self.thumbs = groups, thumbs
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.pix: dict[str, QPixmap] = {}
        self.hover = -1
        self.t = 0.0
        self.scale, self.offset = 1.0, QPointF(0, 0)
        self.auto_fit = True
        self._drag = None
        thumbs.ready.connect(self._ready)

        n = len(groups)
        rng = np.random.default_rng(7)
        self.k = np.array([len(g.photos) for g in groups], float)
        self.phase = rng.random(n) * math.tau
        self._labels()
        self.edges = self._edges()
        self.alive = np.ones(n, bool)
        ang, rad = rng.random(n) * math.tau, np.sqrt(rng.random(n)) * 40   # start small, fly apart
        self.pos = np.stack([np.cos(ang) * rad, np.sin(ang) * rad], 1)
        self.vel = np.zeros((n, 2))
        self.alpha = 1.0
        self.total_imgs = int(self.k.sum())
        self.saving = sum(g.bytes - g.photos[g.best].size for g in groups)
        self.folders = len({os.path.dirname(p.path) for g in groups for p in g.photos})
        self.n_live = len(groups)
        self.text_scale = 0
        self.text_img = None
        self.cursor_img = None
        self.hole = 100.0
        self._layout_sizes(1600, 900)

        self.abstract = len(groups) > ABSTRACT_GROUPS or self.total_imgs > ABSTRACT_IMAGES
        if self.abstract:
            self.clumps = Clumps(groups, self.group_folder, len(self.folder_names))
            self.clump_rects = []
            self._clump_size = None
        self._steps = 0
        self._rep = None
        if not self.abstract:
            for g in groups:
                for p in g.photos:
                    thumbs.request(p.path, THUMB_SIZE, priority=1)
        self.timer = QTimer(self, interval=1000 // FPS, timeout=self._tick)
        self.timer.start()

    def hide(self, i: int):
        """Group i is done: it leaves the net, the numbers in the middle count down."""
        if 0 <= i < len(self.alive) and self.alive[i]:
            self.alive[i] = False
            live = [g for g, a in zip(self.groups, self.alive) if a]
            self.total_imgs = sum(len(g.photos) for g in live)
            self.saving = sum(g.bytes - g.photos[g.best].size for g in live)
            self.folders = len({os.path.dirname(p.path) for g in live for p in g.photos})
            self.n_live = len(live)
            self.text_scale = 0            # re-render the text
            self.alpha = max(self.alpha, 0.3)
            if self.hover == i:
                self.hover = -1

    # ------------------------------------------------------------ model
    def _edges(self) -> np.ndarray:
        """Groups sharing a folder form a chain, plus a few cross links."""
        by_folder: dict[int, list[int]] = {}
        for i, f in enumerate(self.group_folder):
            by_folder.setdefault(f, []).append(i)
        e = set()
        rnd = random.Random(3)
        for members in by_folder.values():
            for a, b in zip(members, members[1:]):
                e.add((a, b))
            for _ in range(len(members) // 20):
                a, b = rnd.sample(members, 2)
                if a != b:
                    e.add((min(a, b), max(a, b)))
        return np.array(sorted(e), int).reshape(-1, 2)

    def _labels(self):
        """Folder names: a group belongs to its most common folder; it is shown under the top folder below
        the common root ('12 04 Berlin'), with one subfolder level only if everything is in one top folder."""
        dirs = [max({os.path.dirname(p.path) for p in g.photos},
                    key=lambda d: sum(os.path.dirname(p.path) == d for p in g.photos)) for g in self.groups]
        try:
            root = os.path.commonpath(list(set(dirs))) if dirs else ""
        except ValueError:
            root = ""
        rels = [os.path.relpath(d, root).replace("\\", "/") if root else d for d in dirs]
        tops = {r.split("/")[0] for r in rels}
        depth = 2 if len(tops) == 1 and len(set(rels)) > 1 else 1
        self.group_folder = []
        names: dict[str, int] = {}
        for d, r in zip(dirs, rels):
            name = os.path.basename(d) if r in (".", "") else "/".join(r.split("/")[:depth])
            self.group_folder.append(names.setdefault(name, len(names)))
        self.folder_names = list(names)
        self.folder_img = {}

    def _layout_sizes(self, w: float, h: float):
        """Thumbnail edge so all images together cover ~30 % of the screen: more images -> smaller."""
        self.s = float(np.clip(math.sqrt(0.30 * w * h / max(1, self.total_imgs)), 10, 150))
        cr = np.array([_grid(int(k)) for k in self.k], float).reshape(-1, 2)
        self.r = 0.5 * self.s * 1.06 * np.hypot(cr[:, 0], cr[:, 1])     # block radius

    def _step(self):
        idx = np.flatnonzero(self.alive)
        n = len(idx)
        if n == 0:
            return
        p, r = self.pos[idx], self.r[idx]
        remap = -np.ones(len(self.alive), int)
        remap[idx] = np.arange(n)
        f = -0.004 * p                                              # gravity to the middle
        self._steps += 1
        if n > 1:
            every = 1 if n <= 150 else REPULSION_EVERY
            if self._rep is None or len(self._rep) != n or self._steps % every == 0:
                q, rq = p.astype(np.float32), r.astype(np.float32)
                d = q[:, None, :] - q[None, :, :]
                dist2 = np.einsum("ijk,ijk->ij", d, d) + 1e-3
                np.fill_diagonal(dist2, np.inf)
                rr = (rq[:, None] + rq[None, :]) * 1.05
                dist = np.sqrt(dist2)
                w = (rr * rr / dist2) * 0.35 / dist + np.clip(rr - dist, 0, None) / dist * 0.5
                self._rep = np.einsum("ij,ijk->ik", w, d).astype(float)   # soft repulsion + hard collision
            f += self._rep
        e = remap[self.edges] if len(self.edges) else self.edges
        e = e[(e >= 0).all(1)] if len(e) else e
        if len(e):
            a, b = e[:, 0], e[:, 1]
            d = p[b] - p[a]
            dist = np.linalg.norm(d, axis=1) + 1e-6
            rest = (r[a] + r[b]) * 1.2
            fs = ((dist - rest) * 0.03 / dist)[:, None] * d
            np.add.at(f, a, fs)
            np.add.at(f, b, -fs)
        vel = (self.vel[idx] + f * self.alpha) * 0.82
        pos = p + vel
        # the middle stays free for the text: whoever is inside is put on the rim
        rad = np.linalg.norm(pos, axis=1) + 1e-6
        inside = rad < self.hole + r
        if inside.any():
            pos[inside] *= ((self.hole + r[inside]) / rad[inside])[:, None]
            vel[inside] *= 0.3
        self.pos[idx], self.vel[idx] = pos, vel
        self.alpha = max(0.08, self.alpha * 0.993)

    # ------------------------------------------------------------ text (pixel font)
    def _lines(self) -> list[str]:
        return ["**** DUPLIKILL ****",
                "",
                *([self.root_name, ""] if self.root_name else []),
                f"{self.n_live} GROUPS",
                f"{self.total_imgs} IMAGES",
                f"{self.folders} FOLDERS",
                f"{_human(self.saving)} TO FREE",
                "",
                "READY."]

    def _update_text(self):
        cols = max(len(l) for l in self._lines())
        # whole numbers only; the text takes at most ~40 % of the short screen side
        scale = max(2, int(0.40 * min(self.width(), self.height()) / (cols * 8)))
        if scale != self.text_scale:
            self.text_scale = scale
            self.text_img = pixelfont.render(self._lines(), scale)
            self.cursor_img = pixelfont.render([" "], scale, WHITE, WHITE)  # solid block
        # hole radius in world units = half the text width on screen + margin
        r_px = max(self.text_img.width(), self.text_img.height()) / 2 + 8 * self.text_scale
        self.hole = r_px / max(self.scale, 1e-3)

    def _draw_text(self, qp: QPainter):
        img = self.text_img
        tl = QPoint(int(round(self.offset.x() - img.width() / 2)), int(round(self.offset.y() - img.height() / 2)))
        qp.drawImage(tl, img)                                       # 1:1, never scaled
        if int(self.t * 2) % 2 == 0:                                # blinking cursor under READY.
            qp.drawImage(QPoint(tl.x(), tl.y() + len(self._lines()) * 8 * self.text_scale), self.cursor_img)

    # ------------------------------------------------------------ view
    def _fit(self):
        if not self.alive.any():
            target_sc, target = 1.0, QPointF(self.width() / 2, self.height() / 2)
        else:
            P, R = self.pos[self.alive], self.r[self.alive][:, None]
            lo = np.minimum((P - R).min(0), -self.hole)
            hi = np.maximum((P + R).max(0), self.hole)
            w, h = max(1, hi[0] - lo[0]), max(1, hi[1] - lo[1])
            target_sc = min(min(self.width() / w, self.height() / h) * 0.92, 1.6)
            cx, cy = (lo + hi) / 2
            target = QPointF(self.width() / 2 - cx * target_sc, self.height() / 2 - cy * target_sc)
        self.scale += (target_sc - self.scale) * 0.08
        self.offset += (target - self.offset) * 0.08

    def _to_world(self, pt: QPointF) -> np.ndarray:
        return np.array([(pt.x() - self.offset.x()) / self.scale, (pt.y() - self.offset.y()) / self.scale])

    def _hit(self, pt: QPointF) -> int:
        if len(self.groups) == 0:
            return -1
        if self.abstract:
            u = self.clumps.u
            for i, (x, y, c, r) in enumerate(self.clump_rects):
                if self.alive[i] and c and x - 1 <= pt.x() < x + c * u + 1 and y - 1 <= pt.y() < y + r * u + 1:
                    return i
            return -1
        d = np.linalg.norm(self.pos - self._to_world(pt), axis=1) - self.r
        d[~self.alive] = np.inf
        i = int(np.argmin(d))
        return i if d[i] < 0 else -1

    def fly_away(self, done, frames: int = 10):
        """Closing: every group shoots outwards (~0.3 s), then done() is called once."""
        self._away, self._away_frames, self._away_done = 0, frames, done
        self.auto_fit = False
        self.hover = -1

    def _tick(self):
        self.t += 1 / FPS
        self._update_text()
        if getattr(self, "_away", None) is not None and self.abstract:
            self._away += 1
            self.update()
            if self._away == self._away_frames:
                self.timer.stop()
                self._away_done()
            return
        if getattr(self, "_away", None) is not None:
            self._away += 1
            self.pos = self.pos * 1.32 + np.sign(self.pos) * 8      # accelerate away from the middle
            self.update()
            if self._away == self._away_frames:
                self.timer.stop()
                self._away_done()
            return
        if not self.abstract:
            self._step()
            if self.auto_fit:
                self._fit()
        self.update()

    def _ready(self, path):
        self.pix.pop(path, None)

    def _pixmap(self, path: str) -> QPixmap | None:
        pm = self.pix.get(path)
        if pm is None:
            img = self.thumbs.get(path, THUMB_SIZE)
            if img is None or img.isNull():
                return None
            pm = self.pix[path] = QPixmap.fromImage(img)
        return pm

    def resizeEvent(self, e):
        self._layout_sizes(max(800, self.width()), max(500, self.height()))
        self.text_scale = 0
        super().resizeEvent(e)

    # ------------------------------------------------------------ paint
    def paintEvent(self, e):
        if self.text_img is None:
            self._update_text()
        qp = QPainter(self)
        bg = QLinearGradient(0, 0, 0, self.height())
        bg.setColorAt(0, QColor(20, 20, 20))
        bg.setColorAt(1, QColor(0, 0, 0))
        qp.fillRect(self.rect(), bg)
        if self.abstract:
            self._paint_clumps(qp)
            self._draw_text(qp)
            if self.hover >= 0:
                self._draw_tip(qp, self.hover)
            return

        qp.save()
        qp.setRenderHint(QPainter.Antialiasing)
        qp.setRenderHint(QPainter.SmoothPixmapTransform, self.total_imgs < 1500)
        qp.translate(self.offset)
        qp.scale(self.scale, self.scale)
        c = self._wobble()
        self._draw_edges(qp, c)
        order = [i for i in range(len(self.groups)) if i != self.hover and self.alive[i]] + ([self.hover] if self.hover >= 0 else [])
        for i in order:
            self._draw_group(qp, i, c[i], i == self.hover)
        self._draw_labels(qp, c)
        qp.restore()
        self._draw_text(qp)
        if self.hover >= 0:
            self._draw_tip(qp, self.hover)

    def _wobble(self) -> np.ndarray:
        t, ph = self.t, self.phase
        amp = self.r * 0.10
        return self.pos + np.stack([np.sin(t * 0.7 + ph) * amp, np.cos(t * 0.53 + ph * 1.7) * amp], 1)

    def _draw_edges(self, qp: QPainter, c: np.ndarray):
        if not len(self.edges):
            return
        pen = QPen(QColor(255, 255, 255, 60), 1.0)
        pen.setCosmetic(True)                                       # 1 screen pixel at any zoom
        hot = QPen(QColor(255, 255, 255, 200), 1.0)
        hot.setCosmetic(True)
        for a, b in self.edges:
            if not (self.alive[a] and self.alive[b]):
                continue
            qp.setPen(hot if self.hover in (a, b) else pen)
            qp.drawLine(QPointF(*c[a]), QPointF(*c[b]))

    def _draw_labels(self, qp: QPainter, c: np.ndarray):
        """Folder names in the pixel font at the middle of their groups, 1:1, the biggest folders first;
        a name that would overlap another (or the numbers in the middle) is left out."""
        live = np.flatnonzero(self.alive)
        if not len(live) or len(self.folder_names) < 2:          # one folder: its name says nothing
            return
        fid = np.array(self.group_folder)[live]
        scale = max(1, self.text_scale // 3)
        taken = [QRectF(self.offset.x() - self.text_img.width() / 2, self.offset.y() - self.text_img.height() / 2,
                        self.text_img.width(), self.text_img.height())] if self.text_img else []
        qp.save()
        qp.resetTransform()
        counts = np.bincount(fid, weights=self.k[live])
        for f in np.argsort(-counts):
            if counts[f] <= 0:
                break
            members = live[fid == f]
            cx, cy = c[members].mean(0)
            key = (f, scale)
            img = self.folder_img.get(key)
            if img is None:
                img = self.folder_img[key] = pixelfont.render([self.folder_names[f].upper()], scale,
                                                              QColor(255, 255, 255), QColor(0, 0, 0, 0))
            x = int(round(self.offset.x() + cx * self.scale - img.width() / 2))
            top = (c[members, 1] - self.r[members]).min()             # above its cluster
            y = int(round(self.offset.y() + top * self.scale)) - img.height() - 4
            box = QRectF(x - 4, y - 2, img.width() + 8, img.height() + 4)
            if any(box.intersects(t) for t in taken):
                continue
            taken.append(box)
            qp.drawImage(QPoint(x, y), img)
        qp.restore()

    def _draw_group(self, qp: QPainter, i: int, c: np.ndarray, hot: bool):
        """One group = one tight block of images, floating as a unit. No frames, no effects."""
        if self.abstract and not hot:
            self._draw_block(qp, i, c)
            return
        g = self.groups[i]
        k = len(g.photos)
        s = self.s * (1.6 if hot else 1.0)
        gap = s * 0.04
        cols, rows = _grid(k)
        # all images of the block share one height, rows are packed edge to edge: no holes
        hh = s * 0.66
        items = []
        for p in g.photos:
            pm = self._pixmap(p.path)
            ar = pm.width() / max(1, pm.height()) if pm and not pm.isNull() else 1.5
            items.append((pm, hh * ar))
        rows_items = [items[r * cols:(r + 1) * cols] for r in range(rows)]
        H = rows * hh + (rows - 1) * gap
        qp.save()
        qp.translate(QPointF(*c))
        qp.rotate(3 * math.sin(self.t * 0.5 + self.phase[i]))
        y = -H / 2
        for row in rows_items:
            x = -(sum(w for _, w in row) + gap * (len(row) - 1)) / 2
            for pm, w in row:
                r = QRectF(x, y, w, hh)
                if pm and not pm.isNull():
                    qp.drawPixmap(r, pm, QRectF(pm.rect()))
                else:
                    qp.fillRect(r, QColor(40, 40, 40))
                x += w + gap
            y += hh + gap
        qp.restore()

    def _draw_block(self, qp: QPainter, i: int, c: np.ndarray):
        """Abstract mode: one grey pixel square per image, no thumbnails."""
        k = len(self.groups[i].photos)
        s = self.s * 0.8
        gap = max(1.0, self.s * 0.2)
        cols, rows = _grid(k)
        x0 = c[0] - (cols * s + (cols - 1) * gap) / 2
        y0 = c[1] - (rows * s + (rows - 1) * gap) / 2
        col = QColor(200, 200, 200)
        for n in range(k):
            r, q = divmod(n, cols)
            qp.fillRect(QRectF(x0 + q * (s + gap), y0 + r * (s + gap), s, s), col)

    def _paint_clumps(self, qp: QPainter):
        """Many groups: one clump per folder, one pixel square per image, folder name above each clump."""
        cl = self.clumps
        ls = max(1, self.text_scale // 3)
        if self._clump_size != (self.width(), self.height(), ls):
            self._clump_size = (self.width(), self.height(), ls)
            lw = [8 * ls * len(n) for n in self.folder_names]
            cl.layout(self.width(), self.height(), (self.text_img.width(), self.text_img.height()), 8 * ls + 6, lw)
        self.offset = QPointF(self.width() / 2, self.height() / 2)          # numbers in the middle
        u = cl.u
        nf = len(cl.members)
        ph = np.arange(nf) * 1.7
        off = np.stack([np.sin(self.t * 0.6 + ph), np.cos(self.t * 0.45 + ph * 1.3)], 1) * max(1, u // 2)
        if getattr(self, "_away", None) is not None:                   # closing: clumps shoot outwards
            d = cl.centre - np.array([self.width() / 2, self.height() / 2])
            off = off + d * (1.35 ** self._away - 1)
        self.clump_rects = cl.group_rects(u, off)
        sq = max(1, u - 1)
        grey, white = QColor(150, 150, 150), WHITE
        for i, (x, y, c, r) in enumerate(self.clump_rects):
            if not self.alive[i] or c == 0:
                continue
            col = white if i == self.hover else grey
            for n in range(len(self.groups[i].photos)):
                rr, cc = divmod(n, c)
                qp.fillRect(x + cc * u, y + rr * u, sq, sq, col)
        for k, m in enumerate(cl.members):
            live = [i for i in m if self.alive[i]]
            if not live:
                continue
            key = (k, ls)
            img = self.folder_img.get(key)
            if img is None:
                img = self.folder_img[key] = pixelfont.render([self.folder_names[k].upper()], ls, WHITE,
                                                              QColor(0, 0, 0, 0))
            top = min(self.clump_rects[i][1] for i in live)
            left = min(self.clump_rects[i][0] for i in live)
            right = max(self.clump_rects[i][0] + self.clump_rects[i][2] * u for i in live)
            qp.drawImage(QPoint(int((left + right) / 2 - img.width() / 2), top - img.height() - 4), img)

    def _draw_tip(self, qp: QPainter, i: int):
        g = self.groups[i]
        folders = sorted({os.path.basename(os.path.dirname(p.path)) for p in g.photos})
        lines = [f"{len(g.photos)} IMAGES  {_human(g.bytes)}"] + folders[:4]
        img = pixelfont.render(lines, max(1, self.text_scale // 2), WHITE, QColor(0, 0, 0, 255))
        pt = self.mapFromGlobal(self.cursor().pos())
        x = min(max(0, pt.x() + 20), self.width() - img.width())
        y = min(max(0, pt.y() + 20), self.height() - img.height())
        qp.drawImage(QPoint(x, y), img)

    # ------------------------------------------------------------ input
    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.offset += e.position() - self._drag
            self._drag = e.position()
            self.auto_fit = False
            return
        h = self._hit(e.position())
        if self.abstract and h >= 0 and h != self.hover:          # real thumbnails only on demand
            for p in self.groups[h].photos:
                self.thumbs.request(p.path, THUMB_SIZE, priority=0)
        self.hover = h
        self.setCursor(Qt.PointingHandCursor if self.hover >= 0 else Qt.ArrowCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            i = self._hit(e.position())
            if i >= 0:
                self.group_clicked.emit(i)
            elif not self.abstract:
                self._drag = e.position()

    def mouseReleaseEvent(self, e):
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        if self._hit(e.position()) < 0:
            self.auto_fit = True          # double click on empty space: fit everything again

    def wheelEvent(self, e):
        if self.abstract:
            return
        f = 1.0015 ** e.angleDelta().y()
        w = self._to_world(e.position())
        self.scale *= f
        self.offset = QPointF(e.position().x() - w[0] * self.scale, e.position().y() - w[1] * self.scale)
        self.auto_fit = False

    def leaveEvent(self, e):
        self.hover = -1
