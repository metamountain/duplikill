"""Overview for many groups: one clump per folder, its size = its number of images.

Inside a clump every image is one pixel square; the squares of a group form a small block, and the blocks are
packed from the middle outwards, so a clump looks organic. The clumps are placed around the numbers in the middle.
"""
from __future__ import annotations

import math

import numpy as np


def _grid(k: int) -> tuple[int, int]:
    cols = math.ceil(math.sqrt(k))
    return cols, math.ceil(k / cols)


def _spiral(n: int, seed: int) -> np.ndarray:
    """Grid cells sorted by distance from the middle, with a little noise for an organic outline."""
    rad = int(math.sqrt(n) * 1.2) + 4
    ys, xs = np.mgrid[-rad:rad + 1, -rad:rad + 1]
    rng = np.random.default_rng(seed)
    d = np.hypot(xs * 1.0, ys * 1.15) + rng.random(xs.shape) * 1.6
    order = np.argsort(d, axis=None)
    return np.stack([xs.ravel()[order], ys.ravel()[order]], 1)


def pack_folder(sizes: list[int], seed: int = 0) -> tuple[list[tuple[int, int, int, int]], tuple[int, int, int, int]]:
    """Places group blocks (cols x rows cells, one free cell between groups) as close to the middle as possible.
    Returns per group (x, y, cols, rows) in cells, relative to the clump, and the clump's bounding box."""
    total = sum((_grid(k)[0] + 1) * (_grid(k)[1] + 1) for k in sizes)
    cand = _spiral(total * 3, seed)
    rad = int(np.abs(cand).max()) + 12
    cand_pts = cand.tolist()
    occ = np.zeros((2 * rad + 1, 2 * rad + 1), bool)
    out = [None] * len(sizes)
    for gi in sorted(range(len(sizes)), key=lambda i: -sizes[i]):
        c, r = _grid(sizes[gi])
        for x, y in cand_pts:
            x0, y0 = x - c // 2 + rad, y - r // 2 + rad
            if x0 < 1 or y0 < 1 or x0 + c + 1 >= occ.shape[1] or y0 + r + 1 >= occ.shape[0]:
                continue
            if not occ[y0 - 1:y0 + r + 1, x0 - 1:x0 + c + 1].any():      # one cell of air around
                occ[y0:y0 + r, x0:x0 + c] = True
                out[gi] = (x0 - rad, y0 - rad, c, r)
                break
    xs = [b[0] for b in out] + [b[0] + b[2] for b in out]
    ys = [b[1] for b in out] + [b[1] + b[3] for b in out]
    return out, (min(xs), min(ys), max(xs), max(ys))


