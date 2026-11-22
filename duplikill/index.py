"""Reading the photos: size, date, perceptual hash and sharpness, cached in SQLite.

Two engines compute the same numbers:
- CudaEngine: CPU processes decode at 1/8 size, the GPU hashes in batches of 256,
- CpuEngine: everything on the CPU (fallback without NVIDIA card).
"""
from __future__ import annotations

import os
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

import numpy as np

IMAGE_EXT = {".jpg", ".jpeg", ".jpe", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".bmp"}
SKIP_DIRS = {".picasaoriginals", "@eadir", ".thumbnails", ".dtrash", "$recycle.bin"}
HASH_SIZE = 32          # image is shrunk to 32x32 for the DCT
SHARP_SIZE = 256        # long edge used to measure sharpness

Progress = Callable[[int, int, str], None]


def data_dir() -> str:
    d = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Duplikill")
    os.makedirs(d, exist_ok=True)
    return d


@dataclass
class Photo:
    path: str
    size: int
    mtime: float
    width: int = 0
    height: int = 0
    taken: float | None = None     # EXIF DateTimeOriginal as a timestamp
    phash: int = 0                 # 64-bit perceptual hash (unsigned)
    sharp: float = 0.0             # variance of the Laplacian, higher = sharper
    blake: str | None = None       # full-file hash, only computed for exact duplicates
    diff: int = 0                  # Czkawka: hash distance to the group's first image

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)


# ---------------------------------------------------------------- cache

class Cache:
    def __init__(self, path: str | None = None):
        self.db = sqlite3.connect(path or os.path.join(data_dir(), "index.sqlite"), check_same_thread=False)
        self.db.execute(
            "create table if not exists files (path text primary key, size int, mtime real, width int,"
            " height int, taken real, phash int, sharp real, blake text)")
        self.db.execute("create table if not exists done (key text primary key, at real)")
        self.db.commit()

    def get_all(self) -> dict[str, Photo]:
        out = {}
        for r in self.db.execute("select path, size, mtime, width, height, taken, phash, sharp, blake from files"):
            out[r[0]] = Photo(r[0], r[1], r[2], r[3], r[4], r[5], r[6] & 0xFFFFFFFFFFFFFFFF, r[7], r[8])
        return out

    def put(self, photos: Iterable[Photo]) -> None:
        self.db.executemany(
            "insert or replace into files values (?,?,?,?,?,?,?,?,?)",
            [(p.path, p.size, p.mtime, p.width, p.height, p.taken, _signed(p.phash), p.sharp, p.blake) for p in photos])
        self.db.commit()

    def set_blake(self, path: str, blake: str) -> None:
        self.db.execute("update files set blake=? where path=?", (blake, path))

    def commit(self) -> None:
        self.db.commit()

    def forget(self, paths: Iterable[str]) -> None:
        self.db.executemany("delete from files where path=?", [(p,) for p in paths])
        self.db.commit()

    def is_done(self, key: str) -> bool:
        return self.db.execute("select 1 from done where key=?", (key,)).fetchone() is not None

    def done_keys(self) -> set[str]:
        return {r[0] for r in self.db.execute("select key from done")}

    def mark_done(self, key: str, done: bool = True) -> None:
        if done:
            self.db.execute("insert or replace into done values (?,?)", (key, time.time()))
        else:
            self.db.execute("delete from done where key=?", (key,))
        self.db.commit()


def _signed(v: int) -> int:
    return v - (1 << 64) if v >= 1 << 63 else v


# ---------------------------------------------------------------- shared maths

def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n)
    m = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n)) * np.sqrt(2 / n)
    m[0] /= np.sqrt(2)
    return m.astype(np.float32)


DCT = _dct_matrix(HASH_SIZE)


def phash_from_small(gray32: np.ndarray) -> int:
    """64-bit pHash from a 32x32 grayscale array."""
    d = DCT @ gray32.astype(np.float32) @ DCT.T
    low = d[:8, :8].flatten()
    bits = low > np.median(low[1:])
    return int(np.packbits(bits).view(">u8")[0])


