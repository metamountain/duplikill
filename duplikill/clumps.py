"""Overview for many groups: one clump per folder, its size = its number of images.

Inside a clump every image is one pixel square; the squares of a group form a small block, and the blocks are
packed from the middle outwards, so a clump looks organic. All in whole screen pixels - crisp, no scaling.
The clumps are placed around the numbers in the middle.
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
    occ = np.zeros((2 * rad + 1, 2 * rad + 1), bool)
    out = [None] * len(sizes)
    for gi in sorted(range(len(sizes)), key=lambda i: -sizes[i]):
        c, r = _grid(sizes[gi])
        for x, y in cand:
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
        self.centre = np.zeros((n_folders, 2))
        self.rects: list[tuple[int, int, int, int]] = [(0, 0, 0, 0)] * len(groups)   # per group, screen px

    def layout(self, w: int, h: int, hole: tuple[int, int], label_h: int, label_w: list[int] | None = None):
        """Largest square pitch for which all clumps fit around the middle."""
        self.label_w = label_w or [0] * len(self.members)
        for u in range(24, 2, -1):
            if self._place(w, h, hole, label_h, u):
                self.u = u
                return
        self.u = 3
        self._place(w, h, hole, label_h, 3, force=True)

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
                if bx0 < margin or by0 < margin or bx1 > w - margin or by1 > h - margin:
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

    def group_rects(self, u: int, offsets: np.ndarray) -> list[tuple[int, int, int, int]]:
        """Screen rectangle (x, y, cols, rows) of each group block for the current clump offsets."""
        out = [(0, 0, 0, 0)] * len(self.groups)
        for k, m in enumerate(self.members):
            if not m:
                continue
            blocks, (x0, y0, x1, y1) = self.packs[k]
            ox = int(round(self.centre[k][0] + offsets[k][0] - (x1 + x0) / 2 * u))
            oy = int(round(self.centre[k][1] + offsets[k][1] - (y1 + y0) / 2 * u + self.label_h / 2))
            for gi, (bx, by, c, r) in zip(m, blocks):
                out[gi] = (ox + bx * u, oy + by * u, c, r)
        return out
