#!/usr/bin/env python3
"""selftest_geometry.py - guard the search-space contract.

FORK ADDITION. A negative result only means "not on the bus" if the search could
have expressed the signal. The search space has four independent axes - byte order,
bit offset, length, and value interpretation - and restricting any one of them
makes a whole class of field unreachable rather than merely low-ranked. This test
pins each axis, using field shapes that are ordinary on real buses: Motorola fields
at non-byte offsets with non-byte widths, and non-linear value encodings.

Run after touching common.extract_*, field_candidates, INTERPRETATIONS, or any
scan's candidate generation:  python selftest_geometry.py
"""
import sys
import numpy as np
import common

FAIL = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


class G:                       # minimal IdGroup stand-in
    def __init__(self, payload):
        self.length = len(payload)
        self.le_int = np.array([int.from_bytes(payload, "little")], dtype=object)
        self.be_int = np.array([int.from_bytes(payload, "big")], dtype=object)


def main():
    print("field geometry self-test\n")
    # A known payload with a distinctive bit pattern.
    pl = bytes([0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC, 0xDE, 0xF0])
    g = G(pl)
    be = int.from_bytes(pl, "big")

    # 1. byte-aligned big-endian must agree with the legacy helper
    for byte in range(6):
        for width in (1, 2, 3):
            legacy = common.extract_be(g.be_int, g.length, byte, width)[0]
            general = common.extract_be_bits(g.be_int, g.length, byte * 8 + 7,
                                             width * 8)[0]
            if legacy != general:
                check(f"BE byte {byte} width {width}", False,
                      f"{legacy} != {general}")
                return
    check("byte-aligned BE matches legacy extract_be for all byte/width", True)

    # 2. arbitrary big-endian against a hand-computed reference
    def ref_be(start, length):
        nbits = len(pl) * 8
        b, bit = start // 8, start % 8
        msb = b * 8 + (7 - bit)
        return (be >> (nbits - (msb + length))) & ((1 << length) - 1)
    for start, length in ((35, 12), (3, 10), (19, 2), (7, 16), (15, 16), (55, 8)):
        got = common.extract_be_bits(g.be_int, g.length, start, length)[0]
        check(f"BE start {start:2d} len {length:2d}", got == ref_be(start, length),
              f"= {int(got)}")

    # 3. within one byte, BE and LE over the same bits agree (bit order cancels)
    ok = all(common.extract_be_bits(g.be_int, g.length, 8 * b + 7, 4)[0]
             == common.extract_le(g.le_int, 8 * b + 4, 4)[0] for b in range(8))
    check("sub-byte BE nibble == LE nibble over the same bits", ok)

    # 4. THE REGRESSION: candidate generation must reach a non-byte-aligned
    #    Motorola field. This is the check that would have caught the original bug.
    cands = set(common.field_candidates(8, lengths=(12,)))
    check("field_candidates emits BE start 35 len 12 (non-byte-aligned, 12-bit)",
          ("big", 35, 12) in cands)
    cands10 = set(common.field_candidates(8, lengths=(10,)))
    check("field_candidates emits BE start 3 len 10 (non-byte-aligned, 10-bit)",
          ("big", 3, 10) in cands10)
    orders = {o for o, _, _ in common.field_candidates(8, lengths=(16,))}
    check("field_candidates emits BOTH endiannesses", orders == {"little", "big"},
          f"got {sorted(orders)}")

    # 5. extract_field must accept big + start/length (build_dbc path)
    try:
        raw, ct, ln = common.extract_field(g, "big", False, start_bit=35,
                                           length_bits=12)
        check("extract_field(big, start_bit, length_bits) works",
              raw[0] == ref_be(35, 12) and ct == 35 and ln == 12)
    except Exception as e:                       # noqa: BLE001
        check("extract_field(big, start_bit, length_bits) works", False, str(e))

    # 6. rate-aware threshold must not demand more frames than a slow ID can give
    class SlowG:
        n = 800
        t = np.arange(800, dtype=float)          # 1 Hz for 800 s
    need = common.min_samples_for(SlowG(), seconds=30.0)
    check("min_samples_for(1 Hz, 30 s window) <= 30", need <= 30, f"= {need}")

    # 7. value interpretations: each must be reachable and correct
    iv = np.array([0x1234, 0x0099, 0x8001, 0x000F], dtype=float)
    check("interp signed is two's complement",
          list(common.interp_signed(np.array([0xFFFF, 1], float), 16)) == [-1.0, 1.0])
    check("interp sign_magnitude splits sign from magnitude",
          list(common.interp_sign_magnitude(np.array([0x8005, 5], float), 16)) == [-5.0, 5.0])
    check("interp complement inverts within the field width",
          list(common.interp_complement(np.array([0x00FF], float), 16)) == [0xFF00])
    bcd = common.interp_bcd(np.array([0x1234, 0x00A0], float), 16)
    check("interp bcd decodes digits and rejects non-BCD nibbles",
          bcd[0] == 1234 and np.isnan(bcd[1]), f"= {bcd[0]}, {bcd[1]}")
    check("interp gray decodes a Gray sequence to 0,1,2,3",
          list(common.interp_gray(np.array([0, 1, 3, 2], float), 4)) == [0.0, 1.0, 2.0, 3.0])
    names = set(common.resolve_interps("all"))
    check("resolve_interps('all') covers every registered interpretation",
          names == set(common.INTERPRETATIONS))
    check("resolve_interps('default') is unsigned + signed",
          common.resolve_interps("default") == ["unsigned", "signed"])
    try:
        common.resolve_interps("nope")
        check("resolve_interps rejects an unknown name", False)
    except ValueError:
        check("resolve_interps rejects an unknown name", True)

    # 8. unit-aware scale roundness
    r = common.scale_roundness(0.01 * 1.609344, ref_unit="km/h")
    check("a signal designed in mph is recognised when measured in km/h",
          r["nice"] and r["switched"] and r["unit"] == "mph"
          and abs(r["nearest"] - 0.01) < 1e-9,
          f"-> {r['nearest']:g} {r['unit']}")
    r = common.scale_roundness(0.01, ref_unit="mph")
    check("a scale already round in the measured unit does not switch",
          r["nice"] and not r["switched"])
    r = common.scale_roundness(2.583, ref_unit="km/h")
    check("a wrong-geometry scale stays NOT round across all siblings",
          not r["nice"] and r["n_tested"] > 1, f"tested {r['n_tested']} unit(s)")
    r = common.scale_roundness(0.03125, ref_unit="km/h")
    check("a binary fraction (1/32) counts as round", r["nice"])
    r = common.scale_roundness(0.0160934, ref_unit="widgets")
    check("an unrecognised unit falls back to a single-unit check",
          r["n_tested"] == 1)
    check("normalise_unit maps aliases", common.normalise_unit("KPH") == "km/h"
          and common.normalise_unit("degC") == "C")
    sibs = dict(common.unit_siblings("km/h"))
    check("unit_siblings converts km/h -> mph correctly",
          abs(sibs.get("mph", 0) - 1 / 1.609344) < 1e-9, f"= {sibs.get('mph')}")
    # the sibling bar must be stricter than the in-unit bar
    loose = common.scale_plausibility(0.0157503, tol=0.02)["nice"]
    check("sibling search requires a stricter tolerance than the reference unit",
          loose and common.scale_roundness(0.0157503, "km/h")["switched"] is False)

    print()
    if FAIL:
        print(f"{len(FAIL)} FAILURE(S): {FAIL}")
        return 1
    print("all geometry checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