def sharpness(gray: np.ndarray) -> float:
    g = gray.astype(np.float32)
    lap = -4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]
    return float(lap.var())


def exif_info(img) -> tuple[int, int, float | None, int]:
    """(width, height) as shown, DateTimeOriginal as timestamp, EXIF orientation."""
    w, h = img.size
    taken, orient = None, 1
    try:
        ex = img.getexif()
        orient = int(ex.get(0x0112, 1) or 1)
        raw = ex.get_ifd(0x8769).get(36867) or ex.get(306)
        if raw:
            taken = datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:
        pass
    if orient in (5, 6, 7, 8):
        w, h = h, w
    return w, h, taken, orient


# ---------------------------------------------------------------- CPU engine

def _analyze_cpu(path: str) -> tuple | None:
    from PIL import Image, ImageOps
    try:
        import pillow_heif  # noqa: F401
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    try:
        with Image.open(path) as img:
            w, h, taken, _ = exif_info(img)
            img.draft("L", (SHARP_SIZE, SHARP_SIZE))
            img = ImageOps.exif_transpose(img.convert("L"))
            img.thumbnail((SHARP_SIZE, SHARP_SIZE))
            g = np.asarray(img)
            small = np.asarray(img.resize((HASH_SIZE, HASH_SIZE), Image.BILINEAR))
            return (path, w, h, taken, phash_from_small(small), sharpness(g))
    except Exception:
        return None


class CpuEngine:
    name = "CPU"

    def __init__(self, workers: int | None = None):
        self.workers = workers or max(2, (os.cpu_count() or 4) - 1)
        self.name = f"CPU · {self.workers} Kerne"

    def analyze(self, paths: list[str], progress: Progress, offset: int = 0, total: int = 0) -> list[tuple]:
        out = []
        with ProcessPoolExecutor(self.workers) as ex:
            for i, r in enumerate(ex.map(_analyze_cpu, paths, chunksize=16)):
                if r:
                    out.append(r)
                if i % 50 == 0:
                    progress(offset + i, total or len(paths), "reading images (CPU)")
        return out


# ---------------------------------------------------------------- CUDA engine

def _decode_small(path: str) -> tuple | None:
    """CPU part for the GPU engine: EXIF + a 256 px grayscale array (JPEG draft mode = 1/8 decode)."""
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    try:
        with Image.open(path) as img:
            w, h, taken, _ = exif_info(img)
            img.draft("L", (SHARP_SIZE, SHARP_SIZE))
            img = ImageOps.exif_transpose(img.convert("L"))
            img.thumbnail((SHARP_SIZE, SHARP_SIZE))
            g = np.zeros((SHARP_SIZE, SHARP_SIZE), np.uint8)
            a = np.asarray(img)
            g[:a.shape[0], :a.shape[1]] = a
            return (path, w, h, taken, g, a.shape[0], a.shape[1])
    except Exception:
        return None


