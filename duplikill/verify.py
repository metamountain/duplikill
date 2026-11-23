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
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing

import numpy as np

from .groups import Group
from .index import data_dir, exif_info, register_heif, sharpness

register_heif()

MIN_CORR = 0.6
MAX_CDIST = 0.12
COPY_CORR = 0.9
SAME_DAY = 24 * 3600
LOOSE_CORR = 0.4     # no pair inside a group may fall below this ...
LOOSE_CDIST = 0.2    # ... or above this
SERIES_GAP = 15 * 60          # groups whose pictures were taken this close may be one series
_DB_CHUNK = 900


class Sig:
    __slots__ = ("grey", "colour", "taken", "sharp")

    def __init__(self, grey, colour, taken, sharp: float = 0.0):
        self.grey, self.colour, self.taken, self.sharp = grey, colour, taken, float(sharp or 0.0)


SHARP_SIDE = 1024
TABLE = "sig2"          # v2: sharpness at 1024 px (v1 rows had sharp = 0 from a late ALTER TABLE)


def signature(path: str) -> Sig | None:
    from PIL import Image, ImageOps
    try:
        with Image.open(path) as im:
            _, _, taken, _ = exif_info(im)
            im.draft("RGB", (SHARP_SIDE, SHARP_SIDE))
            im = ImageOps.exif_transpose(im.convert("RGB"))
            small = np.asarray(im.resize((24, 24), Image.BILINEAR), np.float32) / 255
            im.thumbnail((SHARP_SIDE, SHARP_SIDE))     # blur of a few pixels vanishes at 256 px, not at 1024
            sharp = sharpness(np.asarray(im.convert("L")))
    except Exception:
        return None
    g = small.mean(2).ravel()
    g = (g - g.mean()) / (g.std() + 1e-6)
    colour = small.reshape(4, 6, 4, 6, 3).mean((1, 3)).ravel()
    return Sig(g, colour, taken, sharp)


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
    r"""Signatures by (path, size, mtime): in memory while the program runs (slider is quick) and in a small
    SQLite file under %LOCALAPPDATA%\Duplikill, so the next run over the same folder is quick too."""

    def __init__(self, db_path: str | None = None):
        self.d: dict[tuple, Sig | None] = {}
        self.lock = threading.Lock()
        self.db_path = db_path if db_path is not None else os.path.join(data_dir(), "signatures.sqlite")
        self._schema_ready = False

    def _db(self):
        import sqlite3
        con = sqlite3.connect(self.db_path)
        if not self._schema_ready:
            con.execute(
                f"CREATE TABLE IF NOT EXISTS {TABLE} (path TEXT, size INTEGER, mtime REAL, grey BLOB, colour BLOB,"
                " taken REAL, sharp REAL DEFAULT 0.0, PRIMARY KEY (path, size, mtime))"
            )
            con.execute("DROP TABLE IF EXISTS sig")
            con.commit()
            self._schema_ready = True
        return con

    def _load(self, keys: list[tuple]) -> None:
        if not self.db_path or not keys:
            return
        wanted = set(keys)
        paths = list({k[0] for k in keys})
        try:
            with closing(self._db()) as con, con:
                for i in range(0, len(paths), _DB_CHUNK):
                    chunk = paths[i:i + _DB_CHUNK]
                    q = ",".join("?" * len(chunk))
                    sql = f"SELECT path, size, mtime, grey, colour, taken, sharp FROM {TABLE} WHERE path IN ({q})"
                    for p, sz, mt, grey, colour, taken, sharp in con.execute(sql, chunk):
                        k = (p, sz, mt)
                        if k in wanted:
                            self.d[k] = None if grey is None else Sig(
                                np.frombuffer(grey, np.float32),
                                np.frombuffer(colour, np.float32),
                                taken,
                                sharp or 0.0,
                            )
        except Exception:
            self._schema_ready = False

    def _save(self, items: list[tuple[tuple, Sig | None]]) -> None:
        if not self.db_path or not items:
            return
        try:
            with closing(self._db()) as con, con:
                con.executemany(
                    f"INSERT OR REPLACE INTO {TABLE} VALUES (?,?,?,?,?,?,?)",
                    [(*k,
                      None if s is None else s.grey.astype(np.float32).tobytes(),
                      None if s is None else s.colour.astype(np.float32).tobytes(),
                      None if s is None else s.taken,
                      None if s is None else s.sharp) for k, s in items],
                )
        except Exception:
            self._schema_ready = False

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
        if todo and not (stop is not None and stop.is_set()):
            new = []
            ex = ThreadPoolExecutor(max_workers=workers or os.cpu_count() or 8)
            try:
                futs = {
                    ex.submit(
                        lambda p: None if (stop is not None and stop.is_set()) else signature(p),
                        path,
                    ): (k, path)
                    for k, path in todo
                }
                for fut in as_completed(futs):
                    if stop is not None and stop.is_set():
                        for f in futs:
                            f.cancel()
                        break
                    k, path = futs[fut]
                    sig = fut.result()
                    with self.lock:
                        self.d[k] = sig
                    out[path] = sig
                    new.append((k, sig))
            finally:
                ex.shutdown(wait=stop is None or not stop.is_set(), cancel_futures=True)
            if new:
                with self.lock:
                    self._save(new)
        return out


