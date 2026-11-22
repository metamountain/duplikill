"""Finding groups: exact duplicates, similar images and bursts."""
from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from .index import Cache, Photo, Progress

MODES = {"exact": "Exact duplicates", "similar": "Similar images", "burst": "Bursts"}
# similar: max differing bits of 64; burst: max seconds between two shots
STRICTNESS = {
    "similar": {"Very strict": 3, "Strict": 6, "Normal": 10, "Loose": 14},
    "burst": {"1 s": 1, "2 s": 2, "3 s": 3, "5 s": 5, "10 s": 10},
}
MAX_GROUP = 60  # larger similar groups are split again with a stricter threshold


@dataclass
class Group:
    kind: str
    photos: list[Photo]
    best: int = 0
    key: str = field(default="")

    def __post_init__(self):
        self.photos.sort(key=lambda p: (p.taken if p.taken is not None else p.mtime, p.path))
        self.key = self.kind + ":" + hashlib.blake2b("\n".join(sorted(p.path for p in self.photos)).encode(),
                                                       digest_size=12).hexdigest()
        self.best = pick_best(self)

    @property
    def bytes(self) -> int:
        return sum(p.size for p in self.photos)


COPY_HINT = re.compile(r"(\(\d+\)|kopie|copy|_v\d+|whatsapp|-wa\d+|bearbeitet|edited)", re.I)


def pick_best(g: Group) -> int:
    def score(p: Photo):
        if g.kind == "exact":
            name = os.path.basename(p.path)
            return (-bool(COPY_HINT.search(name)), -len(p.path), -p.mtime)
        if g.kind == "burst":
            return (p.sharp, p.pixels)
        return (np.log1p(p.pixels) + 1.5 * np.log1p(p.sharp), p.size)
    return max(range(len(g.photos)), key=lambda i: score(g.photos[i]))


# ---------------------------------------------------------------- exact

def _blake(path: str) -> str | None:
    h = hashlib.blake2b(digest_size=20)
    try:
        with open(path, "rb") as fh:
            while b := fh.read(1 << 20):
                h.update(b)
    except OSError:
        return None
    return h.hexdigest()


def exact(photos: list[Photo], cache: Cache, progress: Progress) -> list[Group]:
    by_size = defaultdict(list)
    for p in photos:
        by_size[p.size].append(p)
    cand = [p for ps in by_size.values() if len(ps) > 1 for p in ps]
    todo = [p for p in cand if not p.blake]
    with ThreadPoolExecutor(8) as ex:
        for i, (p, b) in enumerate(zip(todo, ex.map(lambda q: _blake(q.path), todo))):
            p.blake = b
            if b:
                cache.set_blake(p.path, b)
            if i % 100 == 0:
                progress(i, len(todo), "comparing file contents")
                cache.commit()
    cache.commit()
    by_hash = defaultdict(list)
    for p in cand:
        if p.blake:
            by_hash[(p.size, p.blake)].append(p)
    return [Group("exact", ps) for ps in by_hash.values() if len(ps) > 1]


# ---------------------------------------------------------------- similar

class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, a):
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.p[b] = a


def _pairs(hashes: np.ndarray, t: int, progress: Progress, use_cuda: bool):
    """All index pairs (i<j) whose hashes differ in at most t bits, in blocks."""
    n = len(hashes)
    if use_cuda:
        try:
            import torch
            if torch.cuda.is_available():
                return list(_pairs_cuda(hashes, t, progress, torch))
        except Exception:
            pass
    out = []
    block = 256
    for s in range(0, n, block):
        a = hashes[s:s + block, None]
        d = np.bitwise_count(a ^ hashes[None, :])
        ii, jj = np.nonzero(d <= t)
        ii = ii + s
        m = jj > ii
        out += list(zip(ii[m].tolist(), jj[m].tolist()))
        if (s // block) % 20 == 0:
            progress(s, n, "comparing (CPU)")
    return out


def _pairs_cuda(hashes, t, progress, torch):
    dev = torch.device("cuda")
    h = torch.tensor(hashes.view(np.int64), device=dev)
    n, block = len(hashes), 512
    # popcount on the GPU via byte lookup table
    lut = torch.tensor([bin(i).count("1") for i in range(256)], dtype=torch.uint8, device=dev)
    for s in range(0, n, block):
        x = h[s:s + block, None] ^ h[None, :]
        cnt = torch.zeros(x.shape, dtype=torch.uint8, device=dev)
        for k in range(8):
            cnt += lut[((x >> (8 * k)) & 0xFF).long()]
        ii, jj = torch.nonzero(cnt <= t, as_tuple=True)
        ii = ii + s
        m = jj > ii
        for a, b in zip(ii[m].tolist(), jj[m].tolist()):
            yield a, b
        progress(s, n, "comparing (CUDA)")


def similar(photos: list[Photo], t: int, progress: Progress, use_cuda: bool = True) -> list[Group]:
    photos = [p for p in photos if p.phash]
    hashes = np.array([p.phash for p in photos], dtype=np.uint64)
    comps = _cluster(list(range(len(photos))), hashes, t, progress, use_cuda)
    return [Group("similar", [photos[i] for i in c]) for c in comps]


def _cluster(idx, hashes, t, progress, use_cuda):
    sub = hashes[idx]
    uf = _UF(len(idx))
    for a, b in _pairs(sub, t, progress, use_cuda):
        uf.union(a, b)
    comps = defaultdict(list)
    for i in range(len(idx)):
        comps[uf.find(i)].append(idx[i])
    out = []
    for c in comps.values():
        if len(c) < 2:
            continue
        if len(c) > MAX_GROUP and t > 1:
            out += _cluster(c, hashes, t - 2, progress, use_cuda)
        else:
            out.append(c)
    return out


# ---------------------------------------------------------------- bursts

def bursts(photos: list[Photo], gap: float, progress: Progress) -> list[Group]:
    by_folder = defaultdict(list)
    for p in photos:
        if p.taken:
            by_folder[p.folder].append(p)
    out = []
    for ps in by_folder.values():
        ps.sort(key=lambda p: p.taken)
        run = [ps[0]]
        for p in ps[1:]:
            if p.taken - run[-1].taken <= gap:
                run.append(p)
            else:
                if len(run) > 1:
                    out.append(Group("burst", run))
                run = [p]
        if len(run) > 1:
            out.append(Group("burst", run))
    return out


def find(kind: str, photos: list[Photo], level, cache: Cache, progress: Progress, use_cuda=True) -> list[Group]:
    if kind == "exact":
        groups = exact(photos, cache, progress)
    elif kind == "similar":
        groups = similar(photos, STRICTNESS["similar"][level], progress, use_cuda)
    else:
        groups = bursts(photos, STRICTNESS["burst"][level], progress)
    done = cache.done_keys()
    groups = [g for g in groups if g.key not in done]
    groups.sort(key=lambda g: (g.photos[0].folder, g.photos[0].taken or g.photos[0].mtime))
    return groups
