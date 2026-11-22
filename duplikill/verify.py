"""Second check after Czkawka: its 8x8 gradient hash also groups unrelated pictures with a similar
light/dark layout (cat next to a skate park). Every pair inside a Czkawka group is checked again:

- structure: correlation of 24x24 greyscale images            >= MIN_CORR
- colour:    mean distance of a 4x4 colour layout              <= MAX_CDIST
- time:      taken on the same day (EXIF), unless it is practically a copy (corr >= COPY_CORR)

Groups are rebuilt from the pairs that pass (union-find); single images drop out.
Calibrated on a real family archive (one year, 2026-11-22): true series corr 0.67-0.9 / cdist <= 0.11,
false groups cdist 0.14-0.36 or months apart.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np

from .groups import Group

MIN_CORR = 0.6
MAX_CDIST = 0.12
COPY_CORR = 0.9
SAME_DAY = 24 * 3600


class Sig:
    __slots__ = ("grey", "colour", "taken")

    def __init__(self, grey, colour, taken):
        self.grey, self.colour, self.taken = grey, colour, taken


def _taken(img) -> float | None:
    try:
        ex = img.getexif()
        raw = ex.get_ifd(0x8769).get(36867) or ex.get(306)          # DateTimeOriginal / DateTime
        if raw:
            return datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:
        pass
    return None


def signature(path: str) -> Sig | None:
    from PIL import Image, ImageOps
    try:
        with Image.open(path) as im:
            taken = _taken(im)
            im.draft("RGB", (256, 256))
            im = ImageOps.exif_transpose(im.convert("RGB"))
            small = np.asarray(im.resize((24, 24), Image.BILINEAR), np.float32) / 255
    except Exception:
        return None
    g = small.mean(2).ravel()
    g = (g - g.mean()) / (g.std() + 1e-6)
    colour = small.reshape(4, 6, 4, 6, 3).mean((1, 3)).ravel()
    return Sig(g, colour, taken)


def similar(a: Sig, b: Sig) -> bool:
    corr = float((a.grey * b.grey).mean())
    if corr >= COPY_CORR and float(np.abs(a.colour - b.colour).mean()) <= MAX_CDIST:
        return True                                                  # a copy, whatever the date
    if corr < MIN_CORR or float(np.abs(a.colour - b.colour).mean()) > MAX_CDIST:
        return False
    if a.taken is not None and b.taken is not None:
        return abs(a.taken - b.taken) <= SAME_DAY
    return corr >= 0.75                                              # no dates: be stricter


class SigCache:
    """Signatures by (path, size, mtime): in memory while the program runs (slider is quick) and in a small
    SQLite file under %LOCALAPPDATA%\Duplikill, so the next run over the same folder is quick too."""

    def __init__(self, db_path: str | None = None):
        self.d: dict[tuple, Sig | None] = {}
        self.lock = threading.Lock()
        self.db_path = db_path if db_path is not None else _default_db()

    def _db(self):
        import sqlite3
        con = sqlite3.connect(self.db_path)
        con.execute("CREATE TABLE IF NOT EXISTS sig (path TEXT, size INTEGER, mtime REAL, grey BLOB, colour BLOB,"
                    " taken REAL, PRIMARY KEY (path, size, mtime))")
        return con

    def _load(self, keys: list[tuple]) -> None:
        if not self.db_path or not keys:
            return
        try:
            con = self._db()
            for k in keys:
                row = con.execute("SELECT grey, colour, taken FROM sig WHERE path=? AND size=? AND mtime=?", k).fetchone()
                if row:
                    self.d[k] = None if row[0] is None else Sig(np.frombuffer(row[0], np.float32),
                                                                np.frombuffer(row[1], np.float32), row[2])
            con.close()
        except Exception:
            pass

    def _save(self, items: list[tuple[tuple, Sig | None]]) -> None:
        if not self.db_path or not items:
            return
        try:
            con = self._db()
            con.executemany("INSERT OR REPLACE INTO sig VALUES (?,?,?,?,?,?)",
                            [(*k, None if s is None else s.grey.astype(np.float32).tobytes(),
                              None if s is None else s.colour.astype(np.float32).tobytes(),
                              None if s is None else s.taken) for k, s in items])
            con.commit()
            con.close()
        except Exception:
            pass

    def get_many(self, photos, stop: threading.Event | None = None, workers: int | None = None) -> dict[str, Sig | None]:
        keys = {p.path: (p.path, p.size, p.mtime) for p in photos}
        with self.lock:
            self._load([k for k in keys.values() if k not in self.d])
        out, todo = {}, []
        for path, k in keys.items():
            if k in self.d:
                out[path] = self.d[k]
            else:
                todo.append((k, path))
        if todo:
            new = []
            with ThreadPoolExecutor(max_workers=workers or os.cpu_count() or 8) as ex:
                for (k, path), sig in zip(todo, ex.map(lambda t: signature(t[1]), todo)):
                    if stop is not None and stop.is_set():
                        break
                    with self.lock:
                        self.d[k] = sig
                    out[path] = sig
                    new.append((k, sig))
            with self.lock:
                self._save(new)
        return out


def _default_db() -> str:
    base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Duplikill")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "signatures.sqlite")


def close(a: Sig, b: Sig) -> bool:
    """Same scene, ignoring the date: used so that every member of a group fits every other one."""
    if a is None or b is None:
        return False
    return float((a.grey * b.grey).mean()) >= MIN_CORR and float(np.abs(a.colour - b.colour).mean()) <= MAX_CDIST


LOOSE_CORR = 0.4     # no pair inside a group may fall below this ...
LOOSE_CDIST = 0.2    # ... or above this


def _fits(x: list, y: list, sigs) -> bool:
    """Average linkage with a floor, so no chains: on average every pair fits closely, no single pair is far
    off, and at least one pair is a real match (date included)."""
    pairs = [(sigs.get(a.path), sigs.get(b.path)) for a in x for b in y]
    if any(a is None or b is None for a, b in pairs):
        return False
    corr = [float((a.grey * b.grey).mean()) for a, b in pairs]
    cd = [float(np.abs(a.colour - b.colour).mean()) for a, b in pairs]
    if min(corr) < LOOSE_CORR or max(cd) > LOOSE_CDIST:
        return False
    if sum(corr) / len(corr) < MIN_CORR or sum(cd) / len(cd) > MAX_CDIST:
        return False
    return any(similar(a, b) for a, b in pairs)


def _agglomerate(clusters: list[list], sigs, candidates) -> list[list]:
    """Joins clusters along candidate pairs (i, j) as long as _fits holds; best pairs first."""
    alive = {i: c for i, c in enumerate(clusters)}
    owner = list(range(len(clusters)))

    def find(i):
        while owner[i] != i:
            i = owner[i]
        return i

    for i, j in candidates:
        a, b = find(i), find(j)
        if a != b and _fits(alive[a], alive[b], sigs):
            alive[a] = alive[a] + alive.pop(b)
            owner[b] = a
    return list(alive.values())


def refine(groups: list[Group], cache: SigCache | None = None, stop: threading.Event | None = None) -> list[Group]:
    cache = cache or SigCache()
    sigs = cache.get_many([p for g in groups for p in g.photos], stop)
    out = []
    for g in groups:
        ps = g.photos
        cand = []
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                a, b = sigs.get(ps[i].path), sigs.get(ps[j].path)
                if a is not None and b is not None and similar(a, b):
                    cand.append((-float((a.grey * b.grey).mean()), i, j))
        cand.sort()
        for members in _agglomerate([[p] for p in ps], sigs, [(i, j) for _, i, j in cand]):
            if len(members) > 1:
                out.append(Group(g.kind, members))
    return merge(out, cache)


SERIES_GAP = 15 * 60          # groups whose pictures were taken this close may be one series


def merge(groups: list[Group], cache: SigCache | None = None) -> list[Group]:
    """Joins groups that are really one series (Czkawka splits long series), without chaining."""
    if len(groups) < 2:
        return groups
    cache = cache or SigCache()
    sigs = cache.get_many([p for g in groups for p in g.photos])
    items = sorted(((sigs[p.path].taken, gi) for gi, g in enumerate(groups) for p in g.photos
                    if sigs.get(p.path) is not None and sigs[p.path].taken is not None))
    cand = []
    for i, (t, gi) in enumerate(items):
        for t2, gj in items[i + 1:]:
            if t2 - t > SERIES_GAP:
                break
            if gi != gj:
                cand.append((t2 - t, gi, gj))
    cand.sort()
    joined = _agglomerate([list(g.photos) for g in groups], sigs, [(i, j) for _, i, j in cand])
    return [Group(groups[0].kind, ps) for ps in joined]
