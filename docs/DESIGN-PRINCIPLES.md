# Design principles

Why the fork's additions look the way they do. Read this before adding a feature —
most of the additions here exist to close a specific way the pipeline produced a
confident wrong answer, and a new feature that does not think about its own failure
mode will reopen one.

## 1. The enemy is confident wrong output, not missing output

CAN reverse engineering has an unusual property: **almost every stage will produce
a plausible answer from bad input, and none of them fail loudly.**

- A capture that silently lost most of its frames still parses, still contains
  every ID, and still yields a confident top correlation computed from the
  survivors.
- A rolling counter correlates with any monotone reference and *passes* smoothness
  tests, precisely because it takes small even steps through its whole range.
- A field sharing only the dominant byte with the true one decodes to nearly the
  right values, passes correlation, and passes the verify gate.
- OCR of a display returns a number whether or not it read the display correctly.
- Speech recognition on non-speech audio returns fluent invented sentences, not
  silence.

In every case the output is *usable-looking*. There is no exception thrown and no
obviously broken number. A missing signal costs you a signal; a fabricated one
corrupts everything downstream that gets cross-checked against it — and it will
correlate with something, because a wrong-but-smooth series always does.

So: **a component that cannot be trusted must say so where the user is already
looking.** Not in a log file, not as a return code — in its own summary output, in
the terms of the decision being made. The degraded-capture banner, the OCR read
rate and discard warning, the transcription rejection report, and the structure
pass that separates counters and checksums from real data all exist for this.

Corollary: when adding a threshold, prefer the setting that yields *nothing* on
ambiguous input, and report what was suppressed and why. Silence that is explained
is a result. Silence that is unexplained looks like a clean run.

## 2. A negative claim is only as good as the search space

"This signal is not on the bus" and "my search could not express this signal" look
identical in the output, and are completely different conclusions. They are only
distinguishable if the search space is complete.

It has four independent axes, and quietly restricting any one of them turns a
false negative into a confident one:

| axis | the failure |
|---|---|
| **byte order** | scanning little-endian only makes every big-endian field at a non-byte offset unreachable — and a bus can be overwhelmingly one order |
| **bit offset** | byte-aligned-only scanning misses every packed sub-byte field |
| **length** | a fixed set of widths misses the rest |
| **interpretation** | unsigned-only misses two's complement, sign-magnitude, complement, BCD and Gray code |

This is why field enumeration lives in `common.py` and why
`selftest_geometry.py` exists: an ad-hoc scan written inline for one investigation
is how an axis gets dropped without anyone noticing. Before recording a negative,
verify the search actually covered all four.

Sampling rate is a fifth, physical limit: a signal cannot be characterised above
half the message rate. A search for behaviour faster than Nyquist allows will
always fail, and that failure says nothing about the bus.

## 3. References are the hard part, and each one lies differently

The search is mechanical; getting a trustworthy reference to search *against* is
where runs succeed or fail. A recorded drive can supply several independent ones,
and their value is that they fail in *uncorrelated* ways — so a candidate confirmed
by two unrelated references is far stronger than one confirmed twice by the same
kind of evidence.

But each carries a distinct systematic error, and the error must be modelled rather
than averaged away:

- **Satellite positioning** measures ground truth, which is *not* what a vehicle
  displays — indicated speed carries a deliberate optimistic bias and a tyre-size
  dependence. Fine for identifying a field; not sufficient for calibrating one.
- **Phone inertial sensors** are on a non-rigid mount. Vibration is broadband
  noise, the mount can be knocked, and raw axes are in the phone's frame, not the
  vehicle's — derive orientation-independent quantities instead of assuming an
  axis. Some quantities are only observable while moving.
- **A display, read by OCR** is the vehicle's own belief, which is what you want,
  but reading it depends entirely on filming conditions. A high mean confidence
  hides per-frame glitches; verify the series against another reference and against
  physical plausibility before relying on it.
- **Human markers — button presses and speech** carry reaction lag, always in the
  direction of being *late*. That direction is a usable check: a bus transition
  that follows its annotation is suspicious. Speech additionally carries
  recognition error, including homophones.

Practical consequence: keep the lag search enabled for human references, and treat
any reference's own quality metric as a gate rather than a footnote.

## 4. Plot the shape before believing the score

Every scoring statistic here — correlation, anchor symmetry, constant-within-hold,
smooth ramping — can be satisfied by a field that is not the signal. The test that
actually settles it is whether the series *does something only the real physical
quantity could do*.

Two failure modes pass scores convincingly and are obvious on sight: a slice across
unrelated sub-records shows flat plateaus with instantaneous jumps and no
intermediate values, where a continuous physical quantity must sweep through them;
and a counter shows a perfect sawtooth. Neither survives being looked at.

## 5. Mechanism, never answers

The skill should know every encoding pattern anyone might plausibly implement, and
none of what a particular manufacturer did. More mechanism is strictly better —
it widens what can be expressed, which is what makes negatives meaningful. Encoded
answers do the opposite: they bias the next run toward confirming them and make any
measurement of the skill's own improvement worthless.

The line is not always obvious. A useful test: does this help decode a bus we have
never seen, or does it only help decode the one we already did? See
`SANITIZATION.md` for the version of this question applied line by line.

## 6. Categorical is not continuous

A held state — gear, drive mode, a switch position — has no line to fit, so
correlation cannot find it, and a discrete-event search looking for bits that flip
*at* a moment cannot either, because the state is constant *between* moments. It
needs its own method: constant-within-hold plus one-to-one across holds.

This is worth stating as a principle because it is a recurring blind spot. When a
signal that obviously exists cannot be found, check first whether the search being
used can express its *kind* at all.
