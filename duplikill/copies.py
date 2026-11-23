"""Exact copies across two folders, and merging them in one step.

"Exact" = the same picture data, whatever the metadata says: for a JPEG the bytes from the first SOS marker
to the end (the compressed image), so a copy that only lost or gained tags (XMP, IPTC) still counts.
Other formats: the whole file.

A folder pair is offered for a merge when it shares at least MIN_COPIES such copies. The kept folder is the
one whose images are mostly copies (the sorted album, not the big dump it came from). Per copy the file with
more metadata wins: if the copy that goes holds the tags, it takes the place of the untagged one instead.
"""
from __future__ import annotations

import hashlib
import os
import threading
from collections import defaultdict
from dataclasses import dataclass, field

from .actions import sidecars

MIN_COPIES = 5
_CHUNK = 1 << 20
_XMP = (b"http://ns.adobe.com/xap/1.0/\x00", b"http://ns.adobe.com/xmp/extension/\x00")

_cache: dict[tuple, tuple[str, int] | None] = {}
_lock = threading.Lock()


def _jpeg(fh) -> tuple[int, int] | None:
    """(offset of the first SOS segment, bytes of XMP + IPTC before it), or None if this is no JPEG."""
    if fh.read(2) != b"\xff\xd8":
        return None
    meta = 0
    while True:
        b = fh.read(1)
        while b and b != b"\xff":                 # tolerate junk between segments
            b = fh.read(1)
        while b == b"\xff":                       # fill bytes
            b = fh.read(1)
        if not b:
            return None
        marker = b[0]
        if marker == 0xDA:                        # start of scan: picture data from here on
            return fh.tell() - 2, meta
        if marker in (0x01, *range(0xD0, 0xD8)):  # markers without a length
            continue
        n = int.from_bytes(fh.read(2), "big")
        if n < 2:
            return None
        if marker == 0xE1:
            head = fh.read(min(n - 2, 40))
            if head.startswith(_XMP):
                meta += n
            fh.seek(n - 2 - len(head), 1)
        else:
            if marker == 0xED:                    # APP13: Photoshop / IPTC
                meta += n
            fh.seek(n - 2, 1)


def scan(path: str) -> tuple[str, int] | None:
    """(hash of the picture data, metadata score) or None if unreadable. Score = XMP/IPTC bytes + sidecars."""
    try:
        with open(path, "rb") as fh:
            j = _jpeg(fh)
            start, meta = (0, 0) if j is None else j
            fh.seek(start)
            h = hashlib.blake2b(digest_size=16)
            while chunk := fh.read(_CHUNK):
                h.update(chunk)
        meta += sum(os.path.getsize(s) for s in sidecars(path))
        return ("j" if j else "f") + h.hexdigest(), meta
    except OSError:
        return None


def scan_cached(path: str, size: int, mtime: float) -> tuple[str, int] | None:
    k = (path, size, mtime)
    with _lock:
        if k in _cache:
            return _cache[k]
    r = scan(path)
    with _lock:
        _cache[k] = r
    return r


def _folder(p: str) -> str:
    return os.path.dirname(p)


def _key(folder: str) -> str:
    return os.path.normcase(os.path.normpath(folder))


