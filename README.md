# CAN bus reverse engineering skills

> ### What this fork changes

Upstream targets a CANsub interface recording webCAN CSV logs, with a human
supplying the reference signal live. This fork keeps all of that intact and adds
what was needed to work from **recorded multi-sensor captures** instead, plus a set
of search-completeness fixes that apply to any bus.

Nothing here is vehicle- or manufacturer-specific, and no vehicle data is included.

### New capabilities

| script | what it does | why it was needed |
|---|---|---|
| **`import_ccap.py`** | Reads a mobile capture-app bundle — CAN, GPS, IMU, operator annotations and instrument-cluster video sharing one clock — and emits the trace and sidecars the existing pipeline already speaks. Derives gravity-projected yaw rate from the IMU, so no mount calibration is needed. | Upstream assumes one CSV from one interface. A recorded bundle carries several references at once, and a button press marks a **held state**, not a point event — there was no way to express that. |
| **`structure.py`** | Proves out rolling counters, checksums and multiplexors: a counter must actually increment by a fixed step, a checksum must actually reproduce from the other bytes. Handles **sub-byte** counters. | These are the largest source of false positives. A counter correlates with any monotone reference and passes "does it ramp smoothly" tests *because* it takes small even steps. `survey.py` flags per byte, so a counter sharing a byte with data is invisible to it. |
| **`segments.py`** | Finds **held categorical states** (gear, drive mode, wiper setting) by constant-within-hold plus one-to-one across holds. | `correlate` fits a line, and a categorical code point has no line to fit. Its `--type discrete` scores bits that flip *at* an event, but a held state is constant *between* them. |
| **`coverage.py`** | Measures how much of a bus is decoded, against bits that are **ACTIVE** rather than all payload bits, counting counters/checksums separately. | RE has no natural finish line. A bit that never moves carries nothing the capture could have taught you, and plumbing shouldn't flatter the headline. |
| **`score_run.py`** | Scores a decoded DBC set against a reference set: exact / partial / unmatched / novel. | The **partial** class is the point: a field sharing only the dominant byte decodes to nearly the right values with the wrong geometry. It passes correlation, passes the verify gate, and reads as a win in any results table. |
| **`selftest_geometry.py`** | Pins the search-space contract. | So the fixes below cannot silently regress. |

### Search completeness

A negative result means one of two very different things — *the signal is not on the
bus*, or *the search could not express it*. Those are only distinguishable if the
search space is complete. It has four independent axes, and restricting any one makes
a whole class of field **unreachable rather than low-ranked**:

- **Both endiannesses, at arbitrary bit offsets and lengths.** `bitsearch` previously
  generated big-endian candidates only at byte-aligned starts with whole-byte widths.
  Packed Motorola fields routinely sit off byte boundaries with non-byte widths, so
  such a field could never be returned. `correlate` keeps byte-aligned sweeps as fast
  triage and gains `--deep`.
- **Rate-aware sample thresholds.** Cycle times on one bus span three orders of
  magnitude, so any constant frame-count minimum is wrong for most messages — and the
  IDs it silently discards are the slow ones carrying status and body signals.
- **Non-linear value interpretations** (`--interp`): sign-magnitude, complement, BCD,
  Gray code. An affine fit already absorbs offset-binary and inverted slopes; these
  are the remappings it cannot absorb.
- **Unit-aware scale roundness.** A scale designed as `0.01 mph` reads as
  `0.016093 km/h` and looks like noise. `build_dbc` now checks the reference unit
  *and its siblings*, and reports which unit made it round — which identifies the
  signal's native unit.

Also: a **capture-health gate**. A wireless capture can lose most of its frames
without losing any structure — the file still parses, every ID is present, and
correlation still returns a confident top candidate computed from the survivors.
`correlate`, `bitsearch` and `verify` now warn when the reference window is degraded.

## License and attribution

These skills are fully open source under the [MIT License](LICENSE) - you are free to use, modify and distribute them in your own projects.

Original work © 2026 CSS Electronics. Modifications in this fork © 2026 Power Gauge Pro LLC, released under the same MIT licence.

If you use them in your projects, videos or blog posts, we'd appreciate a reference to our article: [CAN bus reverse engineering with AI](https://www.csselectronics.com/pages/can-bus-reverse-engineering-ai-llm-claude).
