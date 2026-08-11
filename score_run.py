#!/usr/bin/env python3
"""score_run.py - score a set of decoded DBCs against manufacturer ground truth.

Makes "did the second attempt do better?" an objective question rather than a
judgement call. Scores any directory of produced DBCs against a directory of
reference DBCs, on three axes:

  CORRECTNESS  of the signals produced, how many match a reference signal
               bit-for-bit (EXACT), overlap it partially (PARTIAL), or match
               nothing on that ID (UNMATCHED). Partial matters: a field that
               shares the dominant byte can decode to nearly the right values
               while having the wrong geometry, which no correlation test catches.
  COVERAGE     what fraction of the bus's ACTIVE bits the produced set explains,
               and how many IDs it touches.
  NOVELTY      signals on IDs absent from the reference set. These cannot be
               scored - the reference may simply be incomplete - so they are
               reported separately and never counted as either right or wrong.

Usage:
    python score_run.py --trace temp-output/trace_r7.csv \
        --produced decoding-output/run2 --truth /path/to/manufacturer/dbc \
        [--label "run 2"] [--json out.json]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".claude/skills/cansub-reverse-engineering/scripts"))
import common  # noqa: E402
from coverage import signal_bits  # noqa: E402


def load_dbcs(path: str) -> dict:
    """{frame_id: [(signal_name, set(bits), signal)]} from every .dbc under path."""
    import cantools
    out: dict[int, list] = {}
    files = sorted(glob.glob(os.path.join(path, "**", "*.dbc"), recursive=True))
    loaded = 0
    for p in files:
        try:
            db = cantools.database.load_file(p)
        except Exception:
            continue
        loaded += 1
        for m in db.messages:
            for s in m.signals:
                out.setdefault(m.frame_id, []).append(
                    (s.name, set(signal_bits(s, m.length)), s))
    return out, loaded, len(files)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", required=True)
    ap.add_argument("--produced", required=True, help="dir of DBCs to score")
    ap.add_argument("--truth", required=True, help="dir of reference DBCs")
    ap.add_argument("--label", default="run")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    prod, p_ok, p_all = load_dbcs(args.produced)
    truth, t_ok, t_all = load_dbcs(args.truth)
    df = common.load_trace(args.trace)
    groups = common.group_by_id(df)

    active = {}
    tot_active = 0
    for cid, g in groups.items():
        rates = common._bit_flip_rates_le(g.le_int, g.length * 8)
        a = set(np.where(rates > 1e-6)[0].tolist())
        active[cid] = a
        tot_active += len(a)

    exact, partial, unmatched, novel = [], [], [], []
    for cid, sigs in sorted(prod.items()):
        for name, bits, s in sigs:
            if cid not in truth:
                novel.append((cid, name, len(bits)))
                continue
            best = None
            for tname, tbits, ts in truth[cid]:
                inter = len(bits & tbits)
                if inter == 0:
                    continue
                if best is None or inter > best[0]:
                    best = (inter, tname, tbits, ts)
            if best is None:
                unmatched.append((cid, name, len(bits)))
            elif best[2] == bits:
                exact.append((cid, name, best[1], len(bits)))
            else:
                partial.append((cid, name, best[1], best[0], len(bits), len(best[2])))

    cov = 0
    ids_touched = set()
    for cid, sigs in prod.items():
        b = set()
        for _, bits, _ in sigs:
            b |= bits
        hit = b & active.get(cid, set())
        cov += len(hit)
        if hit:
            ids_touched.add(cid)

    n_sigs = sum(len(v) for v in prod.values())
    scoreable = len(exact) + len(partial) + len(unmatched)
    print("=" * 76)
    print(f"SCORE — {args.label}")
    print("=" * 76)
    print(f"  produced DBCs      : {p_ok}/{p_all} loaded from {args.produced}")
    print(f"  reference DBCs     : {t_ok}/{t_all} loaded, {len(truth)} IDs")
    print(f"  signals produced   : {n_sigs}")
    print()
    print(f"  EXACT match        : {len(exact)}")
    print(f"  PARTIAL (overlaps) : {len(partial)}")
    print(f"  UNMATCHED on a known ID : {len(unmatched)}")
    print(f"  NOVEL (ID absent from reference, unscoreable) : {len(novel)}")
    if scoreable:
        print(f"  precision on scoreable signals : {100*len(exact)/scoreable:.0f}% exact, "
              f"{100*(len(exact)+len(partial))/scoreable:.0f}% at least overlapping")
    print()
    print(f"  ACTIVE bits explained : {cov:,} / {tot_active:,} = "
          f"{100*cov/max(tot_active,1):.2f}%")
    print(f"  IDs touched           : {len(ids_touched)} / {len(groups)}")

    if exact:
        print("\n  EXACT:")
        for cid, name, tname, n in exact:
            print(f"    0x{cid:<4X} {name:<24} == {tname} ({n} bits)")
    if partial:
        print("\n  PARTIAL (right ID, wrong geometry — the dangerous case):")
        for cid, name, tname, inter, n, tn in partial:
            print(f"    0x{cid:<4X} {name:<24} ~ {tname:<22} "
                  f"{inter}/{n} bits shared (ref is {tn} bits)")
    if unmatched:
        print("\n  UNMATCHED on an ID the reference knows:")
        for cid, name, n in unmatched:
            print(f"    0x{cid:<4X} {name:<24} ({n} bits)")
    if novel:
        print("\n  NOVEL (not in reference — could be right or wrong):")
        for cid, name, n in novel:
            print(f"    0x{cid:<4X} {name:<24} ({n} bits)")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"label": args.label, "signals": n_sigs,
                       "exact": len(exact), "partial": len(partial),
                       "unmatched": len(unmatched), "novel": len(novel),
                       "active_bits": cov, "active_total": tot_active,
                       "ids_touched": len(ids_touched),
                       "exact_list": [(f"{c:X}", n, t) for c, n, t, _ in exact],
                       "partial_list": [(f"{c:X}", n, t) for c, n, t, *_ in partial]},
                      f, indent=2)
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
