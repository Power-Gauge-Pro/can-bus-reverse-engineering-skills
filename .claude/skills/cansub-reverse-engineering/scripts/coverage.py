#!/usr/bin/env python3
"""coverage.py - how much of the bus have we actually decoded?

FORK ADDITION (not upstream).

Reverse engineering a bus has no natural finish line, so track progress against a
denominator. Two are reported, and the second is the honest one:

  RAW      - every payload bit on every unique ID. Includes bits that never change
             and may not encode anything, so it flatters nothing but understates
             progress.
  ACTIVE   - only bits that actually TOGGLE during the run. This is the real
             target: a bit that never moves carries no information you could have
             discovered from this capture. Note it is capture-relative - exercise
             more of the vehicle and the denominator grows.

Counters and checksums are counted separately. They are genuinely decoded once
identified, but they are plumbing rather than signal, so a coverage number that
leans on them would be misleading.

Usage:
    python coverage.py --trace temp-output/trace_<tag>.csv \
        --dbc-dir decoding-output/<application>
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def signal_bits(sig, payload_len: int) -> list[int]:
    """LSB-first global bit indices a cantools signal occupies.

    Little-endian: cantools `start` already IS the LSB-first index.
    Big-endian: `start` is the MSB in sawtooth numbering, and the field walks
    DOWN within a byte then jumps to bit 7 of the next byte.
    """
    if sig.byte_order == "little_endian":
        return list(range(sig.start, sig.start + sig.length))
    bits, pos = [], sig.start
    for _ in range(sig.length):
        byte, bit = pos // 8, pos % 8
        if byte >= payload_len:
            break
        bits.append(byte * 8 + bit)
        pos = (byte + 1) * 8 + 7 if bit == 0 else pos - 1
    return bits


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", required=True)
    ap.add_argument("--dbc-dir", default=None,
                    help="directory searched recursively for *.dbc")
    ap.add_argument("--dbc", action="append", default=[],
                    help="an individual DBC (repeatable)")
    ap.add_argument("--min-flip", type=float, default=1e-6,
                    help="flip rate above which a bit counts as ACTIVE")
    ap.add_argument("--structure", default=None,
                    help="structure.json from structure.py; counts counter and "
                         "checksum bits separately so they cannot inflate the "
                         "headline (they are plumbing, not signal)")
    ap.add_argument("--show", type=int, default=25,
                    help="how many undecoded IDs to list, by active-bit count")
    args = ap.parse_args()

    df = common.load_trace(args.trace)
    groups = common.group_by_id(df)

    paths = list(args.dbc)
    if args.dbc_dir:
        paths += sorted(glob.glob(os.path.join(args.dbc_dir, "**", "*.dbc"),
                                  recursive=True))
    # a combined application DBC duplicates its parts; prefer per-signal files
    paths = [p for p in paths if os.path.basename(p) != "combined.dbc"]

    covered: dict[int, set] = {}
    named: dict[int, list] = {}
    if paths:
        import cantools
        for p in paths:
            try:
                db = cantools.database.load_file(p)
            except Exception as e:                      # noqa: BLE001
                print(f"  [!] skipped {p}: {e}")
                continue
            for msg in db.messages:
                g = groups.get(msg.frame_id)
                plen = g.length if g else msg.length
                for s in msg.signals:
                    b = signal_bits(s, plen)
                    covered.setdefault(msg.frame_id, set()).update(b)
                    named.setdefault(msg.frame_id, []).append(
                        (s.name, min(b) if b else -1, len(b)))

    rows = []
    tot_bits = tot_active = tot_cov = tot_cov_active = 0
    for cid, g in sorted(groups.items()):
        nbits = g.length * 8
        rates = common._bit_flip_rates_le(g.le_int, nbits)
        active = set(np.where(rates > args.min_flip)[0].tolist())
        cov = covered.get(cid, set())
        cov_active = cov & active
        tot_bits += nbits
        tot_active += len(active)
        tot_cov += len(cov & set(range(nbits)))
        tot_cov_active += len(cov_active)
        rows.append({
            "id": cid, "bits": nbits, "active": len(active),
            "cov": len(cov & set(range(nbits))), "cov_active": len(cov_active),
            "sigs": named.get(cid, []),
        })

    n_ids = len(groups)
    print("=" * 74)
    print("BUS COVERAGE")
    print("=" * 74)
    print(f"  trace            : {args.trace}")
    print(f"  DBCs loaded      : {len(paths)}")
    print(f"  unique IDs       : {n_ids}")
    print(f"  payload bytes    : {tot_bits // 8:,}   ({tot_bits:,} bits)")
    print(f"  ACTIVE bits      : {tot_active:,} of {tot_bits:,} "
          f"({100*tot_active/max(tot_bits,1):.1f}% of the bus ever changes)")
    print()
    print(f"  decoded (raw)    : {tot_cov:,} / {tot_bits:,} bits  "
          f"= {100*tot_cov/max(tot_bits,1):.2f}%")
    print(f"  decoded (ACTIVE) : {tot_cov_active:,} / {tot_active:,} bits  "
          f"= {100*tot_cov_active/max(tot_active,1):.2f}%   <-- headline")
    ids_touched = sum(1 for r in rows if r["cov"])
    print(f"  IDs touched      : {ids_touched} / {n_ids} "
          f"({100*ids_touched/max(n_ids,1):.0f}%)")

    if args.structure and os.path.exists(args.structure):
        import json as _json
        st = _json.load(open(args.structure))
        t = st.get("totals", {})
        plumb = t.get("counter_bits", 0) + t.get("checksum_bits", 0)
        signal = max(tot_active - plumb, 1)
        print()
        print(f"  plumbing         : {plumb:,} active bits "
              f"({t.get('counter_bits',0):,} counter + "
              f"{t.get('checksum_bits',0):,} checksum) - identified, not signal")
        print(f"  SIGNAL-BEARING   : {signal:,} active bits "
              f"(active minus plumbing)")
        print(f"  decoded / signal : {tot_cov_active:,} / {signal:,} bits  "
              f"= {100*tot_cov_active/signal:.2f}%   <-- strictest")

    if any(r["sigs"] for r in rows):
        print("\n  decoded signals:")
        for r in rows:
            for name, lsb, ln in r["sigs"]:
                print(f"    0x{r['id']:<4X} {name:<24} start_bit {lsb:>3} "
                      f"len {ln:>2}")

    print(f"\n  biggest undecoded IDs by ACTIVE bits (top {args.show}):")
    print(f"    {'ID':>5} {'len':>4} {'active':>7} {'decoded':>8} {'remaining':>10}")
    rem = sorted(rows, key=lambda r: -(r["active"] - r["cov_active"]))
    for r in rem[:args.show]:
        left = r["active"] - r["cov_active"]
        if left <= 0:
            continue
        print(f"    {r['id']:5X} {r['bits']//8:4d} {r['active']:7d} "
              f"{r['cov_active']:8d} {left:10d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
