"""Headless inspection and debugging tool for Duplikill.

Runs the exact same pipeline as the GUI (using saved session / Czkawka cache / signatures.sqlite)
without opening a window, and prints a concise report of images, merge offers (cross-folder and
same-folder), best-of folder candidates, and duplicate groups.

Usage:
    .venv\\Scripts\\python.exe -I tools\\inspect_folder.py "C:\\Pictures\\2023" [--max-diff 12] [--scan]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from duplikill import app, bestof, copies, czkawka, session, verify
from duplikill.actions import auto_root
from duplikill.index import human


def inspect_folder(folder: str, max_diff: int | None = None, force_scan: bool = False, sample: int = 5) -> dict:
    t0 = time.time()
    folder = os.path.normpath(os.path.abspath(folder))
    settings = app.load_settings()
    md = int(max_diff if max_diff is not None else settings.get("max_diff", 12))
    hs = int(settings.get("hash_size", 8))

    drive = os.path.splitdrive(folder)[0] + os.sep
    never = [
        auto_root(folder),
        os.path.join(folder, "Duplikill-Bin"),
        os.path.join(folder, "Duplikill"),
        os.path.join(drive, "Duplikill"),
    ] + ([settings["target"]] if settings.get("target") else []) + list(settings.get("excluded", []))
    excluded = [d for d in never if os.path.isdir(d)]

    opts = czkawka.Options(max_diff=md, hash_size=hs, excluded=excluded)
    paths = czkawka.list_images(folder, opts.excluded)
    fp = session.fingerprint(paths)
    saved = None if force_scan else session.load(folder, md, hs)

    source = "session-cache"
    if saved is not None and saved.get("fingerprint") == fp:
        groups = saved["groups"]
    else:
        source = "live-scan"
        candidates = czkawka.search(folder, opts)
        groups = verify.refine(candidates, verify.SigCache())

    merges = copies.find(groups, paths, skip=excluded)
    in_merge = [f for m in merges if not m.same_folder for f in (m.keep, m.other)]
    bestofs = bestof.candidates(
        groups,
        paths,
        settings.get("excluded", []) + settings.get("bestof_kept", []) + in_merge,
    )
    elapsed = time.time() - t0
    dups = sum(len(g.photos) - 1 for g in groups)
    free_bytes = sum(sum(p.size for j, p in enumerate(g.photos) if j != g.best) for g in groups)

    print(f"=== DUPLIKILL HEADLESS INSPECT ({elapsed:.2f}s, {source}) ===")
    print(f"Root:       {folder}")
    print(f"Settings:   max_diff={md}, hash_size={hs}, excluded={len(excluded)}")
    print(f"Images:     {len(paths)} ({human(fp['bytes'])})")
    print(f"Groups:     {len(groups)} ({dups} duplicates, ~{human(free_bytes)} removable)")
    print()

    print(f"--- MERGE OFFERS ({len(merges)}) ---")
    for idx, m in enumerate(merges, 1):
        kind = "SAME-FOLDER" if m.same_folder else "CROSS-FOLDER"
        if m.same_folder:
            print(f"  [{idx}] {kind}: {m.keep}")
            print(f"      {m.count} exact copies out of {m.n_keep} images | swaps (tagged copy -> clean name): {m.swaps}")
        else:
            print(f"  [{idx}] {kind}: keep={m.keep} ({m.count}/{m.n_keep}) <- other={m.other} ({m.count}/{m.n_other}) | swaps: {m.swaps}")
        remove, renames = m.plan(check=False)
        print(f"      plan: remove {len(remove)} files, rename {len(renames)} files")
        for k, o, mk, mo in m.pairs[:sample]:
            tag_note = " [SWAP]" if mo > mk else ""
            print(f"        keep: {os.path.basename(k)} (meta={mk})  |  copy: {os.path.basename(o)} (meta={mo}){tag_note}")
    if not merges:
        print("  (none)")
    print()

    print(f"--- BEST-OF CANDIDATES ({len(bestofs)}) ---")
    for folder_b, hits, total in bestofs:
        print(f"  {folder_b}: {hits}/{total} ({100 * hits / max(1, total):.0f}%) also in other folders")
    if not bestofs:
        print("  (none)")

    return {"paths": paths, "groups": groups, "merges": merges, "bestofs": bestofs}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Headless Duplikill pipeline inspector.")
    ap.add_argument("folder", help="Folder to inspect")
    ap.add_argument("-s", "--max-diff", type=int, default=None, help="Strictness (0..17)")
    ap.add_argument("--scan", action="store_true", help="Ignore saved session and run Czkawka + verify.refine")
    ap.add_argument("--sample", type=int, default=5, help="Sample pairs to print per merge offer")
    args = ap.parse_args(argv)
    inspect_folder(args.folder, args.max_diff, args.scan, args.sample)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
