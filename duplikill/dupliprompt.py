"""Headless CLI diagnostic and pipeline prompt tool (`dupliprompt`).

Provides instant headless inspection of Duplikill's detection pipeline (Czkawka 8x8 hash + EXIF timestamp
series + 24x24 structure/shift/high-pass-detail/colour-chroma verification + exact folder merges + best-of
detection), as well as deep pair/series diagnostics (`--query`) showing exact EXIF timestamps, time deltas,
unshifted and shift-tolerant structure correlation, 5x5 high-pass detail correlation, raw & chroma colour
distance, sharpness, and why any image pair was grouped or rejected.

Usage examples:
    .venv\\Scripts\\python.exe -I -m duplikill.dupliprompt "C:\\Pictures\\2023" -q 0076 0077 0078 0079
    .venv\\Scripts\\python.exe -I -m duplikill.dupliprompt "C:\\Pictures\\2023" -q 0030 0031 0033 0034
    .venv\\Scripts\\python.exe -I -m duplikill.dupliprompt "C:\\Pictures\\2023" --scan --groups
"""
from __future__ import annotations

import argparse
from datetime import datetime
import os
import time

import numpy as np

from . import app, bestof, copies, czkawka, session, verify
from .actions import auto_root
from .index import Photo, human


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "no-EXIF"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return str(ts)


def _match_query(path: str, queries: list[str]) -> bool:
    name = os.path.basename(path).lower()
    full = path.lower()
    return any(q.lower() in name or q.lower() in full for q in queries)


def pair_metrics(a: Photo, b: Photo, sigs: dict[str, verify.Sig | None], max_diff: int | None = None) -> dict:
    """Detailed comparison metrics between two photos for headless diagnostics."""
    sa, sb = sigs.get(a.path), sigs.get(b.path)
    if sa is None or sb is None:
        return {"valid": False}
    th = verify.thresholds_for(max_diff)
    raw_corr = float((sa.grey * sb.grey).mean())
    ta = np.array([np.nan if sa.taken is None else sa.taken], dtype=np.float64)
    tb = np.array([np.nan if sb.taken is None else sb.taken], dtype=np.float64)
    ga, gb = sa.grey[None, :], sb.grey[None, :]
    shift_corr = float(verify._corr_matrix(ga, gb, ta, tb, th.max_shift, th.series_gap)[0, 0])
    hpa, hpb = verify._highpass_greys(ga), verify._highpass_greys(gb)
    hp_corr = float(verify._corr_matrix(hpa, hpb, ta, tb, th.max_shift, th.series_gap)[0, 0])
    ca, cb = sa.colour[None, :], sb.colour[None, :]
    raw_cdist = float(np.abs(sa.colour - sb.colour).mean())
    eff_cdist = float(verify._effective_cdist(ca, cb, np.array([[shift_corr]]), np.array([[hp_corr]]), ta, tb)[0, 0])
    dt = abs(sa.taken - sb.taken) if (sa.taken is not None and sb.taken is not None) else None
    passed = verify.similar(sa, sb, max_diff=max_diff)
    reasons = []
    if passed:
        if shift_corr >= verify.COPY_CORR and eff_cdist <= th.max_cdist:
            reasons.append("COPY_CORR>=0.90")
        else:
            reasons.append(f"corr={shift_corr:.3f}>={th.min_corr}")
            reasons.append(f"hp={hp_corr:.3f}>={th.min_hp_corr}")
            reasons.append(f"cd={eff_cdist:.3f}<={th.max_cdist}")
        if eff_cdist < raw_cdist - 1e-4:
            reasons.append(f"burst-chroma({raw_cdist:.3f}->{eff_cdist:.3f})")
    else:
        if shift_corr < th.min_corr:
            reasons.append(f"corr {shift_corr:.3f} < {th.min_corr}")
        if hp_corr < th.min_hp_corr and shift_corr < 0.96:
            reasons.append(f"hp_detail {hp_corr:.3f} < {th.min_hp_corr} (bg-only)")
        if eff_cdist > th.max_cdist:
            reasons.append(f"cdist {eff_cdist:.3f} > {th.max_cdist}")
        if dt is not None and dt > verify.SAME_DAY and shift_corr < verify.COPY_CORR:
            reasons.append(f"dt {dt / 3600:.1f}h > 24h")
        if dt is None and shift_corr < max(0.75, th.min_corr):
            reasons.append(f"no-EXIF & corr {shift_corr:.3f} < {max(0.75, th.min_corr)}")
    return {
        "valid": True,
        "raw_corr": raw_corr,
        "shift_corr": shift_corr,
        "hp_corr": hp_corr,
        "raw_cdist": raw_cdist,
        "cdist": eff_cdist,
        "dt": dt,
        "passed": passed,
        "reason": ", ".join(reasons),
    }


