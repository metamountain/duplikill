"""Updates from GitHub releases: check quietly at start, download Duplikill-Setup.exe on request, run it."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

from .index import data_dir

REPO = "metamountain/duplikill"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = "Duplikill-Setup.exe"
CHUNK = 256 * 1024
_UA = {"User-Agent": "Duplikill-updater"}


def parse_version(tag: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in tag.strip().lstrip("vV").split("."))
    except (ValueError, AttributeError):
        return ()


def check(current: str, timeout: float = 4.0) -> dict | None:
    """{"version", "url", "size"} when GitHub has a newer release with the installer, else None."""
    try:
        req = urllib.request.Request(API, headers={**_UA, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rel = json.load(r)
        tag = rel.get("tag_name", "")
        if parse_version(tag) <= parse_version(current):
            return None
        for a in rel.get("assets", []):
            if a.get("name") == ASSET:
                return {"version": tag.lstrip("vV"), "url": a["browser_download_url"], "size": a.get("size", 0)}
    except Exception:
        pass
    return None


def download(url: str, dest_dir: str, progress=None) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    final = os.path.join(dest_dir, ASSET)
    part = final + ".part"
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=30) as r, open(part, "wb") as fh:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            buf = r.read(CHUNK)
            if not buf:
                break
            fh.write(buf)
            done += len(buf)
            if progress:
                progress(done, total)
    os.replace(part, final)
    return final


def update_dir() -> str:
    return os.path.join(data_dir(), "update")


def run_installer(path: str) -> None:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    subprocess.Popen([path], close_fds=True, creationflags=flags)
