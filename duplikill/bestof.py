"""Best-of folders: a folder whose images are mostly copies of images in other folders.

The user collects favourites this way on purpose, so those duplicates are fine. After a scan
candidates() lists such folders; the app asks once per folder whether to exclude it.
"""
from __future__ import annotations

import os
from collections import Counter

MIN_IMAGES = 5          # smaller folders are not worth a question
MIN_SHARE = 0.5         # at least half of the folder's images also exist elsewhere


def _key(folder: str) -> str:
    return os.path.normcase(os.path.normpath(folder))


def candidates(groups, paths: list[str], skip: list[str] = (),
               min_images: int = MIN_IMAGES, min_share: float = MIN_SHARE) -> list[tuple[str, int, int]]:
    """[(folder, images also found in another folder, images in the folder)], highest share first."""
    total = Counter(_key(os.path.dirname(p)) for p in paths)
    elsewhere: dict[str, set[str]] = {}
    for g in groups:
        folders = {_key(os.path.dirname(p.path)) for p in g.photos}
        if len(folders) < 2:
            continue
        for p in g.photos:
            elsewhere.setdefault(_key(os.path.dirname(p.path)), set()).add(_key(p.path))
    names = {_key(os.path.dirname(p.path)): os.path.dirname(p.path) for g in groups for p in g.photos}
    skip_k = {_key(s) for s in skip}
    out = []
    for k, hits in elsewhere.items():
        n = total.get(k, 0)
        if k in skip_k or n < min_images or len(hits) < min_share * n:
            continue
        out.append((names[k], len(hits), n))
    out.sort(key=lambda t: (-t[1] / t[2], -t[2]))
    return out