def run_prompt(
    folder: str,
    max_diff: int | None = None,
    force_scan: bool = False,
    queries: list[str] | None = None,
    show_groups: bool = False,
    sample: int = 5,
) -> dict:
    t0 = time.time()
    folder = os.path.normpath(os.path.abspath(folder))
    settings = app.load_settings()
    md = int(max_diff if max_diff is not None else settings.get("max_diff", 12))
    hs = int(settings.get("hash_size", 8))
    th = verify.thresholds_for(md)

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
    sig_cache = verify.SigCache()

    source = "session-cache"
    cz_groups: list = []
    if saved is not None and saved.get("fingerprint") == fp:
        groups = saved["groups"]
    else:
        source = "live-scan"
        cz_groups = czkawka.search(folder, opts)
        groups = verify.refine(cz_groups, sig_cache, paths=paths, max_diff=md)

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

    print(f"=== DUPLIPROMPT HEADLESS ({elapsed:.2f}s, {source}) ===")
    print(f"Root:       {folder}")
    print(
        f"Settings:   max_diff={md} (min_corr={th.min_corr}, min_hp={th.min_hp_corr}, "
        f"max_cdist={th.max_cdist}, gap={th.series_gap:.0f}s, shift=+-{th.max_shift})"
    )
    print(f"Images:     {len(paths)} ({human(fp['bytes'])})")
    if cz_groups:
        cz_photos = sum(len(g.photos) for g in cz_groups)
        print(f"Czkawka:    {len(cz_groups)} raw groups ({cz_photos} images)")
    print(f"Verified:   {len(groups)} groups ({dups} duplicates, ~{human(free_bytes)} removable)")
    print(f"Merges:     {len(merges)} offer(s) | Best-of candidates: {len(bestofs)}")

    if show_groups:
        print(f"\n--- ALL VERIFIED GROUPS ({len(groups)}) ---")
        for gi, g in enumerate(groups, 1):
            best_p = g.photos[g.best]
            names = [
                f"*{os.path.basename(p.path)}*" if j == g.best else os.path.basename(p.path)
                for j, p in enumerate(g.photos)
            ]
            t_str = _fmt_ts(best_p.taken)
            print(f"  [{gi:3d}] ({len(g.photos)}) {t_str}  {', '.join(names)}")

    if queries:
        matched_paths = sorted(p for p in paths if _match_query(p, queries))
        print(f"\n--- QUERY DIAGNOSTICS ({', '.join(queries)}) -> {len(matched_paths)} file(s) ---")
        photos = []
        for p in matched_paths:
            try:
                st = os.stat(p)
                photos.append(Photo(p, st.st_size, st.st_mtime))
            except OSError:
                pass
        sigs = sig_cache.get_many(photos)
        verify._apply_sigs(photos, sigs)
        for p in photos:
            s = sigs.get(p.path)
            ex_str = _fmt_ts(p.taken)
            mt_str = _fmt_ts(p.mtime)
            sh_str = f"{s.sharp:.1f}" if s else "n/a"
            print(f"  FILE: {p.path}")
            print(f"        size={human(p.size)}  EXIF_taken={ex_str}  mtime={mt_str}  sharp={sh_str}")

        if not cz_groups and matched_paths:
            cz_groups = czkawka.search(folder, opts)
        cz_hits = [
            g for g in cz_groups if any(_match_query(p.path, queries) for p in g.photos)
        ]
        print(f"\n  Czkawka raw groups matching query (max_diff={md}): {len(cz_hits)}")
        for g in cz_hits:
            items = [f"{os.path.basename(p.path)}(diff={p.diff})" for p in g.photos]
            print(f"    czkawka: {', '.join(items)}")

        v_hits = [
            (gi, g) for gi, g in enumerate(groups, 1) if any(_match_query(p.path, queries) for p in g.photos)
        ]
        print(f"\n  Verified groups matching query: {len(v_hits)}")
        for gi, g in v_hits:
            items = [
                f"*{os.path.basename(p.path)}*(BEST,sharp={p.sharp:.0f})"
                if j == g.best
                else f"{os.path.basename(p.path)}(sharp={p.sharp:.0f})"
                for j, p in enumerate(g.photos)
            ]
            print(f"    group #{gi}: {', '.join(items)}")

        if len(photos) >= 2:
            print(f"\n  Pairwise verification matrix ({len(photos)} queried files):")
            for i, a in enumerate(photos):
                for b in photos[i + 1:]:
                    if a.folder != b.folder and len(photos) > 12:
                        continue
                    m = pair_metrics(a, b, sigs, max_diff=md)
                    if not m.get("valid"):
                        continue
                    dt_s = f"{m['dt']:.0f}s" if m["dt"] is not None else "n/a"
                    status = "MATCH" if m["passed"] else "DROP "
                    print(
                        f"    [{status}] {os.path.basename(a.path)} <-> {os.path.basename(b.path)}: "
                        f"dt={dt_s:>5s} | raw_c={m['raw_corr']:.3f} | shift_c={m['shift_corr']:.3f} "
                        f"| hp_c={m['hp_corr']:.3f} | cd={m['cdist']:.3f} | {m['reason']}"
                    )

    return {"paths": paths, "groups": groups, "merges": merges, "bestofs": bestofs}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="dupliprompt",
        description="Headless Duplikill CLI & diagnostic prompt (timestamps, Czkawka, shift & detail verify).",
    )
    ap.add_argument("folder", help="Root folder to scan or inspect")
    ap.add_argument("-s", "--max-diff", type=int, default=None, help="Strictness (0..17, default from settings)")
    ap.add_argument("--scan", action="store_true", help="Ignore session cache and force live Czkawka + verify scan")
    ap.add_argument("-q", "--query", nargs="+", default=None, help="Filename/number substrings to diagnose")
    ap.add_argument("-g", "--groups", action="store_true", help="List all verified duplicate/series groups")
    ap.add_argument("--sample", type=int, default=5, help="Sample pairs to display")
    args = ap.parse_args(argv)
    run_prompt(
        folder=args.folder,
        max_diff=args.max_diff,
        force_scan=args.scan,
        queries=args.query,
        show_groups=args.groups,
        sample=args.sample,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