class CudaEngine:
    """CPU processes decode small grayscale images in parallel; the GPU hashes them in batches.

    Decoding runs ahead of the GPU (up to `ahead` batches), so neither side waits.
    """

    def __init__(self, batch: int = 256, ahead: int = 4, workers: int | None = None):
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("no CUDA device")
        self.torch = torch
        self.batch, self.ahead = batch, ahead
        self.workers = workers or max(2, (os.cpu_count() or 4) - 1)
        self.dev = torch.device("cuda")
        self.dct = torch.tensor(DCT, device=self.dev)
        self.name = f"CUDA · {torch.cuda.get_device_name(0)} + {self.workers} cores"

    def analyze(self, paths: list[str], progress: Progress, offset: int = 0, total: int = 0) -> list[tuple]:
        total = total or len(paths)
        out, buf, done = [], [], 0
        with ProcessPoolExecutor(self.workers) as ex:
            # imap-style: results stream in order while workers keep decoding ahead
            for r in ex.map(_decode_small, paths, chunksize=8):
                done += 1
                if r:
                    buf.append(r)
                if len(buf) >= self.batch:
                    out += self._gpu(buf)
                    buf = []
                if done % 64 == 0:
                    progress(offset + done, total, "reading images (CUDA)")
            if buf:
                out += self._gpu(buf)
        return out

    def _gpu(self, items: list[tuple]) -> list[tuple]:
        torch = self.torch
        F = torch.nn.functional
        x = torch.from_numpy(np.stack([it[4] for it in items])).to(self.dev, non_blocking=True).float()
        res = []
        hs = torch.tensor([it[5] for it in items], device=self.dev)
        ws = torch.tensor([it[6] for it in items], device=self.dev)
        # sharpness: Laplacian variance over the valid area of each image
        lap = (-4 * x[:, 1:-1, 1:-1] + x[:, :-2, 1:-1] + x[:, 2:, 1:-1] + x[:, 1:-1, :-2] + x[:, 1:-1, 2:])
        yy = torch.arange(1, SHARP_SIZE - 1, device=self.dev)[None, :, None]
        xx = torch.arange(1, SHARP_SIZE - 1, device=self.dev)[None, None, :]
        mask = ((yy < hs[:, None, None] - 1) & (xx < ws[:, None, None] - 1)).float()
        n = mask.sum((1, 2)).clamp(min=1)
        mean = (lap * mask).sum((1, 2)) / n
        var = ((lap - mean[:, None, None]) ** 2 * mask).sum((1, 2)) / n
        # pHash: resize each valid area to 32x32, DCT, compare low frequencies with their median
        smalls = []
        for i, it in enumerate(items):
            h, w = it[5], it[6]
            smalls.append(F.interpolate(x[i:i + 1, None, :h, :w], size=(HASH_SIZE, HASH_SIZE),
                                        mode="bilinear", align_corners=False, antialias=True)[0, 0])
        s = torch.stack(smalls)
        d = (self.dct @ s @ self.dct.T)[:, :8, :8].reshape(len(items), 64)
        bits = (d > d[:, 1:].median(dim=1).values[:, None]).cpu().numpy()
        packed = np.packbits(bits, axis=1).view(">u8")[:, 0]
        var = var.cpu().numpy()
        for i, it in enumerate(items):
            res.append((it[0], it[1], it[2], it[3], int(packed[i]), float(var[i])))
        return res



def make_engine(prefer_cuda: bool = True):
    if prefer_cuda:
        try:
            return CudaEngine()
        except Exception:
            pass
    return CpuEngine()


# ---------------------------------------------------------------- scanning

def walk(roots: list[str], exts: set[str] | None = IMAGE_EXT) -> list[tuple[str, int, float]]:
    files = []
    for root in roots:
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if d.lower() not in SKIP_DIRS and not d.startswith(".")]
            for f in fn:
                if exts is not None and os.path.splitext(f)[1].lower() not in exts:
                    continue
                p = os.path.join(dp, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                files.append((p, st.st_size, st.st_mtime))
    return files


def scan(roots: list[str], cache: Cache, engine, progress: Progress, batch: int = 2000) -> list[Photo]:
    """All images under roots, analysing only new or changed files (in batches, saved as it goes)."""
    progress(0, 0, "scanning folders")
    files = walk(roots)
    known = cache.get_all()
    photos, todo = [], []
    for p, size, mtime in files:
        c = known.get(p)
        if c and c.size == size and abs(c.mtime - mtime) < 1:
            photos.append(c)
        else:
            todo.append((p, size, mtime))
    meta = {p: (s, m) for p, s, m in todo}
    for i in range(0, len(todo), batch):
        chunk = [p for p, _, _ in todo[i:i + batch]]
        res = engine.analyze(chunk, progress, i, len(todo))
        new = [Photo(p, *meta[p], w, h, t, ph, sh) for p, w, h, t, ph, sh in res]
        cache.put(new)
        photos += new
    progress(len(todo), len(todo), f"{len(photos)} images ready")
    return photos
