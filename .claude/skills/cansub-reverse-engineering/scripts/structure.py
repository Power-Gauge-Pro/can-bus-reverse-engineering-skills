#!/usr/bin/env python3
"""structure.py - find counters, checksums, multiplexors and constants.

FORK ADDITION (not upstream).

These carry no physical signal, so they are not the point of reverse engineering -
but they are the single biggest source of FALSE POSITIVES. A rolling counter
correlates with any monotone reference, ties at the top of any stepped-hold
correlation, and passes naive "does it ramp smoothly" tests precisely because it
takes small even steps through its whole range. Identify them once, up front, and
every later search gets quieter.

`survey.py` already flags counter-ish and checksum-ish BYTES. This goes further in
the two ways that matter:

  * sub-byte fields - a counter occupying only part of a byte is invisible to a
    per-byte test, because the rest of the byte carries real data;
  * proof rather than suspicion - a counter must actually increment by a fixed
    step across consecutive frames, and a checksum must actually reproduce from
    the other bytes under a named algorithm.

Usage:
    python structure.py --trace temp-output/trace_r7.csv
    python structure.py --trace ... --json temp-output/structure.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

MAX_FRAMES = 30000          # per ID, for runtime
COUNTER_MIN_SCORE = 0.97
CKSUM_MIN_SCORE = 0.97


# ---------------------------------------------------------------- counters

def counter_fields(g, max_frames=MAX_FRAMES):
    """Fields that increment by a fixed step on consecutive frames."""
    n = min(g.n, max_frames)
    le = g.le_int[:n]
    t = g.t[:n]
    dt = np.diff(t)
    cyc = float(np.median(dt[dt > 0])) if np.any(dt > 0) else 0.0
    if cyc <= 0:
        return []
    # only pairs with no dropped frame in between
    ok = dt <= 1.6 * cyc
    if ok.sum() < 50:
        return []

    nbits = g.length * 8
    found = []
    for length in (2, 3, 4, 6, 8):
        mod = 1 << length
        for start in range(0, nbits - length + 1):
            v = common.extract_le(le, start, length).astype(np.int64)
            if np.ptp(v) == 0:
                continue
            d = np.diff(v) % mod
            d = d[ok]
            if len(d) < 50:
                continue
            steps, counts = np.unique(d, return_counts=True)
            k = counts.argmax()
            step, score = int(steps[k]), counts[k] / len(d)
            if step == 0 or score < COUNTER_MIN_SCORE:
                continue
            # must actually use its range, else a slow signal looks like a counter
            if len(np.unique(v)) < mod * 0.75:
                continue
            # TRIM to the minimal field. A 2-bit counter at bits 4-5 also reads as
            # a "3-bit counter with step 2" at bits 3-5 once a constant bit is
            # dragged in, which is how one physical counter shows up half a dozen
            # times per ID. Drop non-toggling bits at both ends and re-derive.
            # Trim on flip RATE, not "did it ever change": a neighbouring data bit
            # that flips a handful of times in 30k frames still leaves the wider
            # field scoring ~100% on a fixed step, so `ptp` would keep it. The real
            # counter bits flip on >=25% of frames (a 2-bit counter's MSB flips
            # every other frame); anything under 2% is a passenger.
            fr = np.array([float(np.mean(np.diff((v >> k) & 1) != 0))
                           for k in range(length)])
            nz = np.nonzero(fr > 0.02)[0]
            if len(nz) == 0:
                continue
            s2, l2 = start + int(nz[0]), int(nz[-1] - nz[0] + 1)
            v2 = common.extract_le(le, s2, l2).astype(np.int64)
            mod2 = 1 << l2
            d2 = (np.diff(v2) % mod2)[ok]
            st2, ct2 = np.unique(d2, return_counts=True)
            k2 = ct2.argmax()
            step2, score2 = int(st2[k2]), ct2[k2] / len(d2)
            if step2 == 0 or score2 < COUNTER_MIN_SCORE:
                continue
            if len(np.unique(v2)) < mod2:      # a counter visits every value
                continue
            found.append({"start": s2, "length": l2, "step": step2,
                          "score": float(score2), "mod": mod2})

    # one entry per physical counter
    uniq = {}
    for f in found:
        key = (f["start"], f["length"])
        if key not in uniq or f["score"] > uniq[key]["score"]:
            uniq[key] = f
    kept, used = [], set()
    for f in sorted(uniq.values(), key=lambda x: (-x["length"], -x["score"])):
        bits = set(range(f["start"], f["start"] + f["length"]))
        if bits & used:
            continue
        used |= bits
        kept.append(f)
    return sorted(kept, key=lambda x: x["start"])


# --------------------------------------------------------------- checksums

def _payload_matrix(g, n):
    """(n, length) uint8 matrix of payload bytes."""
    le = g.le_int[:n]
    L = g.length
    out = np.empty((len(le), L), dtype=np.int64)
    for b in range(L):
        out[:, b] = common.extract_le(le, 8 * b, 8)
    return out


def checksum_fields(g, max_frames=MAX_FRAMES):
    """Bytes reproducible from the other bytes under a named algorithm."""
    n = min(g.n, max_frames)
    if n < 100 or g.length < 2:
        return []
    P = _payload_matrix(g, n)
    L = g.length

    # Whole-frame relations FIRST. If XOR over every byte is constant, then for
    # ANY byte b we can write b = XOR(others) ^ C - so a per-byte test reports
    # every byte as a checksum with a different constant, which is one algebraic
    # fact wearing L hats. Detect the relation once and nominate a single byte.
    x_all = np.zeros(n, dtype=np.int64)
    for j in range(L):
        x_all ^= P[:, j]
    vals, cnt = np.unique(x_all, return_counts=True)
    if cnt.max() / n >= CKSUM_MIN_SCORE:
        # the checksum byte is the one that must move to hold the relation:
        # prefer the last byte that actually varies (trailing is the common
        # placement, though nothing requires it)
        cand = [b for b in range(L) if np.ptp(P[:, b]) > 0]
        pick = cand[-1] if cand else L - 1
        return [{"byte": pick, "algo": "xor_all_const", "const": int(vals[cnt.argmax()]),
                 "score": float(cnt.max() / n), "whole_frame": True}]

    s_all = P.sum(axis=1) & 0xFF
    vals, cnt = np.unique(s_all, return_counts=True)
    if cnt.max() / n >= CKSUM_MIN_SCORE:
        cand = [b for b in range(L) if np.ptp(P[:, b]) > 0]
        pick = cand[-1] if cand else L - 1
        return [{"byte": pick, "algo": "sum8_all_const", "const": int(vals[cnt.argmax()]),
                 "score": float(cnt.max() / n), "whole_frame": True}]

    out = []
    for b in range(L):
        target = P[:, b]
        if np.ptp(target) == 0:
            continue
        others = np.delete(P, b, axis=1)

        cands = {}
        x = np.zeros(n, dtype=np.int64)
        for j in range(others.shape[1]):
            x ^= others[:, j]
        cands["xor"] = x
        s = others.sum(axis=1) & 0xFF
        cands["sum8"] = s
        cands["sum8_neg"] = (-others.sum(axis=1)) & 0xFF
        cands["sum8_inv"] = (~others.sum(axis=1)) & 0xFF

        for name, pred in cands.items():
            # allow a fixed additive constant
            diff = (target - pred) & 0xFF
            consts, counts = np.unique(diff, return_counts=True)
            k = counts.argmax()
            score = counts[k] / n
            if score >= CKSUM_MIN_SCORE:
                out.append({"byte": b, "algo": name, "const": int(consts[k]),
                            "score": float(score)})
                break
    return out


# -------------------------------------------------------------- multiplexor

def multiplex_fields(g, counters, max_frames=MAX_FRAMES):
    """A small field whose value partitions the rest of the payload.

    A real multiplexor makes the other bytes far more predictable *within* a group
    than overall. A counter also partitions the frames, so counters are excluded.
    """
    n = min(g.n, max_frames)
    # was 500: a 0.5 Hz message never reaches that in a 13-minute run, so the
    # check silently skipped exactly the slow IDs multiplexors live on.
    if n < 150 or g.length < 3:
        return []
    P = _payload_matrix(g, n)
    counter_bits = set()
    for c in counters:
        counter_bits |= set(range(c["start"], c["start"] + c["length"]))

    def entropy_bytes(M):
        e = 0.0
        for j in range(M.shape[1]):
            _, cnt = np.unique(M[:, j], return_counts=True)
            p = cnt / cnt.sum()
            e += float(-(p * np.log2(p)).sum())
        return e

    nbits = g.length * 8
    best = []
    for length in (2, 3, 4):
        for start in range(0, nbits - length + 1):
            if set(range(start, start + length)) & counter_bits:
                continue
            v = common.extract_le(g.le_int[:n], start, length)
            vals, cnt = np.unique(v, return_counts=True)
            if not (2 <= len(vals) <= 8):
                continue
            if cnt.min() < 30:
                continue
            byte_of_field = start // 8
            keep = [j for j in range(g.length) if j != byte_of_field]
            if not keep:
                continue
            M = P[:, keep]
            e_all = entropy_bytes(M)
            e_grp = 0.0
            for val in vals:
                m = v == val
                e_grp += (m.sum() / n) * entropy_bytes(M[m])
            if e_all <= 0:
                continue
            gain = (e_all - e_grp) / e_all
            if gain > 0.35:
                best.append({"start": start, "length": length,
                             "n_values": int(len(vals)),
                             "values": [int(x) for x in vals[:8]],
                             "entropy_drop": float(gain)})
    best.sort(key=lambda x: -x["entropy_drop"])
    kept, used = [], set()
    for f in best:
        bits = set(range(f["start"], f["start"] + f["length"]))
        if bits & used:
            continue
        used |= bits
        kept.append(f)
    return kept[:2]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", required=True)
    ap.add_argument("--ids", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--no-mux", action="store_true")
    args = ap.parse_args()

    df = common.load_trace(args.trace)
    groups = common.group_by_id(df)
    only = {int(x, 0) for x in args.ids.split(",")} if args.ids else None

    results = {}
    tot_counter_bits = tot_cksum_bits = tot_static = tot_bits = tot_active = 0

    for cid, g in sorted(groups.items()):
        if only and cid not in only:
            continue
        nbits = g.length * 8
        rates = common._bit_flip_rates_le(g.le_int, nbits)
        active = int((rates > 1e-6).sum())
        tot_bits += nbits
        tot_active += active
        tot_static += nbits - active

        ctr = counter_fields(g)
        cks = checksum_fields(g)
        mux = [] if args.no_mux else multiplex_fields(g, ctr)

        cbits = sum(c["length"] for c in ctr)
        kbits = 8 * len(cks)
        tot_counter_bits += cbits
        tot_cksum_bits += kbits
        if ctr or cks or mux:
            results[f"{cid:X}"] = {"counters": ctr, "checksums": cks,
                                   "multiplexors": mux, "active_bits": active,
                                   "payload_bytes": g.length}

    print("=" * 76)
    print("BUS STRUCTURE  (counters / checksums / multiplexors)")
    print("=" * 76)
    print(f"  IDs analysed     : {len(groups) if not only else len(results)}")
    print(f"  payload bits     : {tot_bits:,}")
    print(f"  active bits      : {tot_active:,}")
    print(f"  static bits      : {tot_static:,} "
          f"({100*tot_static/max(tot_bits,1):.1f}%)")
    print(f"  counter bits     : {tot_counter_bits:,} "
          f"({100*tot_counter_bits/max(tot_active,1):.1f}% of active)")
    print(f"  checksum bits    : {tot_cksum_bits:,} "
          f"({100*tot_cksum_bits/max(tot_active,1):.1f}% of active)")
    print(f"  -> plumbing      : {tot_counter_bits + tot_cksum_bits:,} of "
          f"{tot_active:,} active bits "
          f"({100*(tot_counter_bits+tot_cksum_bits)/max(tot_active,1):.1f}%)")

    print("\n  COUNTERS (field increments by a fixed step, consecutive frames):")
    print(f"    {'ID':>5} {'start':>5} {'len':>4} {'step':>5} {'score':>7}")
    ncr = 0
    for k, v in results.items():
        for c in v["counters"]:
            print(f"    {k:>5} {c['start']:5d} {c['length']:4d} {c['step']:5d} "
                  f"{c['score']*100:6.1f}%")
            ncr += 1
    if not ncr:
        print("    (none)")

    print("\n  CHECKSUMS (byte reproduced from the others):")
    print(f"    {'ID':>5} {'byte':>5} {'algo':>10} {'const':>6} {'score':>7}")
    nck = 0
    for k, v in results.items():
        for c in v["checksums"]:
            print(f"    {k:>5} {c['byte']:5d} {c['algo']:>10} "
                  f"0x{c['const']:02X} {c['score']*100:6.1f}%")
            nck += 1
    if not nck:
        print("    (none)")

    if not args.no_mux:
        print("\n  MULTIPLEXOR CANDIDATES (field value predicts the other bytes):")
        print(f"    {'ID':>5} {'start':>5} {'len':>4} {'vals':>5} {'H drop':>8}  values")
        nmx = 0
        for k, v in results.items():
            for m in v["multiplexors"]:
                print(f"    {k:>5} {m['start']:5d} {m['length']:4d} "
                      f"{m['n_values']:5d} {m['entropy_drop']*100:7.1f}%  "
                      f"{m['values']}")
                nmx += 1
        if not nmx:
            print("    (none)")

    out = args.json or "temp-output/structure.json"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as f:
        json.dump({"totals": {
            "payload_bits": tot_bits, "active_bits": tot_active,
            "static_bits": tot_static, "counter_bits": tot_counter_bits,
            "checksum_bits": tot_cksum_bits}, "by_id": results}, f, indent=2)
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
