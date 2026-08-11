#!/usr/bin/env python3
"""segments.py - find the field encoding a HELD CATEGORICAL state.

FORK ADDITION (not upstream).

`correlate.py --type continuous` fits a LINE from raw value to reference, and
`--type discrete` scores bits that flip NEAR an event. Neither fits a categorical
state such as gear position, drive mode, or wiper setting: an ECU encodes those as
arbitrary code points - small consecutive integers, a bitmask, or something with
gaps - so there is no line to fit, and the field is constant *between* transitions
rather than merely flipping *at* them.

The right test for a state that is HELD is:

  * within each held segment the field is CONSTANT (high purity), and
  * across segments the field maps ONE-TO-ONE onto the states - in particular a
    state that recurs (park -> ... -> park) must show the SAME code both times.

That recurrence constraint is what kills rolling counters, which otherwise tie at
the top of any correlation on a handful of stepped holds.

Input is the state sidecar written by import_ccap.py (kind=value, sample-and-hold).
Output is a ranked table plus the decoded state->code mapping, which you hand to
build_dbc.py as an explicit --scale 1 --offset 0 field.

Example:
    python segments.py --trace temp-output/trace_steady.csv \
        --sidecar temp-output/sidecar_state_gear_steady.csv --guard 1.5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def build_segments(ref_t, ref_v, t_lo, t_hi, guard, min_hold):
    """Merge the sample-and-hold reference into [start, end, value] segments."""
    order = np.argsort(ref_t)
    ref_t, ref_v = np.asarray(ref_t)[order], np.asarray(ref_v)[order]

    segs = []
    for i, (t, v) in enumerate(zip(ref_t, ref_v)):
        end = ref_t[i + 1] if i + 1 < len(ref_t) else t_hi
        if segs and segs[-1][2] == v and abs(segs[-1][1] - t) < 1e-9:
            segs[-1][1] = end                     # merge repeats
        else:
            segs.append([t, end, v])

    out = []
    for a, b, v in segs:
        a2, b2 = a + guard, b - min(guard, 0.25 * (b - a))
        if b2 - a2 >= min_hold and b2 > t_lo and a2 < t_hi:
            out.append((max(a2, t_lo), min(b2, t_hi), float(v)))
    return out


def candidate_fields(payload_len):
    """(order, start_bit, length) candidates: bytes, nibbles, bits, and WIDE fields.

    Sub-byte fields need only one endianness: within a single byte, a Motorola
    field and the Intel field over the same bits evaluate to the same value (MSB
    -first from bit 7 down equals LSB-first from bit 0 up). Multi-byte fields do
    NOT have that property, so those are emitted in both orders - a categorical
    state occasionally spans a byte boundary, and scanning one endianness is how
    real Motorola signals get missed.
    """
    c = []
    for b in range(payload_len):
        c.append(("little", 8 * b, 8))
        c.append(("little", 8 * b, 4))
        c.append(("little", 8 * b + 4, 4))
        for i in range(8):
            c.append(("little", 8 * b + i, 1))
    nbits = payload_len * 8
    for length in (12, 16):
        for start in range(nbits):
            if start + length <= nbits:
                c.append(("little", start, length))
            if common.be_fits(payload_len, start, length):
                c.append(("big", start, length))
    return c


def score_field(raw, t, segs, min_frames):
    """Purity within segments + one-to-one state<->code mapping across them."""
    modes, purities, states = [], [], []
    for a, b, v in segs:
        m = (t >= a) & (t <= b)
        if m.sum() < min_frames:
            return None
        vals = raw[m]
        cnt = Counter(vals.tolist())
        mode, n = cnt.most_common(1)[0]
        modes.append(mode)
        purities.append(n / len(vals))
        states.append(v)

    # a state that recurs must show the same code; distinct states distinct codes
    by_state: dict[float, set] = {}
    for s, m in zip(states, modes):
        by_state.setdefault(s, set()).add(m)
    consistent = all(len(v) == 1 for v in by_state.values())
    codes = [next(iter(v)) for v in by_state.values()]
    injective = len(set(codes)) == len(codes)
    n_distinct = len(set(modes))

    if n_distinct < 2:
        return None

    mapping = {float(s): int(next(iter(v))) for s, v in sorted(by_state.items())}
    return {
        "mean_purity": float(np.mean(purities)),
        "min_purity": float(np.min(purities)),
        "consistent": consistent,
        "injective": injective,
        "bijective": consistent and injective,
        "n_distinct": n_distinct,
        "tidy": tidiness(mapping),
        "mapping": mapping,
        "modes": [int(m) for m in modes],
    }


def tidiness(mapping: dict) -> float:
    """How much does this look like an OEM code rather than a coincidence?

    The analogue of build_dbc's round-scale heuristic. Many fields can be
    constant-in-hold and one-to-one purely by luck, and they tie on purity. Real
    encodings tend to be tidy: small codes, consecutive, ordered the same way as
    the physical states. An accidental match is typically scattered (0,3,1,2).
    Ranking on this surfaces the plausible one first without ruling the others out
    - a designer is free to use scattered codes, so tidiness is a prior, not a
    filter.
    """
    codes = [mapping[k] for k in sorted(mapping)]
    if len(codes) < 2:
        return 0.0
    score = 0.0
    d = np.diff(codes)
    if np.all(d > 0) or np.all(d < 0):
        score += 0.5                                   # monotone with the states
    if np.all(np.abs(d) == 1):
        score += 0.3                                   # consecutive, no gaps
    elif np.all(np.abs(d) == np.abs(d[0])):
        score += 0.15                                  # evenly spaced
    if max(abs(c) for c in codes) <= 15:
        score += 0.2                                   # fits a nibble
    return round(score, 3)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", required=True)
    ap.add_argument("--sidecar", required=True)
    ap.add_argument("--guard", type=float, default=1.5,
                    help="seconds trimmed after each transition (reaction lag + "
                         "actuation settling), default 1.5")
    ap.add_argument("--min-hold", type=float, default=1.5,
                    help="ignore segments shorter than this, default 1.5s")
    ap.add_argument("--min-frames", type=int, default=0,
                    help="frames of an ID required per segment; 0 = RATE-AWARE "
                         "(derived from each message's own cycle time). A fixed "
                         "count silently drops slow IDs - a 1 Hz message yields ~30 "
                         "frames in a 30 s hold, so a flat '>=40' rule discards it "
                         "and can turn 'not on this bus' into a false conclusion.")
    ap.add_argument("--min-purity", type=float, default=0.98)
    ap.add_argument("--ids", default=None, help="restrict to these IDs")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    df = common.load_trace(args.trace)
    sidecar = common.load_sidecar(args.sidecar)
    common.warn_if_degraded(df, sidecar["epoch"].to_numpy(float))

    ref_t, ref_v = common.continuous_reference(sidecar)
    if len(ref_t) < 2:
        print("ERROR: need >=2 'value' rows in the state sidecar.", file=sys.stderr)
        return 1

    t = df["t"].to_numpy(float)
    segs = build_segments(ref_t, ref_v, t.min(), t.max(), args.guard, args.min_hold)
    if len(segs) < 2:
        print("ERROR: fewer than 2 usable held segments after guard/min-hold.",
              file=sys.stderr)
        return 1

    print(f"{len(segs)} held segment(s) (guard {args.guard}s, min hold {args.min_hold}s):")
    t0 = t.min()
    for a, b, v in segs:
        print(f"    state {v:>5g}   {a-t0:7.1f}..{b-t0:7.1f}s  ({b-a:5.1f}s)")
    recurring = len(segs) - len({v for _, _, v in segs})
    print(f"  {recurring} recurring state(s) - "
          + ("these pin the answer hard." if recurring else
             "NONE, so a monotone counter can still masquerade as the field."))
    print()

    only = {int(x, 0) for x in args.ids.split(",")} if args.ids else None
    groups = common.group_by_id(df)
    shortest = min((b - a) for a, b, _ in segs)
    results = []
    for cid, g in groups.items():
        if only and cid not in only:
            continue
        min_frames = (args.min_frames if args.min_frames > 0
                      else common.min_samples_for(g, shortest, floor=3))
        for order, start, length in candidate_fields(g.length):
            raw = common.extract_any(g, order, start, length)
            if np.ptp(raw) == 0:
                continue
            s = score_field(raw, g.t, segs, min_frames)
            if s and s["mean_purity"] >= args.min_purity:
                s.update({"id": cid, "id_hex": f"{cid:X}", "order": order,
                          "start_bit": start, "length": length,
                          "byte": start // 8, "bit_in_byte": start % 8})
                results.append(s)

    results.sort(key=lambda r: (r["bijective"], r["tidy"], r["n_distinct"],
                                r["mean_purity"], r["min_purity"]), reverse=True)

    if not results:
        print("No field is constant within every held segment. Either the state is "
              "not on this bus/window, or the guard is too small - try --guard 3.")
        return 2

    print(f"Top {min(args.top, len(results))} of {len(results)} constant-in-segment "
          f"field(s):\n")
    print(f"{'id_hex':>7} {'ord':>4} {'start':>6} {'len':>4} {'byte':>5} {'bit':>4} "
          f"{'purity':>7} {'min':>6} {'codes':>6} {'1:1':>4} {'tidy':>5}  mapping")
    print("-" * 112)
    for r in results[:args.top]:
        mp = " ".join(f"{k:g}->{v}" for k, v in r["mapping"].items())
        print(f"{r['id_hex']:>7} {r.get('order','le')[:2]:>4} {r['start_bit']:>6} "
              f"{r['length']:>4} {r['byte']:>5} "
              f"{r['bit_in_byte']:>4} {r['mean_purity']:>7.3f} {r['min_purity']:>6.3f} "
              f"{r['n_distinct']:>6} {'yes' if r['bijective'] else 'no':>4} "
              f"{r['tidy']:>5.2f}  {mp}")

    out = args.json or f"temp-output/segments_{os.path.basename(args.sidecar)}.json"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nWrote {out}")

    best = results[0]
    if best["bijective"]:
        print(f"\nBest: 0x{best['id_hex']} start_bit {best['start_bit']} "
              f"length {best['length']} - constant in every hold and one-to-one "
              f"with the states.")
        print(f"Next: build_dbc.py --id 0x{best['id_hex']} "
              f"--order {best.get('order','little')} "
              f"--start-bit {best['start_bit']} --length-bits {best['length']} "
              f"--scale 1 --offset 0 --name <Signal> --trace {args.trace}")
    else:
        print("\n[!] No field is BOTH constant in every hold AND one-to-one with the "
              "states. Treat the table as leads, not an answer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