@dataclass
class Merge:
    keep: str                     # folder that keeps the pictures
    other: str                    # folder whose copies go
    n_keep: int                   # images in each folder (scanned)
    n_other: int
    # (file in keep, file in other, metadata score of each); more metadata in the other file -> it moves over
    pairs: list[tuple[str, str, int, int]] = field(default_factory=list)
    album: str = ""               # the folder made of copies (the suggested keeper; stays when flipped)

    @property
    def count(self) -> int:
        return len({o for _, o, _, _ in self.pairs})

    @property
    def swaps(self) -> int:
        return len(self.plan(check=False)[1])

    def flipped(self) -> "Merge":
        return Merge(self.other, self.keep, self.n_other, self.n_keep,
                     [(o, k, mo, mk) for k, o, mk, mo in self.pairs], self.album)

    def plan(self, check: bool = True) -> tuple[list[str], list[tuple[str, str]]]:
        """(files to remove, renames (src, dst) done after removing). Pairs whose files are gone are skipped.
        Each file in the kept folder is replaced at most once; every copy in the other folder goes."""
        remove, renames, gone, replaced = [], [], set(), set()
        for k, o, mk, mo in sorted(self.pairs, key=lambda t: -t[3]):    # richest copy first
            if o in gone or (check and not (os.path.exists(k) and os.path.exists(o))):
                continue
            gone.add(o)
            if mo > mk and k not in replaced:
                replaced.add(k)
                remove.append(k)
                renames.append((o, k))
            else:
                remove.append(o)
        return remove, renames


def find(groups, paths: list[str], skip: list[str] = (), stop: threading.Event | None = None,
         min_copies: int = MIN_COPIES) -> list[Merge]:
    """Folder pairs sharing at least min_copies exact copies, most copies first."""
    total = defaultdict(int)
    names = {}
    for p in paths:
        total[_key(_folder(p))] += 1
        names.setdefault(_key(_folder(p)), _folder(p))
    skip_k = {_key(s) for s in skip}
    # only groups with pictures of equal size in different folders can hold copies
    todo = []
    for g in groups:
        by_dim = defaultdict(list)
        for p in g.photos:
            by_dim[(p.width, p.height)].append(p)
        for ps in by_dim.values():
            if len({_key(_folder(p.path)) for p in ps}) > 1:
                todo.append(ps)
    sets: list[list[tuple[str, int]]] = []
    for ps in todo:
        if stop is not None and stop.is_set():
            return []
        by_hash = defaultdict(list)
        for p in ps:
            r = scan_cached(p.path, p.size, p.mtime)
            if r is not None:
                by_hash[r[0]].append((p.path, r[1]))
        sets += [s for s in by_hash.values() if len(s) > 1]
    pairs: dict[tuple[str, str], list[tuple[str, str, int, int]]] = defaultdict(list)
    for s in sets:
        for i, (a, ma) in enumerate(s):
            for b, mb in s[i + 1:]:
                fa, fb = _key(_folder(a)), _key(_folder(b))
                if fa == fb or fa in skip_k or fb in skip_k:
                    continue
                if fa > fb:
                    (a, ma, fa), (b, mb, fb) = (b, mb, fb), (a, ma, fa)
                pairs[(fa, fb)].append((a, b, ma, mb))
    out = []
    for (fa, fb), ps in pairs.items():
        if len(ps) < min_copies:
            continue
        m = Merge(names.get(fa, _folder(ps[0][0])), names.get(fb, _folder(ps[0][1])),
                  total.get(fa, 0), total.get(fb, 0), ps)
        share_a = m.count / max(1, m.n_keep)
        share_b = m.count / max(1, m.n_other)
        if share_b > share_a or (share_b == share_a and fb.count(os.sep) > fa.count(os.sep)):
            m = m.flipped()
        m.album = m.keep
        out.append(m)
    out.sort(key=lambda m: -m.count)
    return out


def rename_with_sidecars(src: str, dst: str) -> list[tuple[str, str]]:
    """Moves src to dst inside the archive, XMP sidecars along (a.jpg.xmp -> b.jpg.xmp, a.xmp -> b.xmp)."""
    import shutil
    if os.path.exists(dst):
        raise FileExistsError(dst)
    done = []
    s_stem, d_stem = os.path.splitext(src)[0], os.path.splitext(dst)[0]
    for side in sidecars(src):
        if os.path.normcase(side).startswith(os.path.normcase(src)):
            to = dst + side[len(src):]
        else:
            to = d_stem + side[len(s_stem):]
        if os.path.exists(to):
            continue
        shutil.move(side, to)
        done.append((side, to))
    shutil.move(src, dst)
    done.insert(0, (src, dst))
    return done
