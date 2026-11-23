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


AUTO_FOLDER = "Duplikill-Bin"
BIN_NAMES = {"duplikill-bin", "duplikill"}     # never scanned ("Duplikill" = name before 0.2.0)


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
        total_bytes = 0
        for p in paths:
            for src in [p, *sidecars(p)]:
                dst = target_for(src, self.target_root, self.mode, self.scan_root)
                if os.path.exists(dst):
                    base, ext = os.path.splitext(dst)
                    stamp = int(time.time())
                    dst = f"{base}_{stamp}{ext}"
                    k = 1
                    while os.path.exists(dst):
                        dst = f"{base}_{stamp}_{k}{ext}"
                        k += 1
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                moved.append((src, dst))
                try:
                    total_bytes += os.path.getsize(dst)
                except OSError:
                    pass
        with open(self.journal, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.time(), "group": group_key, "moved": moved, "bytes": total_bytes},
                                ensure_ascii=False) + "\n")
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

    def _entries(self) -> list[dict]:
        if not os.path.exists(self.journal):
            return []
        with open(self.journal, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def bin_items(self) -> list[dict]:
        """Images (no sidecars) that were moved and are still in the bin, newest first:
        {"src", "dst", "at", "group", "size"}. Deleted by hand in Explorer -> simply not listed."""
        out = []
        for e in self._entries():
            for src, dst in e["moved"]:
                if src.lower().endswith(".xmp"):
                    continue
                try:
                    size = os.path.getsize(dst)
                except OSError:
                    continue
                out.append({"src": src, "dst": dst, "at": e.get("at", 0), "group": e.get("group", ""), "size": size})
        out.sort(key=lambda d: -d["at"])
        return out

    def restore(self, srcs: list[str]) -> list[str]:
        """Moves these images (and their XMP sidecars) back to where they were; returns the restored paths."""
        want = {os.path.normcase(s) for s in srcs}
        stems = {os.path.splitext(s)[0] for s in want}
        done, keep = [], []
        for e in self._entries():
            rest = []
            for src, dst in e["moved"]:
                k = os.path.normcase(src)
                side = k.endswith(".xmp") and (k[:-4] in want or os.path.splitext(k[:-4])[0] in stems
                                               or k[:-4] in stems)
                if (k in want or side) and os.path.exists(dst) and not os.path.exists(src):
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    shutil.move(dst, src)
                    if k in want:
                        done.append(src)
                else:
                    rest.append([src, dst])
            if rest:
                e["moved"] = rest
                keep.append(e)
        with open(self.journal, "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in keep)
        return done

    def stats(self) -> tuple[int, int]:
        """(files moved, bytes moved) over the whole journal."""
        n = b = 0
        if os.path.exists(self.journal):
            with open(self.journal, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    moved = entry["moved"]
                    n += len(moved)
                    if "bytes" in entry:
                        b += int(entry["bytes"])
                    else:
                        for _, dst in moved:
                            try:
                                b += os.path.getsize(dst)
                            except OSError:
                                pass
        return n, b
