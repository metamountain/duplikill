"""The search itself: runs czkawka_cli (same algorithm as Krokiet) and turns its JSON into groups.

czkawka_cli prints no progress, so the caller counts the images first (count_images) and
estimates the time (estimate_seconds). Its cache lives in %LOCALAPPDATA%\\Qarmin\\Czkawka\\cache
and is shared with an installed Krokiet.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
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
                            if not _excluded(e.path, ex) and e.name.lower() not in ("$recycle.bin", "duplikill"):
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


def cached_fraction(paths: list[str], o: "Options", sample: int = 200) -> float:
    """Share of the images Czkawka already has hashed (its cache file stores the paths as text)."""
    if not paths:
        return 1.0
    import glob
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Qarmin", "Czkawka", "cache")
    files = glob.glob(os.path.join(base, f"cache_similar_images_{o.hash_size}_{o.hash_alg}_{o.image_filter}_*.bin"))
    if not files:
        return 0.0
    try:
        data = b"".join(open(f, "rb").read() for f in files).lower()
    except OSError:
        return 0.0
    step = max(1, len(paths) // sample)
    pick = paths[::step][:sample]
    return sum(p.lower().encode("utf-8") in data for p in pick) / len(pick)


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


def _real(path: str) -> str | None:
    """czkawka reports paths in lower case; get the spelling on disk (None if the file is gone)."""
    if not os.path.isfile(path):
        return None
    return os.path.realpath(path)


def parse(json_path: str) -> list[Group]:
    with open(json_path, encoding="utf-8") as fh:
        raw = json.load(fh)
    groups = []
    for items in raw:
        photos = []
        seen = set()
        for it in items:
            p = _real(it["path"])
            if p is None or os.path.normcase(p) in seen or "duplikill" in os.path.normcase(p).split(os.sep)[:-1]:
                continue
            seen.add(os.path.normcase(p))
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
                time.sleep(0.2)
        if proc.returncode != 0 or not os.path.exists(out):
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                tail = fh.read()[-2000:]
            raise RuntimeError(f"czkawka_cli ist mit Code {proc.returncode} beendet:\n{tail}")
        return parse(out)
    finally:
        for p in (out, log_path):
            try:
                os.remove(p)
            except OSError:
                pass
