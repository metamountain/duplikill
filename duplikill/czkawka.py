"""The search itself: runs czkawka_cli (same algorithm as Krokiet) and turns its JSON into groups.

czkawka_cli prints no progress, so the caller counts the images first (count_images) and
estimates the time (estimate_seconds). Its cache lives in %LOCALAPPDATA%\\Qarmin\\Czkawka\\cache
and is shared with an installed Krokiet.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field

from .groups import Group
from .index import Photo, data_dir

# extensions czkawka's image tool reads (without -x); used only for counting
IMAGE_EXT = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp",
             ".heic", ".heif", ".avif", ".jxl", ".ico", ".tga", ".pnm", ".qoi", ".dds", ".hdr", ".exr"}
MIN_SIZE = 16384                       # czkawka default -m
SPEED_UNCACHED = 4108 / 53             # images/s, measured on a 4108-image folder
SPEED_CACHED = 4108 / 4                # rough; cached runs only compare hashes

HASH_ALGS = ["Gradient", "Mean", "Blockhash", "VertGradient", "DoubleGradient", "Median"]
FILTERS = ["Nearest", "Triangle", "CatmullRom", "Gaussian", "Lanczos3"]


@dataclass
class Options:
    max_diff: int = 12                 # -s; with hash size 8: 10 = near copies, 20 = loose series
    hash_alg: str = "Gradient"
    hash_size: int = 8                 # measured 2026-11-22: size 16 finds only pairs even at -s 40
    image_filter: str = "Nearest"
    excluded: list[str] = field(default_factory=list)
    use_cache: bool = True


def exe_path() -> str:
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "tools", "windows_czkawka_cli.exe")


def command(root: str, out_json: str, o: Options) -> list[str]:
    cmd = [exe_path(), "image", "-d", root, "-s", str(o.max_diff), "-g", o.hash_alg,
           "-c", str(o.hash_size), "-z", o.image_filter, "-C", out_json, "-N", "-W",
           "-E", "$TRASH"]
    for e in o.excluded:
        cmd += ["-e", e]
    if not o.use_cache:
        cmd.append("-H")
    return cmd


def _excluded(path: str, excluded: list[str]) -> bool:
    p = os.path.normcase(os.path.abspath(path))
    return any(p == e or p.startswith(e.rstrip("\\/") + os.sep) for e in excluded)


def list_images(root: str, excluded: list[str] | None = None, stop: threading.Event | None = None) -> list[str]:
    """All image files Czkawka would look at (same extensions, same minimum size)."""
    ex = [os.path.normcase(os.path.abspath(e)) for e in excluded or []]
    out = []
    stack = [root]
    while stack:
        if stop is not None and stop.is_set():
            break
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if (not ex or not _excluded(e.path, ex)) and e.name.lower() not in ("$recycle.bin", "duplikill"):
                                stack.append(e.path)
                        elif os.path.splitext(e.name)[1].lower() in IMAGE_EXT and e.stat().st_size >= MIN_SIZE:
                            out.append(e.path)
                    except OSError:
                        pass
        except OSError:
            pass
    return out


def count_images(root: str, excluded: list[str] | None = None, stop: threading.Event | None = None) -> int:
    return len(list_images(root, excluded, stop))


def _load_cache_blob(o: Options) -> bytes:
    if not o.use_cache:
        return b""
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Qarmin", "Czkawka", "cache")
    files = glob.glob(os.path.join(base, f"cache_similar_images_{o.hash_size}_{o.hash_alg}_{o.image_filter}_*.bin"))
    if not files:
        return b""
    chunks = []
    try:
        for f in files:
            with open(f, "rb") as fh:
                chunks.append(fh.read())
    except OSError:
        return b""
    return b"".join(chunks).lower()


def cached_fractions(path_lists: list[list[str]], o: Options, sample: int = 200) -> list[float]:
    """Batched cached_fraction across multiple path lists, reading Czkawka cache files at most once."""
    if not path_lists:
        return []
    data = _load_cache_blob(o)
    if not data:
        return [1.0 if not ps else 0.0 for ps in path_lists]
    seen: dict[bytes, bool] = {}
    out: list[float] = []
    for ps in path_lists:
        if not ps:
            out.append(1.0)
            continue
        step = max(1, len(ps) // sample)
        pick = [p.lower().encode("utf-8") for p in ps[::step][:sample]]
        hits = 0
        for n in pick:
            ok = seen.get(n)
            if ok is None:
                ok = n in data
                seen[n] = ok
            if ok:
                hits += 1
        out.append(hits / len(pick))
    return out


def cached_fraction(paths: list[str], o: "Options", sample: int = 200) -> float:
    """Share of the images Czkawka already has hashed (its cache file stores the paths as text)."""
    if not paths:
        return 1.0
    return cached_fractions([paths], o, sample)[0]


def subfolders(root: str, paths: list[str]) -> list[tuple[str, int]]:
    """Image count per direct subfolder of root, biggest first."""
    counts: dict[str, int] = {}
    for p in paths:
        rel = os.path.relpath(p, root)
        top = rel.split(os.sep)[0] if os.sep in rel else ""
        if top:
            counts[top] = counts.get(top, 0) + 1
    return sorted(counts.items(), key=lambda t: -t[1])


def estimate_seconds(n_images: int, cached_fraction: float = 0.0) -> float:
    return n_images * ((1 - cached_fraction) / SPEED_UNCACHED + cached_fraction / SPEED_CACHED)


def parse(json_path: str) -> list[Group]:
    with open(json_path, encoding="utf-8") as fh:
        raw = json.load(fh)
    dir_files: dict[str, dict[str, tuple[str, bool]]] = {}
    groups = []
    for items in raw:
        photos = []
        seen = set()
        for it in items:
            raw_path = it["path"]
            norm_raw = os.path.normcase(raw_path)
            if "duplikill" in norm_raw.split(os.sep)[:-1]:
                continue
            dn, fn = os.path.dirname(raw_path), os.path.basename(raw_path)
            dk = os.path.normcase(dn)
            files = dir_files.get(dk)
            if files is None:
                files = {}
                try:
                    rd = os.path.realpath(dn or ".", strict=True)
                    with os.scandir(rd) as it_dir:
                        for e in it_dir:
                            try:
                                if e.is_file():
                                    files[os.path.normcase(e.name)] = (e.path, e.is_symlink())
                            except OSError:
                                pass
                except OSError:
                    pass
                dir_files[dk] = files
            hit = files.get(os.path.normcase(fn))
            if hit is None:
                continue
            p, is_sym = hit
            if is_sym:
                try:
                    p = os.path.realpath(p, strict=True)
                except OSError:
                    continue
            np_ = os.path.normcase(p)
            if np_ in seen or "duplikill" in np_.split(os.sep)[:-1]:
                continue
            seen.add(np_)
            photos.append(Photo(path=p, size=it.get("size", 0), mtime=float(it.get("modified_date", 0)),
                                width=it.get("width", 0), height=it.get("height", 0),
                                diff=int(it.get("difference", 0))))
        if len(photos) > 1:
            groups.append(Group("similar", photos))
    return groups


class Cancelled(Exception):
    pass


def search(root: str, o: Options, stop: threading.Event | None = None, workdir: str | None = None) -> list[Group]:
    """Runs czkawka_cli and blocks until it is done; setting `stop` kills it (raises Cancelled)."""
    workdir = workdir or os.path.join(data_dir(), "runs")
    os.makedirs(workdir, exist_ok=True)
    fd, out = tempfile.mkstemp(suffix=".json", dir=workdir)
    os.close(fd)
    os.remove(out)
    log_path = out[:-5] + ".log"
    try:
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(command(root, out, o), stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            while proc.poll() is None:
                if stop is not None and stop.is_set():
                    proc.kill()
                    proc.wait()
                    raise Cancelled()
                try:
                    proc.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
        if proc.returncode != 0 or not os.path.exists(out):
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                tail = fh.read()[-2000:]
            raise RuntimeError(f"czkawka_cli exited with code {proc.returncode}:\n{tail}")
        return parse(out)
    finally:
        for p in (out, log_path):
            try:
                os.remove(p)
            except OSError:
                pass
