"""Moving marked photos out of the archive, and undoing it. Nothing is ever deleted."""
from __future__ import annotations

import json
import os
import shutil
import time

from .index import data_dir

def sidecars(path: str) -> list[str]:
    stem, _ = os.path.splitext(path)
    out = []
    seen = set()
    for c in (path + ".xmp", stem + ".xmp", path + ".XMP", stem + ".XMP"):
        k = os.path.normcase(c)
        if k not in seen and os.path.exists(c):
            seen.add(k)
            out.append(c)
    return out


AUTO_FOLDER = "Duplikill"


def auto_root(path: str) -> str:
    """The automatic target: a Duplikill folder on the same drive (moving there is instant, no copy)."""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    return os.path.join(drive + os.sep, AUTO_FOLDER)


def target_for(path: str, target_root: str, mode: str = "", scan_root: str = "") -> str:
    """Keeps the whole place of the file.
    target_root empty (automatic): D:\\Pictures\\2019\\a.jpg -> D:\\Duplikill\\Pictures\\2019\\a.jpg
    target_root set:               D:\\Pictures\\2019\\a.jpg -> <target_root>\\D\\Pictures\\2019\\a.jpg"""
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    if mode == "folder" and scan_root:          # <scanned folder>\Duplikill\<path below it>
        return os.path.join(scan_root, AUTO_FOLDER, os.path.relpath(os.path.abspath(path), scan_root))
    if mode == "central" or not target_root:
        return os.path.join(auto_root(path), rest.lstrip("\\/"))
    return os.path.join(target_root, drive.rstrip(":\\/") or "root", rest.lstrip("\\/"))


class Mover:
    def __init__(self, target_root: str = "", mode: str = "", scan_root: str = ""):
        self.target_root, self.mode, self.scan_root = target_root, mode, scan_root
        self.journal = os.path.join(data_dir(), "journal.jsonl")

    def move(self, paths: list[str], group_key: str) -> list[tuple[str, str]]:
        moved = []
        for p in paths:
            for src in [p, *sidecars(p)]:
                dst = target_for(src, self.target_root, self.mode, self.scan_root)
                if os.path.exists(dst):
                    base, ext = os.path.splitext(dst)
                    dst = f"{base}_{int(time.time())}{ext}"
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                moved.append((src, dst))
        with open(self.journal, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.time(), "group": group_key, "moved": moved}, ensure_ascii=False) + "\n")
        return moved

    def undo_last(self) -> dict | None:
        """Moves the last batch back; returns the journal entry or None."""
        if not os.path.exists(self.journal):
            return None
        with open(self.journal, encoding="utf-8") as fh:
            lines = fh.readlines()
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            return None
        entry = json.loads(lines[-1])
        for src, dst in reversed(entry["moved"]):
            if os.path.exists(dst) and not os.path.exists(src):
                os.makedirs(os.path.dirname(src), exist_ok=True)
                shutil.move(dst, src)
        with open(self.journal, "w", encoding="utf-8") as fh:
            fh.writelines(lines[:-1])
        return entry

    def stats(self) -> tuple[int, int]:
        """(files moved, bytes moved) over the whole journal."""
        n = b = 0
        if os.path.exists(self.journal):
            for line in open(self.journal, encoding="utf-8"):
                if line.strip():
                    for src, dst in json.loads(line)["moved"]:
                        n += 1
                        try:
                            b += os.path.getsize(dst)
                        except OSError:
                            pass
        return n, b
