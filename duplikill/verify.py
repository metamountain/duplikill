"""Second check after Czkawka: its 8x8 gradient hash also groups unrelated pictures with a similar
light/dark layout (cat next to a skate park), and misses series where a small pan/zoom shifted the 8x8 hash.
Every candidate pair (from Czkawka and from EXIF time-series runs in the same folder) is checked:

- structure: 24x24 greyscale correlation (with up to +-2 cell shift tolerance for time series) >= min_corr
- detail:    5x5 high-pass detail correlation >= MIN_HP_CORR (suppresses plain studio backgrounds)
- colour:    mean distance of a 4x4 colour layout <= max_cdist (or exposure-invariant chroma match on bursts)
- time:      taken on the same day (EXIF), unless it is practically a copy (corr >= COPY_CORR)

Groups are rebuilt from the pairs that pass (average linkage with floor); single images drop out.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass
import os
import threading

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .groups import Group
from .index import Photo, data_dir, exif_info, register_heif, sharpness

register_heif()

MIN_CORR = 0.6
MAX_CDIST = 0.12
COPY_CORR = 0.9
SAME_DAY = 24 * 3600
LOOSE_CORR = 0.4        # no pair inside a group may fall below this ...
LOOSE_CDIST = 0.2       # ... or above this
MIN_HP_CORR = 0.22      # 5x5 high-pass detail floor: separates different studio subjects on plain backgrounds
BURST_GAP = 15.0        # seconds: burst window for exposure/flash-invariant chroma matching
BURST_CORR = 0.90       # minimum shifted structure correlation for exposure-invariant burst match
BURST_CDIST = 0.28      # maximum raw colour distance allowed when burst structure + chroma match
SERIES_GAP = 15 * 60    # groups whose pictures were taken this close may be one series
_DB_CHUNK = 900


@dataclass(frozen=True)
class Thresholds:
    min_corr: float = MIN_CORR
    loose_corr: float = LOOSE_CORR
    max_cdist: float = MAX_CDIST
    loose_cdist: float = LOOSE_CDIST
    min_hp_corr: float = MIN_HP_CORR
    series_gap: float = SERIES_GAP
    max_shift: int = 2


def thresholds_for(max_diff: int | None = None) -> Thresholds:
    """Scales verification thresholds and time-series window smoothly with the STRICT <0..17> LOOSE slider."""
    if max_diff is None:
        return Thresholds()
    u = max(0.0, min(1.0, float(max_diff) / 17.0))
    # At default max_diff=12..14, u ~ 0.71..0.82 -> min_corr ~ 0.60, max_cdist ~ 0.12, series_gap ~ 650..750s
    min_corr = round(0.84 - 0.28 * u, 3)         # 0 -> 0.84 (very strict), 12 -> 0.64, 14 -> 0.61, 17 -> 0.56
    loose_corr = round(max(0.35, min_corr - 0.20), 3)
    max_cdist = round(0.07 + 0.065 * u, 3)       # 0 -> 0.07, 12 -> 0.116, 14 -> 0.123, 17 -> 0.135
    loose_cdist = round(max_cdist + 0.08, 3)
    min_hp_corr = round(0.30 - 0.10 * u, 3)      # 0 -> 0.30, 14 -> 0.22, 17 -> 0.20
    if max_diff <= 2:
        gap = 15.0 * max_diff                    # 0 -> 0s (copies only), 1 -> 15s, 2 -> 30s
    else:
        gap = round(30.0 + (SERIES_GAP - 30.0) * ((u - 2 / 17) / (1 - 2 / 17)) ** 1.2, 1)
    shift = 0 if max_diff == 0 else (1 if max_diff <= 5 else 2)
    return Thresholds(min_corr, loose_corr, max_cdist, loose_cdist, min_hp_corr, gap, shift)


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


def _highpass_greys(greys: np.ndarray) -> np.ndarray:
    """5x5 local high-pass filter on 24x24 grey signatures: removes uniform backgrounds (e.g. studio walls)."""
    g2d = greys.reshape(-1, 24, 24)
    padded = np.pad(g2d, ((0, 0), (2, 2), (2, 2)), mode="edge")
    local_mean = sliding_window_view(padded, (5, 5), axis=(1, 2)).mean(axis=(-1, -2))
    hp = (g2d - local_mean).reshape(len(greys), 576)
    hp = (hp - hp.mean(axis=1, keepdims=True)) / (hp.std(axis=1, keepdims=True) + 1e-6)
    return hp.astype(np.float32)


def _shifted_greys(greys: np.ndarray, max_shift: int = 2) -> np.ndarray:
    """Normalized 24x24 grey signatures shifted by dx, dy in [-max_shift..max_shift]: shape (S, N, 576)."""
    g2d = greys.reshape(-1, 24, 24)
    padded = np.pad(g2d, ((0, 0), (max_shift, max_shift), (max_shift, max_shift)), mode="edge")
    views = []
    for dy in range(-max_shift, max_shift + 1):
        for dx in range(-max_shift, max_shift + 1):
            y0, x0 = max_shift + dy, max_shift + dx
            v = padded[:, y0:y0 + 24, x0:x0 + 24].reshape(len(greys), 576)
            v = (v - v.mean(axis=1, keepdims=True)) / (v.std(axis=1, keepdims=True) + 1e-6)
            views.append(v.astype(np.float32))
    return np.stack(views, axis=0)


def _corr_matrix(a_grey: np.ndarray, b_grey: np.ndarray, ta: np.ndarray, tb: np.ndarray,
                 max_shift: int = 2, series_gap: float = SERIES_GAP) -> np.ndarray:
    """Pairwise structure correlation; for shots taken within series_gap, allows small camera pan/zoom shifts."""
    base_corr = (a_grey @ b_grey.T) / a_grey.shape[1]
    if max_shift <= 0:
        return base_corr
    both_t = np.isfinite(ta)[:, None] & np.isfinite(tb)[None, :]
    close_t = both_t & (np.abs(ta[:, None] - tb[None, :]) <= series_gap)
    if not bool(close_t.any()):
        return base_corr
    b_views = _shifted_greys(b_grey, max_shift)
    shifted_corr = (np.einsum("ik,sjk->sij", a_grey, b_views) / a_grey.shape[1]).max(axis=0)
    if a_grey is b_grey or (a_grey.shape == b_grey.shape and np.may_share_memory(a_grey, b_grey)):
        shifted_corr = np.maximum(shifted_corr, shifted_corr.T)
    else:
        a_views = _shifted_greys(a_grey, max_shift)
        rev_corr = (np.einsum("sik,jk->sij", a_views, b_grey) / a_grey.shape[1]).max(axis=0)
        shifted_corr = np.maximum(shifted_corr, rev_corr)
    return np.where(close_t, np.maximum(base_corr, shifted_corr), base_corr)


def _chroma_cdist(a_col: np.ndarray, b_col: np.ndarray) -> np.ndarray:
    """Exposure/white-balance invariant colour layout distance (subtracts each image's mean RGB first)."""
    ra = a_col.reshape(-1, 16, 3)
    rb = b_col.reshape(-1, 16, 3)
    ra_n = (ra - ra.mean(axis=1, keepdims=True)).reshape(len(a_col), 48)
    rb_n = (rb - rb.mean(axis=1, keepdims=True)).reshape(len(b_col), 48)
    out = np.empty((len(a_col), len(b_col)), dtype=np.float32)
    for s in range(0, len(a_col), 128):
        out[s:s + 128] = np.abs(ra_n[s:s + 128, None, :] - rb_n[None, :, :]).mean(axis=2)
    return out


def _effective_cdist(a_col: np.ndarray, b_col: np.ndarray, corr: np.ndarray, hp_corr: np.ndarray,
                     ta: np.ndarray, tb: np.ndarray) -> np.ndarray:
    """Colour distance; for high-correlation bursts (dt <= BURST_GAP) where flash or auto-exposure shifted
    brightness, uses exposure-invariant chroma distance if raw cdist <= BURST_CDIST."""
    k = len(a_col)
    raw_cd = np.empty((k, len(b_col)), dtype=np.float32)
    for s in range(0, k, 128):
        raw_cd[s:s + 128] = np.abs(a_col[s:s + 128, None, :] - b_col[None, :, :]).mean(axis=2)
    both_t = np.isfinite(ta)[:, None] & np.isfinite(tb)[None, :]
    burst_exposure = (
        both_t
        & (np.abs(ta[:, None] - tb[None, :]) <= BURST_GAP)
        & (corr >= BURST_CORR)
        & (hp_corr >= 0.5)
        & (raw_cd <= BURST_CDIST)
    )
    if not bool(burst_exposure.any()):
        return raw_cd
    chroma_cd = _chroma_cdist(a_col, b_col)
    return np.where(burst_exposure, np.minimum(raw_cd, chroma_cd), raw_cd)


def similar(a: Sig, b: Sig, max_diff: int | None = None) -> bool:
    th = thresholds_for(max_diff)
    ta = np.array([np.nan if a.taken is None else a.taken], dtype=np.float64)
    tb = np.array([np.nan if b.taken is None else b.taken], dtype=np.float64)
    ga, gb = a.grey[None, :], b.grey[None, :]
    corr = float(_corr_matrix(ga, gb, ta, tb, th.max_shift, th.series_gap)[0, 0])
    hpa, hpb = _highpass_greys(ga), _highpass_greys(gb)
    hp_corr = float(_corr_matrix(hpa, hpb, ta, tb, th.max_shift, th.series_gap)[0, 0])
    if hp_corr < th.min_hp_corr and corr < 0.96:
        return False
    ca, cb = a.colour[None, :], b.colour[None, :]
    cd = float(_effective_cdist(ca, cb, np.array([[corr]]), np.array([[hp_corr]]), ta, tb)[0, 0])
    if corr >= COPY_CORR and cd <= th.max_cdist and hp_corr >= th.min_hp_corr:
        return True                                                  # a copy, whatever the date
    if corr < th.min_corr or cd > th.max_cdist:
        return False
    if a.taken is not None and b.taken is not None:
        return abs(a.taken - b.taken) <= SAME_DAY
    return corr >= max(0.75, th.min_corr)                            # no dates: be stricter


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


def _fits(x: list, y: list, sigs, th: Thresholds | None = None) -> bool:
    """Average linkage with a floor, so no chains: on average every pair fits closely, no single pair is far
    off, and at least one pair is a real match (date included)."""
    if not x or not y:
        return False
    th = th or Thresholds()
    sx = [sigs.get(a.path) for a in x]
    sy = [sigs.get(b.path) for b in y]
    if any(s is None for s in sx) or any(s is None for s in sy):
        return False
    a_grey = np.stack([s.grey for s in sx])
    b_grey = np.stack([s.grey for s in sy])
    ta = np.array([np.nan if s.taken is None else s.taken for s in sx], dtype=np.float64)
    tb = np.array([np.nan if s.taken is None else s.taken for s in sy], dtype=np.float64)
    corr = _corr_matrix(a_grey, b_grey, ta, tb, th.max_shift, th.series_gap)
    if float(corr.min()) < th.loose_corr or float(corr.mean()) < th.min_corr:
        return False
    a_hp = _highpass_greys(a_grey)
    b_hp = _highpass_greys(b_grey)
    hp_corr = _corr_matrix(a_hp, b_hp, ta, tb, th.max_shift, th.series_gap)
    if float(hp_corr.mean()) < th.min_hp_corr and float(corr.min()) < 0.96:
        return False
    a_col = np.stack([s.colour for s in sx])
    b_col = np.stack([s.colour for s in sy])
    cd = _effective_cdist(a_col, b_col, corr, hp_corr, ta, tb)
    if float(cd.max()) > th.loose_cdist or float(cd.mean()) > th.max_cdist:
        return False
    detail_ok = (hp_corr >= th.min_hp_corr) | (corr >= 0.96)
    if bool(((corr >= COPY_CORR) & (cd <= th.max_cdist) & detail_ok).any()):
        return True
    base = (corr >= th.min_corr) & (cd <= th.max_cdist) & detail_ok
    if not bool(base.any()):
        return False
    both_t = np.isfinite(ta)[:, None] & np.isfinite(tb)[None, :]
    date_ok = np.where(both_t, np.abs(ta[:, None] - tb[None, :]) <= SAME_DAY, corr >= max(0.75, th.min_corr))
    return bool((base & date_ok).any())


def _agglomerate(clusters: list[list], sigs, candidates, th: Thresholds | None = None) -> list[list]:
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
        if _fits(alive[a], alive[b], sigs, th):
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


def _group_matrices(sigs_list: list[Sig], th: Thresholds | None = None):
    th = th or Thresholds()
    greys = np.stack([s.grey for s in sigs_list])
    colours = np.stack([s.colour for s in sigs_list])
    takens = np.array([np.nan if s.taken is None else s.taken for s in sigs_list], dtype=np.float64)
    corr = _corr_matrix(greys, greys, takens, takens, th.max_shift, th.series_gap)
    hp = _highpass_greys(greys)
    hp_corr = _corr_matrix(hp, hp, takens, takens, th.max_shift, th.series_gap)
    cd = _effective_cdist(colours, colours, corr, hp_corr, takens, takens)
    both_t = np.isfinite(takens)[:, None] & np.isfinite(takens)[None, :]
    same_day = np.abs(takens[:, None] - takens[None, :]) <= SAME_DAY
    detail_ok = (hp_corr >= th.min_hp_corr) | (corr >= 0.96)
    sim = ((corr >= COPY_CORR) & (cd <= th.max_cdist) & detail_ok) | (
        (corr >= th.min_corr) & (cd <= th.max_cdist) & detail_ok & np.where(both_t, same_day, corr >= max(0.75, th.min_corr))
    )
    return corr, cd, sim


def _combine_with_time_series(groups: list[Group], paths: list[str], cache: SigCache,
                              stop: threading.Event | None = None,
                              series_gap: float = SERIES_GAP) -> tuple[list[Group], dict[str, Sig | None]]:
    """Unions Czkawka candidate groups with same-folder time-runs (shots taken within series_gap) so series
    whose 8x8 Czkawka gradient hash shifted are still checked by verify.refine."""
    by_path: dict[str, Photo] = {}
    for g in groups:
        for p in g.photos:
            by_path[os.path.normcase(p.path)] = p
    for p in paths:
        k = os.path.normcase(p)
        if k not in by_path:
            try:
                st = os.stat(p)
                by_path[k] = Photo(p, st.st_size, st.st_mtime)
            except OSError:
                pass
    all_photos = list(by_path.values())
    sigs = cache.get_many(all_photos, stop)
    if stop is not None and stop.is_set():
        return [], sigs
    _apply_sigs(all_photos, sigs)

    parent = {k: k for k in by_path}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for g in groups:
        ks = [os.path.normcase(p.path) for p in g.photos if os.path.normcase(p.path) in parent]
        for k in ks[1:]:
            union(ks[0], k)

    if series_gap > 0:
        by_folder: dict[str, list[Photo]] = defaultdict(list)
        for p in all_photos:
            if p.taken is not None:
                by_folder[os.path.normcase(p.folder)].append(p)
        for ps in by_folder.values():
            ps.sort(key=lambda p: (p.taken, p.path))
            for a, b in zip(ps, ps[1:]):
                if b.taken - a.taken <= series_gap:
                    union(os.path.normcase(a.path), os.path.normcase(b.path))

    comps: dict[str, list[Photo]] = defaultdict(list)
    for k, p in by_path.items():
        comps[find(k)].append(p)
    kind = groups[0].kind if groups else "similar"
    return [Group(kind, ps) for ps in comps.values() if len(ps) > 1], sigs


def refine(groups: list[Group], cache: SigCache | None = None, stop: threading.Event | None = None,
           paths: list[str] | None = None, max_diff: int | None = None) -> list[Group]:
    cache = cache or SigCache()
    th = thresholds_for(max_diff)
    if paths:
        groups, sigs = _combine_with_time_series(groups, paths, cache, stop, th.series_gap)
    else:
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
        corr, _, sim = _group_matrices([s for _, s in valid], th)
        ii, jj = np.nonzero(np.triu(sim, 1))
        if not len(ii):
            continue
        order = np.argsort(-corr[ii, jj])
        pairs = [(int(ii[idx]), int(jj[idx])) for idx in order.tolist()]
        for cluster in _agglomerate([[p] for p in v_photos], sigs, pairs, th):
            if len(cluster) > 1:
                out.append(Group(g.kind, cluster))
    return merge(out, cache, sigs, th)


def merge(groups: list[Group], cache: SigCache | None = None,
          _sigs: dict[str, Sig | None] | None = None, th: Thresholds | None = None) -> list[Group]:
    """Joins groups that are really one series (Czkawka splits long series), without chaining."""
    if len(groups) < 2:
        return groups
    th = th or Thresholds()
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
            if t2 - t > th.series_gap:
                break
            if gi != gj:
                cand.append((t2 - t, gi, gj))
    cand.sort()
    joined = _agglomerate([list(g.photos) for g in groups], sigs, [(i, j) for _, i, j in cand], th)
    return [Group(groups[0].kind, ps) for ps in joined]
