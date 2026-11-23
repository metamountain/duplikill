"""Bug reports and local statistics - nothing is ever sent by itself.

A crash writes crash.log (paths anonymised). The user can open a prefilled GitHub issue
(F8), read and edit it in the browser, and submit it there - or not. Statistics only live in
stats.json on this computer; "share" also just opens a prefilled issue.
"""
from __future__ import annotations

import json
import os
import platform
import re
import sys
import time
import traceback
from urllib.parse import urlencode

from .index import data_dir

REPO_URL = "https://github.com/metamountain/duplikill"
MAX_BODY = 5000                     # GitHub's prefilled-issue URLs get unreliable above ~8 KB

_START = r"""(?:[A-Za-z]:|\\\\[^\\/\s'"]+)[\\/]"""
_QUOTED = re.compile(r"""(?<=['"])""" + _START + r"""[^'"\n]*(?=['"])""")   # quoted paths may contain spaces
_PATH = re.compile(_START + r"""[^\s'"<>|:*?]*""")


def anonymise(text: str) -> str:
    """Every Windows path -> <PATH>; Duplikill's own source files keep their name (duplikill/app.py)."""
    def one(m: re.Match) -> str:
        p = m.group(0).replace("\\", "/")
        k = p.lower().rfind("/duplikill/")
        if k >= 0 and p.lower().endswith(".py"):
            return "duplikill/" + p[k + len("/duplikill/"):]
        ext = os.path.splitext(p)[1]
        return "<PATH>" + (ext if 1 < len(ext) <= 5 else "")
    return _PATH.sub(one, _QUOTED.sub(one, text))


def crash_path() -> str:
    return os.path.join(data_dir(), "crash.log")


def write_crash(exc_type, exc, tb) -> str:
    text = anonymise("".join(traceback.format_exception(exc_type, exc, tb)))
    with open(crash_path(), "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


def last_crash() -> str:
    try:
        with open(crash_path(), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def clear_crash():
    try:
        os.remove(crash_path())
    except OSError:
        pass


def system_line(version: str) -> str:
    return f"Duplikill {version} · {platform.system()} {platform.release()} ({platform.version()}) · " \
           f"Python {platform.python_version()} · {'exe' if getattr(sys, 'frozen', False) else 'source'}"


def bug_url(version: str, crash: str = "") -> str:
    body = ("**What happened / what did you expect?**\n\n\n\n"
            "**Steps to reproduce**\n1. \n\n"
            f"**System**\n{system_line(version)}\n")
    if crash:
        tail = crash[-(MAX_BODY - len(body) - 40):]
        body += f"\n**Error (paths removed)**\n```\n{tail}\n```\n"
    title = "Bug: " + (crash.strip().splitlines()[-1][:80] if crash.strip() else "")
    return f"{REPO_URL}/issues/new?" + urlencode({"title": title, "body": body, "labels": "bug"})


# ---------------------------------------------------------------- statistics (local only)

def stats_path() -> str:
    return os.path.join(data_dir(), "stats.json")


def load_stats() -> dict:
    s = {"since": time.strftime("%Y-%m-%d"), "images": 0, "bytes": 0, "groups": 0, "scans": 0,
         "scanned_images": 0, "by_action": {}}
    try:
        with open(stats_path(), encoding="utf-8") as fh:
            s.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return s


def save_stats(s: dict):
    tmp = stats_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(s, fh, indent=1)
    os.replace(tmp, stats_path())


def count_removed(s: dict, n: int, nbytes: int, action: str, group_done: bool):
    s["images"] += n
    s["bytes"] += nbytes
    s["groups"] += int(group_done)
    s["by_action"][action] = s["by_action"].get(action, 0) + n


def count_undo(s: dict, n: int, nbytes: int, action: str, group_done: bool):
    count_removed(s, -n, -nbytes, action, False)
    s["groups"] -= int(group_done)


def stats_url(version: str, s: dict) -> str:
    gb = s["bytes"] / 1e9
    body = ("Numbers from my Duplikill (nothing else attached, edit as you like):\n\n"
            f"- since: {s['since']}\n- scans: {s['scans']} ({s['scanned_images']} images scanned)\n"
            f"- removed: {s['images']} duplicates in {s['groups']} groups, {gb:.2f} GB freed\n"
            f"- by: {', '.join(f'{k} {v}' for k, v in sorted(s['by_action'].items())) or '-'}\n"
            f"- {system_line(version)}\n")
    return f"{REPO_URL}/issues/new?" + urlencode({"title": f"[stats] {s['images']} duplicates, {gb:.1f} GB freed",
                                                 "body": body, "labels": "stats"})
