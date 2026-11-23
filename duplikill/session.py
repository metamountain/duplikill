"""Saved results per folder + strictness: reopening needs no scan while the folder is unchanged.

The fingerprint (count, bytes, sum of whole-second mtimes) is cheap to compute and can be adjusted
when Duplikill itself moves files away (or back with undo), so its own work does not force a rescan.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os

from .groups import Group
from .index import Photo, data_dir

VERSION = 3


def fingerprint(paths: list[str]) -> dict:
    n = b = m = 0
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        n, b, m = n + 1, b + st.st_size, m + int(st.st_mtime)
    return {"count": n, "bytes": b, "mtimes": m}


def adjust(fp: dict, photos, sign: int) -> dict:
    photos = list(photos)
    return {"count": fp["count"] + sign * len(photos),
            "bytes": fp["bytes"] + sign * sum(p.size for p in photos),
            "mtimes": fp["mtimes"] + sign * sum(int(p.mtime) for p in photos)}


def _file(root: str, max_diff: int, hash_size: int) -> str:
    key = f"{os.path.normcase(os.path.abspath(root))}|{max_diff}|{hash_size}"
    return os.path.join(data_dir(), "sessions", hashlib.blake2b(key.encode(), digest_size=10).hexdigest() + ".json")


def save(root: str, max_diff: int, hash_size: int, fp: dict, groups: list[Group],
         marks: dict[str, list[str]], done: list[str], position: int) -> None:
    path = _file(root, max_diff, hash_size)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {"version": VERSION, "root": root, "fingerprint": fp, "position": position,
            "groups": [{"kind": g.kind, "photos": [dataclasses.asdict(p) for p in g.photos],
                        "marked": marks.get(g.key, []), "done": g.key in done} for g in groups]}
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def load(root: str, max_diff: int, hash_size: int) -> dict | None:
    try:
        with open(_file(root, max_diff, hash_size), encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("version") != VERSION:
            return None
        saved = data["groups"]
        groups, marks, done, old_index = [], {}, set(), []
        for k, sg in enumerate(saved):
            photos = [Photo(**d) for d in sg["photos"] if os.path.exists(d["path"])]
            if len(photos) < 2:
                continue
            g = Group(sg["kind"], photos)
            want = set(sg.get("marked", []))
            m = {i for i, p in enumerate(g.photos) if p.path in want}       # review marks are indices
            if m and len(m) < len(g.photos):                                 # one image always stays
                marks[g.key] = m
            if sg.get("done"):
                done.add(g.key)
            groups.append(g)
            old_index.append(k)
        pos = int(data.get("position", 0))
        new_pos = old_index.index(pos) if pos in old_index else min(pos, len(groups) - 1)
        return {"fingerprint": data["fingerprint"], "position": max(0, new_pos), "groups": groups,
                "marks": marks, "done": done}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def forget(root: str, max_diff: int, hash_size: int) -> None:
    try:
        os.remove(_file(root, max_diff, hash_size))
    except OSError:
        pass