class Clumps:
    """Layout in screen pixels for a widget of size w x h with a free middle of size hole (w, h)."""

    def __init__(self, groups, folder_of: list[int], n_folders: int):
        self.groups = groups
        self.folder_of = folder_of
        self.members = [[i for i, f in enumerate(folder_of) if f == k] for k in range(n_folders)]
        self.packs = [pack_folder([len(groups[i].photos) for i in m], seed=k) if m else ([], (0, 0, 0, 0))
                      for k, m in enumerate(self.members)]
        self.u = 6
        self.label_h = 0
        self.label_w = [0] * n_folders
        self.centre = np.zeros((n_folders, 2), float)

        n_g = len(groups)
        self.group_clump = np.array(folder_of, int) if n_g else np.zeros(0, int)
        self.block_xy = np.zeros((n_g, 2), float)
        self.block_cr = np.zeros((n_g, 2), int)
        self.clump_mid = np.zeros((n_folders, 2), float)
        self.clump_counts = np.ones(n_folders, float)
        for k, m in enumerate(self.members):
            if not m:
                continue
            blocks, (x0, y0, x1, y1) = self.packs[k]
            self.clump_mid[k] = ((x1 + x0) / 2.0, (y1 + y0) / 2.0)
            self.clump_counts[k] = float(len(m))
            for gi, (bx, by, c, r) in zip(m, blocks):
                self.block_xy[gi] = (bx, by)
                self.block_cr[gi] = (c, r)
        self._cols = self.block_cr[:, 0].tolist()
        self._rows = self.block_cr[:, 1].tolist()
        self.clump_phase = np.arange(n_folders, dtype=float) * 1.7
        if n_g:
            local = self.block_xy + self.block_cr * 0.5 - self.clump_mid[self.group_clump]
            self.radial = np.hypot(local[:, 0], local[:, 1])
            self.radial_dir = local / np.maximum(self.radial[:, None], 1e-6)
            self.radial_dir[self.radial < 1e-6] = 0.0
            self.radial_perp = np.stack([-self.radial_dir[:, 1], self.radial_dir[:, 0]], 1)
            self.group_phase = (
                self.clump_phase[self.group_clump]
                + self.radial * 0.35
                + np.arctan2(local[:, 1], local[:, 0]) * 0.35
            )
        else:
            self.radial = np.zeros(0, float)
            self.radial_dir = np.zeros((0, 2), float)
            self.radial_perp = np.zeros((0, 2), float)
            self.group_phase = np.zeros(0, float)
        self.base_xy = np.zeros((n_g, 2), float)

    def layout(self, w: int, h: int, hole: tuple[int, int], label_h: int, label_w: list[int] | None = None):
        """Largest square pitch for which all clumps fit around the middle."""
        self.label_w = label_w or [0] * len(self.members)
        spans = [(k, self.packs[k][1][2] - self.packs[k][1][0], self.packs[k][1][3] - self.packs[k][1][1])
                 for k, m in enumerate(self.members) if m]
        for u in range(24, 2, -1):
            needed = sum(max(sw * u, self.label_w[k]) * (sh * u + label_h) for k, sw, sh in spans)
            avail = max(0, (w - 4 * u) * (h - 4 * u - 32) - (hole[0] + 4 * u) * (hole[1] + 4 * u))
            if needed <= avail and self._place(w, h, hole, label_h, u):
                self.u = u
                self._update_base()
                return
        self.u = 3
        self._place(w, h, hole, label_h, 3, force=True)
        self._update_base()

    def _update_base(self):
        if len(self.groups):
            self.base_xy = self.centre[self.group_clump] + (self.block_xy - self.clump_mid[self.group_clump]) * self.u
            self.base_xy[:, 1] += self.label_h / 2.0
        else:
            self.base_xy = np.zeros((0, 2), float)

    def _place(self, w, h, hole, label_h, u, force=False) -> bool:
        cx, cy = w / 2, h / 2
        boxes = [(cx - hole[0] / 2 - u * 2, cy - hole[1] / 2 - u * 2, cx + hole[0] / 2 + u * 2, cy + hole[1] / 2 + u * 2)]
        order = sorted(range(len(self.members)), key=lambda k: -sum(len(self.groups[i].photos) for i in self.members[k]))
        centre = np.zeros((len(self.members), 2))
        margin = u * 2
        for n, k in enumerate(order):
            if not self.members[k]:
                continue
            x0, y0, x1, y1 = self.packs[k][1]
            bw, bh = max((x1 - x0) * u, self.label_w[k]), (y1 - y0) * u + label_h
            placed = False
            # candidate points on a spiral around the middle, starting at a different angle per clump
            for step in range(0, 4000):
                rr = 6 * math.sqrt(step) * u ** 0.5 * 4
                a = step * 2.39996 + n
                px, py = cx + math.cos(a) * rr * (w / h) ** 0.5, cy + math.sin(a) * rr / (w / h) ** 0.5
                bx0, by0 = px - bw / 2, py - bh / 2
                bx1, by1 = bx0 + bw, by0 + bh
                if bx0 < margin or by0 < margin or bx1 > w - margin or by1 > h - margin - 32:
                    if rr > max(w, h):
                        break
                    continue
                if any(bx0 < b[2] + margin and bx1 > b[0] - margin and by0 < b[3] + margin and by1 > b[1] - margin
                       for b in boxes):
                    continue
                boxes.append((bx0, by0, bx1, by1))
                centre[k] = (px, py)
                placed = True
                break
            if not placed and not force:
                return False
        self.centre = centre
        self.label_h = label_h
        return True

    def group_positions(self, u: int, offsets: np.ndarray, group_offsets: np.ndarray | None = None) -> np.ndarray:
        """Vectorized sub-pixel top-left position (x, y) per group for clump and individual offsets."""
        if not len(self.groups):
            return np.zeros((0, 2), float)
        if u == self.u:
            pos = self.base_xy + offsets[self.group_clump]
        else:
            pos = self.centre[self.group_clump] + (self.block_xy - self.clump_mid[self.group_clump]) * u
            pos[:, 1] += self.label_h / 2.0
            pos += offsets[self.group_clump]
        if group_offsets is not None:
            pos = pos + group_offsets
        return pos

    def group_rects(self, u: int, offsets: np.ndarray, group_offsets: np.ndarray | None = None) -> list[tuple[float, float, int, int]]:
        """Screen rectangle (x, y, cols, rows) of each group block for current clump and group offsets."""
        if not len(self.groups):
            return []
        pos = self.group_positions(u, offsets, group_offsets)
        return list(zip(pos[:, 0].tolist(), pos[:, 1].tolist(), self._cols, self._rows))