def _fits(x: list, y: list, sigs) -> bool:
    """Average linkage with a floor, so no chains: on average every pair fits closely, no single pair is far
    off, and at least one pair is a real match (date included)."""
    if not x or not y:
        return False
    sx = [sigs.get(a.path) for a in x]
    sy = [sigs.get(b.path) for b in y]
    if any(s is None for s in sx) or any(s is None for s in sy):
        return False
    a_grey = np.stack([s.grey for s in sx])
    b_grey = np.stack([s.grey for s in sy])
    corr = (a_grey @ b_grey.T) / a_grey.shape[1]
    if float(corr.min()) < LOOSE_CORR or float(corr.mean()) < MIN_CORR:
        return False
    a_col = np.stack([s.colour for s in sx])
    b_col = np.stack([s.colour for s in sy])
    cd = np.abs(a_col[:, None, :] - b_col[None, :, :]).mean(axis=2)
    if float(cd.max()) > LOOSE_CDIST or float(cd.mean()) > MAX_CDIST:
        return False
    if bool(((corr >= COPY_CORR) & (cd <= MAX_CDIST)).any()):
        return True
    base = (corr >= MIN_CORR) & (cd <= MAX_CDIST)
    if not bool(base.any()):
        return False
    ta = np.array([np.nan if s.taken is None else s.taken for s in sx], dtype=np.float64)
    tb = np.array([np.nan if s.taken is None else s.taken for s in sy], dtype=np.float64)
    both_t = np.isfinite(ta)[:, None] & np.isfinite(tb)[None, :]
    date_ok = np.where(both_t, np.abs(ta[:, None] - tb[None, :]) <= SAME_DAY, corr >= 0.75)
    return bool((base & date_ok).any())


def _agglomerate(clusters: list[list], sigs, candidates) -> list[list]:
    """Joins clusters along candidate pairs (i, j) as long as _fits holds; best pairs first."""
    alive = {i: c for i, c in enumerate(clusters)}
    owner = list(range(len(clusters)))
    ver = [0] * len(clusters)
    rejected: set[tuple[int, int, int, int]] = set()

    def find(i):
        while owner[i] != i:
            owner[i] = owner[owner[i]]
            i = owner[i]
        return i

    for i, j in candidates:
        a, b = find(i), find(j)
        if a == b:
            continue
        u, v = (a, b) if a < b else (b, a)
        state = (u, ver[u], v, ver[v])
        if state in rejected:
            continue
        if _fits(alive[a], alive[b], sigs):
            alive[a] = alive[a] + alive.pop(b)
            owner[b] = a
            ver[a] += 1
        else:
            rejected.add(state)
    return list(alive.values())


def _apply_sigs(photos: list, sigs: dict[str, Sig | None]) -> None:
    for p in photos:
        s = sigs.get(p.path)
        if s is not None:
            if s.taken is not None and p.taken is None:
                p.taken = s.taken
            if s.sharp and not p.sharp:
                p.sharp = s.sharp


def _group_matrices(sigs_list: list[Sig]):
    greys = np.stack([s.grey for s in sigs_list])
    colours = np.stack([s.colour for s in sigs_list])
    k = len(sigs_list)
    corr = (greys @ greys.T) / greys.shape[1]
    cd = np.empty((k, k), dtype=np.float32)
    for s in range(0, k, 128):
        cd[s:s + 128] = np.abs(colours[s:s + 128, None, :] - colours[None, :, :]).mean(axis=2)
    takens = np.array([np.nan if s.taken is None else s.taken for s in sigs_list], dtype=np.float64)
    both_t = np.isfinite(takens)[:, None] & np.isfinite(takens)[None, :]
    same_day = np.abs(takens[:, None] - takens[None, :]) <= SAME_DAY
    sim = ((corr >= COPY_CORR) & (cd <= MAX_CDIST)) | (
        (corr >= MIN_CORR) & (cd <= MAX_CDIST) & np.where(both_t, same_day, corr >= 0.75)
    )
    return corr, cd, sim


def refine(groups: list[Group], cache: SigCache | None = None, stop: threading.Event | None = None) -> list[Group]:
    cache = cache or SigCache()
    sigs = cache.get_many([p for g in groups for p in g.photos], stop)
    if stop is not None and stop.is_set():
        return []
    out = []
    for g in groups:
        ps = g.photos
        _apply_sigs(ps, sigs)
        valid = [(p, sigs[p.path]) for p in ps if sigs.get(p.path) is not None]
        if len(valid) < 2:
            continue
        v_photos = [p for p, _ in valid]
        corr, _, sim = _group_matrices([s for _, s in valid])
        ii, jj = np.nonzero(np.triu(sim, 1))
        if not len(ii):
            continue
        order = np.argsort(-corr[ii, jj])
        pairs = [(int(ii[idx]), int(jj[idx])) for idx in order.tolist()]
        for cluster in _agglomerate([[p] for p in v_photos], sigs, pairs):
            if len(cluster) > 1:
                out.append(Group(g.kind, cluster))
    return merge(out, cache, sigs)


def merge(groups: list[Group], cache: SigCache | None = None,
          _sigs: dict[str, Sig | None] | None = None) -> list[Group]:
    """Joins groups that are really one series (Czkawka splits long series), without chaining."""
    if len(groups) < 2:
        return groups
    if _sigs is None:
        cache = cache or SigCache()
        sigs = cache.get_many([p for g in groups for p in g.photos])
        for g in groups:
            _apply_sigs(g.photos, sigs)
    else:
        sigs = _sigs
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
