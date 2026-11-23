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
from .index import human

THUMB_SIZE = 192      # loaded thumbnail size (long edge)
ABSTRACT_GROUPS = 80   # more groups (or images) than this: pixel blocks instead of thumbnails,
ABSTRACT_IMAGES = 250  # real thumbnails only for the group under the mouse
REPULSION_EVERY = 3    # with many groups the pairwise forces are recomputed only every few steps
FPS = 60
REVERB_HIST = 48
REVERB_TAPS = ((0, 0.60), (11, 0.35), (23, 0.20), (35, 0.10))
WHITE = QColor(255, 255, 255)
_SHADES = [QColor(v, v, v) for v in range(140, 256)]
_EDGE_PENS = []
for _a in range(55, 226, 18):
    _p = QPen(QColor(255, 255, 255, _a), 1.0)
    _p.setCosmetic(True)
    _EDGE_PENS.append(_p)


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
        self._c = self.pos.copy()
        self._mouse_in = False
        self._mouse_raw = np.zeros(2, float)
        self._mouse_smooth = np.zeros(2, float)
        self._mvel = np.zeros(2, float)
        self._mhead = 0
        self._mhist = np.zeros((REVERB_HIST, 2), float)
        self._vhist = np.zeros((REVERB_HIST, 2), float)
        self._mactive = np.zeros(REVERB_HIST, float)
        self._net_label_pos: dict[int, np.ndarray] = {}
        self._clump_label_pos: dict[int, np.ndarray] = {}
        self._local_rects: list[list[QRectF]] = []
        self._bg_pm: QPixmap | None = None
        self._tip_key = None
        self._tip_img: QImage | None = None
        if self.abstract:
            self.clumps = Clumps(groups, self.group_folder, len(self.folder_names))
            self.clump_rects = []
            self._clump_size = None
            nf = len(self.folder_names)
            self._cl_disp = np.zeros((nf, 2), float)
            self._cl_vel = np.zeros((nf, 2), float)
            self._c_disp = np.zeros((n, 2), float)
            self._c_vel = np.zeros((n, 2), float)
            self._c_glow = np.zeros(n, float)
            self._chot_idx = -1
            self._chot_val = 0.0
            self._chot_vel = 0.0
        else:
            self._disp = np.zeros((n, 2), float)
            self._dvel = np.zeros((n, 2), float)
            self._hot = np.zeros(n, float)
            self._hot_vel = np.zeros(n, float)
            self._thrill = np.zeros(n, float)
            for g in groups:
                for p in g.photos:
                    thumbs.request(p.path, THUMB_SIZE, priority=1)
        self._steps = 0
        self._rep = None
        self.timer = QTimer(self, interval=1000 // FPS, timeout=self._tick)
        self.timer.start()

    def recount(self):
        live = [g for g, a in zip(self.groups, self.alive) if a]
        self.total_imgs = sum(len(g.photos) for g in live)
        self.saving = sum(g.bytes - g.photos[g.best].size for g in live)
        self.folders = len({os.path.dirname(p.path) for g in live for p in g.photos})
        self.n_live = len(live)
        self.text_scale = 0            # re-render the text

    def hide(self, i: int):
        """Group i is done: it leaves the net, the numbers in the middle count down."""
        if 0 <= i < len(self.alive) and self.alive[i]:
            self.alive[i] = False
            self.recount()
            self.alpha = max(self.alpha, 0.3)
            if self.hover == i:
                self.hover = -1
            if self.abstract:
                self._c_glow[i] = 0.0
            else:
                self._hot[i] = self._hot_vel[i] = 0.0
                d = self.pos - self.pos[i]
                dist = np.linalg.norm(d, axis=1) + 1e-3
                w = np.exp(-dist * dist / (2.0 * max(self.r[i] * 3.0, 120.0) ** 2))
                w[i] = 0.0
                self._dvel += (d / dist[:, None]) * (w * 4.5)[:, None]
                self._thrill = np.clip(self._thrill + w * 0.7, 0.0, 1.0)

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
        self.alpha = max(0.08, self.alpha * 0.996)

    # ------------------------------------------------------------ text (pixel font)
    def _lines(self) -> list[str]:
        return ["**** DUPLIKILL ****",
                "",
                *([self.root_name, ""] if self.root_name else []),
                f"{self.total_imgs - self.n_live} DUPLICATES",          # one image per group stays
                f"{self.folders} FOLDERS",
                f"{human(self.saving)} TO FREE",
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
        self.scale += (target_sc - self.scale) * 0.06
        self.offset += (target - self.offset) * 0.06

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
        pos = self._c if len(self._c) == len(self.pos) else self.pos
        d = np.linalg.norm(pos - self._to_world(pt), axis=1) - self.r * (1.0 + self._hot)
        d[~self.alive] = np.inf
        i = int(np.argmin(d))
        return i if d[i] < 0 else -1

    def fly_away(self, done, frames: int = 10):
        """Closing: every group shoots outwards (~0.3 s), then done() is called once."""
        self._away, self._away_frames, self._away_done = 0, frames, done
        self.auto_fit = False
        self.hover = -1

    def _record_mouse(self, world: bool = False):
        if self._mouse_in:
            d = self._mouse_raw - self._mouse_smooth
            self._mouse_smooth = self._mouse_smooth + d * 0.48
            self._mvel = self._mvel * 0.72 + d * 0.28
        else:
            self._mvel *= 0.78
        self._mhead = (self._mhead + 1) % REVERB_HIST
        if world:
            sc = max(self.scale, 1e-3)
            off = np.array([self.offset.x(), self.offset.y()])
            self._mhist[self._mhead] = (self._mouse_smooth - off) / sc
            self._vhist[self._mhead] = self._mvel / sc
        else:
            self._mhist[self._mhead] = self._mouse_smooth
            self._vhist[self._mhead] = self._mvel
        self._mactive[self._mhead] = 1.0 if self._mouse_in else 0.0

    def _ensure_clump_layout(self):
        ls = max(1, self.text_scale // 3)
        size = (self.width(), self.height(), ls)
        if self._clump_size == size:
            return
        self._clump_size = size
        lw = [8 * ls * len(n) for n in self.folder_names]
        self.clumps.layout(self.width(), self.height(), (self.text_img.width(), self.text_img.height()), 8 * ls + 6, lw)
        u = self.clumps.u
        sq = max(1.0, float(u - 1))
        self._local_rects = [
            [QRectF((n % c) * u, (n // c) * u, sq, sq) for n in range(len(self.groups[i].photos))] if c else []
            for i, c in enumerate(self.clumps._cols)
        ]
        self._clump_label_pos.clear()

    def _clump_step(self):
        self._ensure_clump_layout()
        cl = self.clumps
        n, nf, u, t = len(self.groups), len(cl.members), cl.u, self.t
        if n == 0 or nf == 0:
            self.clump_rects = []
            return
        self._record_mouse(world=False)
        amp_c = min(9.5, max(4.5, u * 1.15))
        ph = cl.clump_phase
        off_clump = np.stack([
            np.sin(t * 0.54 + ph) * 0.72 + np.sin(t * 1.11 + ph * 2.1) * 0.28,
            np.cos(t * 0.42 + ph * 1.3) * 0.72 + np.cos(t * 0.89 + ph * 0.7) * 0.28,
        ], 1) * amp_c

        # 1. Cohesive ball-level mouse tracking + 4-tap reverb delay (keeps the ball's round organic shape intact!)
        if getattr(self, "_away", None) is None:
            f_cl = np.zeros((nf, 2), float)
            cl_pos = cl.centre + off_clump + self._cl_disp
            sigma_cl = max(130.0, min(self.width(), self.height()) * 0.22)
            inv_2scl = 1.0 / (2.0 * sigma_cl * sigma_cl)
            cl_tap = np.arange(nf) % 10
            for lag, gain in REVERB_TAPS:
                ih = (self._mhead - cl_tap - lag) % REVERB_HIST
                act = self._mactive[ih]
                if not act.any():
                    continue
                mp, mv = self._mhist[ih], self._vhist[ih]
                delta = mp - cl_pos  # gentle magnetic pull toward cursor wake + velocity drag
                d2 = (delta * delta).sum(1)
                w = np.exp(-d2 * inv_2scl) * act
                f_cl += (delta * 0.012 + mv * 0.14) * (w * gain)[:, None]
            self._cl_vel = self._cl_vel * 0.935 - self._cl_disp * 0.055 + np.tanh(f_cl / 2.2) * 2.2
            self._cl_disp = np.clip(self._cl_disp + self._cl_vel, -14.0, 14.0)
        else:
            d = cl.centre - np.array([self.width() / 2.0, self.height() / 2.0])
            off_clump = off_clump + d * (1.35 ** self._away - 1.0)

        off_clump = off_clump + self._cl_disp

        # 2. Gentle internal breathing & radial ripple (bounded to <= 0.42*u so blocks never tear apart!)
        amp_r = min(1.8, max(0.7, u * 0.24))
        amp_t = min(1.3, max(0.5, u * 0.18))
        wave_r = np.sin(t * 1.30 - cl.radial * 0.38 + cl.clump_phase[cl.group_clump])
        wave_t = np.cos(t * 0.86 - cl.radial * 0.38 + cl.group_phase)
        off_inner = cl.radial_dir * (wave_r * amp_r)[:, None] + cl.radial_perp * (wave_t * amp_t)[:, None]

        cur_pos = cl.group_positions(u, off_clump) + off_inner + self._c_disp + cl.block_cr * (0.5 * u)
        f_in = np.zeros((n, 2), float)
        glow_in = np.zeros(n, float)
        if getattr(self, "_away", None) is None:
            tap_i = np.clip((cl.radial * 1.8).astype(int), 0, 12)
            sigma = max(85.0, min(self.width(), self.height()) * 0.14)
            inv_2s2 = 1.0 / (2.0 * sigma * sigma)
            for lag, gain in REVERB_TAPS:
                ih = (self._mhead - tap_i - lag) % REVERB_HIST
                act = self._mactive[ih]
                if not act.any():
                    continue
                mp, mv = self._mhist[ih], self._vhist[ih]
                delta = cur_pos - mp
                d2 = (delta * delta).sum(1)
                w = np.exp(-d2 * inv_2s2) * act
                spd = np.hypot(mv[:, 0], mv[:, 1])
                f_tap = mv * (0.045 * w[:, None]) + delta * ((0.0005 * spd + 0.0010) * w)[:, None]
                f_in += f_tap * gain
                glow_in += w * (0.035 + 0.015 * spd) * gain

        if 0 <= self.hover < n and self.alive[self.hover]:
            f_in[self.hover] *= 0.25
        max_inner = max(1.5, u * 0.42)
        self._c_vel = self._c_vel * 0.92 - self._c_disp * 0.075 + np.tanh(f_in / 0.8) * 0.8
        self._c_disp = np.clip(self._c_disp + self._c_vel, -max_inner, max_inner)
        vel_mag = np.hypot(self._c_vel[:, 0], self._c_vel[:, 1]) + np.hypot(
            self._cl_vel[cl.group_clump, 0], self._cl_vel[cl.group_clump, 1]
        ) * 0.4
        self._c_glow = np.clip(self._c_glow * 0.945 + glow_in + vel_mag * 0.025, 0.0, 1.0)
        if 0 <= self.hover < n and self.alive[self.hover]:
            self._c_glow[self.hover] = 1.0

        if 0 <= self.hover < n and self.alive[self.hover] and getattr(self, "_away", None) is None:
            if self.hover != self._chot_idx:
                self._chot_idx = self.hover
                self._chot_val = 0.28
                self._chot_vel = 0.0
            self._chot_vel = self._chot_vel * 0.64 + (1.0 - self._chot_val) * 0.12
            self._chot_val += self._chot_vel
        else:
            self._chot_idx = -1
            self._chot_val = 0.0
            self._chot_vel = 0.0

        self.clump_rects = cl.group_rects(u, off_clump, off_inner + self._c_disp)

    def _reverb_step(self):
        n = len(self.groups)
        if n == 0:
            return
        self._record_mouse(world=True)
        cur_pos = self.pos + self._disp
        tap_i = np.clip((np.linalg.norm(self.pos, axis=1) / max(self.hole, 60.0) * 4.0 + self.phase / math.tau * 6.0).astype(int), 0, 12)
        sig = np.maximum(self.r * 2.4, 140.0 / max(self.scale, 0.2))
        inv_2s2 = 1.0 / (2.0 * sig * sig)

        f_in = np.zeros((n, 2), float)
        th_in = np.zeros(n, float)
        for lag, gain in REVERB_TAPS:
            ih = (self._mhead - tap_i - lag) % REVERB_HIST
            act = self._mactive[ih]
            if not act.any():
                continue
            mp, mv = self._mhist[ih], self._vhist[ih]
            delta = cur_pos - mp
            d2 = (delta * delta).sum(1)
            w = np.exp(-d2 * inv_2s2) * act
            spd = np.hypot(mv[:, 0], mv[:, 1])
            f_tap = mv * (0.14 * w[:, None]) + delta * ((0.0011 * spd + 0.0020) * w)[:, None]
            f_tap += np.stack([-delta[:, 1], delta[:, 0]], 1) * (0.0007 * spd * w)[:, None]
            f_in += f_tap * gain
            th_in += w * (0.024 + 0.014 * spd * max(self.scale, 1e-3)) * gain

        if 0 <= self.hover < n and self.alive[self.hover]:
            f_in[self.hover] *= 0.20

        f_coup = np.zeros((n, 2), float)
        th_coup = np.zeros(n, float)
        if len(self.edges):
            live_e = self.edges[self.alive[self.edges[:, 0]] & self.alive[self.edges[:, 1]]]
            if len(live_e):
                a, b = live_e[:, 0], live_e[:, 1]
                fe = (self._disp[b] - self._disp[a]) * 0.032
                np.add.at(f_coup, a, fe)
                np.add.at(f_coup, b, -fe)
                dt = (self._thrill[b] - self._thrill[a]) * 0.045
                np.add.at(th_coup, a, dt)
                np.add.at(th_coup, b, -dt)

        max_f = np.maximum(self.r * 0.12, 8.0)[:, None]
        f_sat = max_f * np.tanh(f_in / max_f)
        self._dvel = self._dvel * 0.955 - self._disp * 0.020 + f_coup + f_sat
        self._disp += self._dvel
        vel_norm = np.hypot(self._dvel[:, 0], self._dvel[:, 1]) / np.maximum(self.r * 0.08, 4.0)
        self._thrill = np.clip(self._thrill * 0.945 + th_coup + th_in + vel_norm * 0.030, 0.0, 1.0)

        target_hot = np.zeros(n, float)
        if 0 <= self.hover < n and self.alive[self.hover]:
            target_hot[self.hover] = 0.60
            self._thrill[self.hover] = max(self._thrill[self.hover], 0.85)
        self._hot_vel = self._hot_vel * 0.64 + (target_hot - self._hot) * 0.12
        self._hot += self._hot_vel

    def _tick(self):
        self.t += 1 / FPS
        self._update_text()
        if getattr(self, "_away", None) is not None and self.abstract:
            self._away += 1
            self._clump_step()
            self.update()
            if self._away == self._away_frames:
                self.timer.stop()
                self._away_done()
            return
        if getattr(self, "_away", None) is not None:
            self._away += 1
            self.pos = self.pos * 1.32 + np.sign(self.pos) * 8      # accelerate away from the middle
            self._c = self._wobble()
            self.update()
            if self._away == self._away_frames:
                self.timer.stop()
                self._away_done()
            return
        if self.abstract:
            self._clump_step()
        else:
            self._step()
            if self.auto_fit:
                self._fit()
            self._reverb_step()
            self._c = self._wobble()
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
        self._bg_pm = None
        self._net_label_pos.clear()
        if self.abstract:
            self._clump_size = None
            self._clump_label_pos.clear()
        super().resizeEvent(e)

    # ------------------------------------------------------------ paint
    def paintEvent(self, e):
        if self.text_img is None:
            self._update_text()
        if self._bg_pm is None or self._bg_pm.size() != self.size():
            self._bg_pm = QPixmap(self.size())
            bgp = QPainter(self._bg_pm)
            bg = QLinearGradient(0, 0, 0, self.height())
            bg.setColorAt(0, QColor(20, 20, 20))
            bg.setColorAt(1, QColor(0, 0, 0))
            bgp.fillRect(self.rect(), bg)
            bgp.end()
        qp = QPainter(self)
        qp.drawPixmap(0, 0, self._bg_pm)
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
        c = self._c if len(self._c) == len(self.pos) else self._wobble()
        self._draw_edges(qp, c)
        order = sorted(np.flatnonzero(self.alive).tolist(), key=lambda i: float(self._hot[i]))
        for i in order:
            self._draw_group(qp, i, c[i], i == self.hover)
        self._draw_labels(qp, c)
        qp.restore()
        self._draw_text(qp)
        if self.hover >= 0:
            self._draw_tip(qp, self.hover)

    def _wobble(self) -> np.ndarray:
        if len(self.pos) == 0:
            return self.pos
        t, ph = self.t, self.phase
        amp = self.r * 0.13 + 2.5
        sway = np.stack([
            np.sin(t * 0.58 + ph) * 0.72 + np.sin(t * 1.19 + ph * 2.1) * 0.28,
            np.cos(t * 0.44 + ph * 1.7) * 0.72 + np.cos(t * 0.93 + ph * 0.8) * 0.28,
        ], 1) * amp[:, None]
        return self.pos + sway + self._disp

    def _draw_edges(self, qp: QPainter, c: np.ndarray):
        if not len(self.edges):
            return
        n_pens = len(_EDGE_PENS)
        for a, b in self.edges:
            if not (self.alive[a] and self.alive[b]):
                continue
            if self.hover in (a, b):
                qp.setPen(_EDGE_PENS[-1])
            else:
                e = max(float(self._thrill[a]), float(self._thrill[b]))
                qp.setPen(_EDGE_PENS[min(n_pens - 1, int(e * (n_pens - 1)))])
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
            target = np.array([float(c[members, 0].mean()), float((c[members, 1] - self.r[members]).min())])
            cur = self._net_label_pos.get(int(f))
            cur = target if cur is None else cur + (target - cur) * 0.16
            self._net_label_pos[int(f)] = cur
            key = (f, scale)
            img = self.folder_img.get(key)
            if img is None:
                img = self.folder_img[key] = pixelfont.render([self.folder_names[f].upper()], scale,
                                                              QColor(255, 255, 255), QColor(0, 0, 0, 0))
            x = int(round(self.offset.x() + cur[0] * self.scale - img.width() / 2))
            y = int(round(self.offset.y() + cur[1] * self.scale)) - img.height() - 4
            box = QRectF(x - 4, y - 2, img.width() + 8, img.height() + 4)
            if any(box.intersects(t) for t in taken):
                continue
            taken.append(box)
            qp.drawImage(QPoint(x, y), img)
        qp.restore()

    def _draw_thumbs_block(self, qp: QPainter, i: int, center: tuple[float, float], s: float, tilt: float,
                           clamp_view: bool = False, pad_bg: bool = False):
        g = self.groups[i]
        cols, _ = _grid(len(g.photos))
        gap = s * 0.04
        hh = s * 0.66
        items = []
        for p in g.photos:
            pm = self._pixmap(p.path)
            ar = pm.width() / max(1, pm.height()) if pm and not pm.isNull() else 1.5
            items.append((pm, hh * ar))
        rows_items = [items[r * cols:(r + 1) * cols] for r in range(math.ceil(len(items) / cols))]
        row_ws = [sum(w for _, w in row) + gap * max(0, len(row) - 1) for row in rows_items]
        W = max(row_ws, default=0.0)
        H = len(rows_items) * hh + max(0, len(rows_items) - 1) * gap
        cx, cy = center
        if clamp_view:
            cx = min(max(W / 2 + 12.0, cx), max(W / 2 + 12.0, self.width() - W / 2 - 12.0))
            cy = min(max(H / 2 + 12.0, cy), max(H / 2 + 12.0, self.height() - H / 2 - 12.0))
        qp.save()
        qp.translate(QPointF(cx, cy))
        qp.rotate(tilt)
        if pad_bg and W > 0 and H > 0:
            qp.fillRect(QRectF(-W / 2 - 6, -H / 2 - 6, W + 12, H + 12), QColor(8, 8, 8, 215))
        y = -H / 2
        for row, rw in zip(rows_items, row_ws):
            x = -rw / 2
            for pm, w in row:
                r = QRectF(x, y, w, hh)
                if pm and not pm.isNull():
                    qp.drawPixmap(r, pm, QRectF(pm.rect()))
                else:
                    qp.fillRect(r, QColor(40, 40, 40))
                x += w + gap
            y += hh + gap
        qp.restore()

    def _draw_group(self, qp: QPainter, i: int, c: np.ndarray, hot: bool):
        """One group = one tight block of images, floating as a unit with spring-reverb tilt."""
        if self.abstract and not hot:
            self._draw_block(qp, i, c)
            return
        hot_amt = max(float(self._hot[i]), 0.60 if hot else 0.0) if not self.abstract else (0.60 if hot else 0.0)
        thrill = float(self._thrill[i]) if not self.abstract else 0.0
        vx = float(self._dvel[i, 0]) if not self.abstract else 0.0
        s = self.s * (1.0 + hot_amt + thrill * 0.04)
        tilt = 3.2 * math.sin(self.t * 0.52 + self.phase[i]) + 1.4 * math.sin(self.t * 1.07 + self.phase[i] * 1.9)
        tilt += float(np.clip(vx * 1.4, -9.0, 9.0))
        self._draw_thumbs_block(qp, i, (float(c[0]), float(c[1])), s, tilt)

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
        ls = max(1, self.text_scale // 3)
        if not self.clump_rects or self._clump_size != (self.width(), self.height(), ls):
            self._clump_step()
        cl = self.clumps
        u = cl.u
        self.offset = QPointF(self.width() / 2, self.height() / 2)          # numbers in the middle

        qp.save()
        qp.setPen(Qt.NoPen)
        px = py = 0.0
        last_s = -1
        shades = (self._c_glow * 115.0).astype(int)
        for i, (x, y, c, r) in enumerate(self.clump_rects):
            if not self.alive[i] or c == 0:
                continue
            rects = self._local_rects[i]
            if not rects:
                continue
            s_idx = int(shades[i])
            if s_idx > 115:
                s_idx = 115
            if s_idx != last_s:
                qp.setBrush(_SHADES[s_idx])
                last_s = s_idx
            qp.translate(x - px, y - py)
            px, py = x, y
            qp.drawRects(rects)
        qp.restore()

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
            target = np.array([(left + right) / 2.0, top])
            cur = self._clump_label_pos.get(k)
            cur = target if cur is None else cur + (target - cur) * 0.14
            self._clump_label_pos[k] = cur
            qp.drawImage(QPoint(int(round(cur[0] - img.width() / 2.0)), int(round(cur[1] - img.height() - 4))), img)

        hi = self.hover
        if 0 <= hi < len(self.groups) and self.alive[hi] and getattr(self, "_away", None) is None and self._chot_val > 0.05:
            x, y, c, r = self.clump_rects[hi]
            cx = x + c * u / 2.0 + float(self._c_disp[hi, 0]) * 0.25
            cy = y + r * u / 2.0 + float(self._c_disp[hi, 1]) * 0.25
            base_s = float(np.clip(min(self.width(), self.height()) * 0.14 / max(1.0, math.sqrt(len(self.groups[hi].photos) / 2.2)), 52.0, 118.0))
            tilt = 2.6 * math.sin(self.t * 0.58 + self.phase[hi]) + float(np.clip(self._c_vel[hi, 0] * 1.1, -8.0, 8.0))
            qp.save()
            qp.setRenderHint(QPainter.Antialiasing, True)
            qp.setRenderHint(QPainter.SmoothPixmapTransform, True)
            self._draw_thumbs_block(qp, hi, (cx, cy), base_s * max(0.2, self._chot_val), tilt, clamp_view=True, pad_bg=True)
            qp.restore()

    def _draw_tip(self, qp: QPainter, i: int):
        ts = max(1, self.text_scale // 2)
        key = (i, ts, len(self.groups[i].photos))
        if self._tip_key != key or self._tip_img is None:
            g = self.groups[i]
            folders = sorted({os.path.basename(os.path.dirname(p.path)) for p in g.photos})
            lines = [f"{len(g.photos)} IMAGES  {human(g.bytes)}"] + folders[:4]
            self._tip_img = pixelfont.render(lines, ts, WHITE, QColor(0, 0, 0, 255))
            self._tip_key = key
        img = self._tip_img
        pt = QPoint(int(self._mouse_raw[0]), int(self._mouse_raw[1])) if self._mouse_in else self.mapFromGlobal(self.cursor().pos())
        x = min(max(0, pt.x() + 20), self.width() - img.width())
        y = min(max(0, pt.y() + 20), self.height() - img.height())
        qp.drawImage(QPoint(x, y), img)

    # ------------------------------------------------------------ input
    def mouseMoveEvent(self, e):
        pt = e.position()
        if self._drag is not None:
            self.offset += pt - self._drag
            self._drag = pt
            self.auto_fit = False
            self._mouse_in = False
            return
        raw = np.array([pt.x(), pt.y()], float)
        if not self._mouse_in:
            self._mouse_smooth = raw.copy()
            self._mvel[:] = 0.0
        self._mouse_raw = raw
        self._mouse_in = True
        h = self._hit(pt)
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
                self._mouse_in = False

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            pt = e.position()
            self._mouse_raw = np.array([pt.x(), pt.y()], float)
            self._mouse_smooth = self._mouse_raw.copy()
            self._mvel[:] = 0.0
            self._mouse_in = True
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
        self._mouse_in = False
        self.hover = -1
