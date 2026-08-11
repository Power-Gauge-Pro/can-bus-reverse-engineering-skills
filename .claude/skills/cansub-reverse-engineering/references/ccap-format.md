# ccap bundles — the mobile capture app's export format

**Fork addition.** Upstream assumes a webCAN CSV from a CANsub. These captures come
from the `can-capture` mobile app as a *bundle* of sources sharing one clock.
`scripts/import_ccap.py` converts a bundle into the trace + sidecars the rest of
the pipeline already speaks. Nothing downstream of the import changes.

## Bundle layout

```
ccap_<ts>_<vehicle>_<session-ulid>_r<n>/
  session.json                 title, notes, export time
  vehicle.json                 year/make/model, known_buses (bus_id, bitrate)
  manifest.json
  experiments/<exp>/run-NNN/
    run.json                   started_utc/ended_utc, adapter string, per-source files
    can.csv                    the bus
    gps.csv                    phone GPS
    imu.csv                    phone accelerometer + gyroscope (when present)
    annotations.json           button presses, app markers, toggles
    video.mp4                  instrument-cluster video
    experiment.json            the script the operator followed (steps, hold_seconds)
```

Sources are stamped in **UTC ISO-8601 from the same phone clock**, so they align
without a sync step and there is no `flask_sync.py` equivalent — but button presses
still carry human reaction lag, so keep the lag search enabled.

**The schema is not frozen.** Columns have been added between app versions. Read
the actual header of each CSV rather than trusting this document, and check
`manifest.json` for the generator version.

## can.csv → webCAN

Same information, different spelling. `import_ccap.py` handles all of it.

| ccap | webCAN | note |
|---|---|---|
| `utc` | `TimestampEpoch` | ISO-8601 → float epoch |
| `bus` | `BusChannel` | `bus_1` → `1` |
| `id_hex` | `ID` | already bare hex |
| `ide` | `IDE` | |
| `dlc` | `DLC` | |
| `len` | `DataLength` | |
| `dir` | `Dir` | `R`/`T` → `0`/`1` |
| `fdf` | `EDL` | |
| `brs` | `BRS` | |
| `esi` | `ESI` | |
| `rtr` | `RTR` | column order differs — webCAN puts RTR 11th |
| `data` | `DataBytes` | |
| `err` | *(dropped)* | error frames are filtered out |
| `t_mono_us` | *(becomes the timebase when present)* | monotonic microseconds |

Separator is `,` in ccap and `;` in webCAN.

**Timestamp gotcha.** These stamps have microsecond precision, so pandas parses
them at `us` resolution and `astype("int64") / 1e9` is wrong by 1000×. Subtract a
`Timestamp("1970-01-01", tz="UTC")` instead — resolution-proof.

**Prefer `t_mono_us` when the bundle has it.** Wall-clock stamping has two failure
modes that a monotonic counter does not:

- *Batch stamping.* If frames are stamped when a read batch is drained rather than
  per frame, many frames share a timestamp. Cycle time and jitter are then
  meaningless — `survey.py`'s `period`/`jit` columns become artifacts — even though
  correlation against a slow reference still works fine.
- *Clock steps.* A phone's wall clock can jump mid-run; the app records a
  `clock_stepped` interruption when it notices. That reorders frames and corrupts
  any timing measurement.

The importer prefers `t_mono_us` automatically, rebuilds the timeline from it, and
re-anchors to wall time using the MEDIAN offset so cross-source alignment with GPS,
annotations and video survives. It reports which timebase it used and how many
frames the anchoring corrected — read that line.

## References: all off-bus

Upstream's offline mode decodes the reference *from the log* via a DBC. These are
all external, so `--exclude-ids` is never needed — the reference cannot self-match.

**GPS speed** (`sidecar_gps_<tag>.csv`, `kind=value`). m/s in the source, converted
with `--gps-unit`. The natural reference for wheel/vehicle speed — *if the vehicle
actually moved while the bus was healthy*. The importer warns when it did not.

Timestamps come from `t_fix_utc` (when the fix was taken) rather than `utc` (when
the phone recorded it). The two differ, and using the record time injects that
difference as a systematic lag into every correlation.

Note GPS is **not** interchangeable with indicated speed: a speedometer differs
from ground truth by tyre size and by a deliberate optimistic bias, and the size of
that gap is vehicle-specific. GPS is fine for *identifying* a speed field; to
*calibrate* its scale you need a reference that reads what the vehicle believes.

**Annotations** land two ways:

- `sidecar_events_<tag>.csv` — every annotation as `kind=event`, for
  `correlate.py --type discrete`.
- `sidecar_state_<family>_<tag>.csv` — held states as `kind=value` **and**
  `kind=anchor`.

  A press like `<family>-<state>` is not a point event; it puts the vehicle into a
  state that holds until the next press in that family. The importer groups by the
  `button_id` prefix and emits a sample-and-hold series plus anchors at each
  instant. Grouping is structural, not vehicle-specific.

  The `value` series suits a state that genuinely persists. The `anchor` rows suit
  a continuously-varying quantity, where holding one value between presses would be
  a lie — use `--ref-window` to consume those instead.

  Ordinals default to first-appearance order, which is fine for correlation but
  arbitrary for scale/offset. `--state-values` lets you order them physically:

  ```json
  {"<family>": {"<state-a>": 0, "<state-b>": 1, "<state-c>": 2}}
  ```

  `--state-initial FAMILY=STATE@REL` seeds a state the vehicle was already in
  before the first press. That matters more than it looks: a state that RECURS is
  what lets `segments.py` reject rolling counters, and without the seed the opening
  state is invisible.

  `toggle_on`/`toggle_off` pairs become 1/0 series per label.

**Operator annotations are approximate.** They are typed by a human, often while
the vehicle is moving, so treat their timing as a hint and the bus as the evidence.
Expect a lag between the physical event and the press, in the direction of the
press being LATE — and check that direction, because a bus transition that
*follows* its annotation is suspicious.

**Cluster video** — when the phone films the instrument cluster it carries an
independent record of whatever the cluster displays, for the whole run.

Align with `--start-epoch` from `run.video.started_utc` (the importer prints the
value). The file is usually portrait while the cluster is landscape, so it needs
rotating before anything can read it:

```bash
ffmpeg -i video.mp4 -vf "transpose=1" -c:a copy video_upright.mp4   # 90° clockwise
ffmpeg -i video.mp4 -vf "transpose=2" -c:a copy video_upright.mp4   # 90° anticlockwise
```

**Check which one is correct for your bundle** — it depends on how the phone was
mounted and has differed between runs. Extract one frame and look at it.

`vision_reference.py` OCRs a displayed number into a sidecar. Whether that succeeds
depends entirely on the filming: digit height, contrast, glare, and whether the
mount held still. **Verify its output before using it** — it reports a read rate
and a mean confidence, and a low read rate means the series is noise, not a
reference. Two independent checks are worth doing: does the OCR'd series track GPS
where both exist, and does its range match what the vehicle plausibly did.

If OCR is not usable, reading the cluster **by eye at chosen timestamps** gives
exact values and works as anchors. Pick timestamps where the CAN capture actually
has frames — a dropout makes an anchor useless — then:

```bash
ffmpeg -v error -ss <t> -i video.mp4 -frames:v 1 \
    -vf "transpose=<1 or 2>,crop=<w>:<h>:<x>:<y>,scale=500:300" frame_<t>.jpg
```

Re-check the crop across the run rather than assuming one region holds: if the
mount shifts, a fixed region will drift off the target.

**Spoken narration** — if the video has an audio track, an operator who talked
through the run recorded annotations that a button press cannot match: speech is
hands-free, so it can be given while driving, and it lands closer to the event.

`transcribe_audio.py` extracts the audio, transcribes it locally, and writes both a
readable transcript and an events sidecar on the same clock as everything else
(pass `--start-epoch` from `run.video.started_utc`, exactly as for the video).

```bash
python scripts/transcribe_audio.py --video <run>/video.mp4 \
    --start-epoch <run.video.started_utc> --label narration
```

**Speech recognition invents text on non-speech audio.** This is the failure mode
that matters, and it is not rare: engine and road noise reliably produce confident,
fluent, entirely fabricated sentences — most notoriously stock phrases absorbed
from training data ("Thanks for watching", "Subscribe"). A fabricated annotation is
worse than a missing one, because it *will* correlate with something and the result
gets believed.

So the script gates hard by default — voice-activity detection, the model's own
no-speech probability, average token log-probability, and a blocklist for the stock
phrases (which are fluent enough to pass the numeric checks). It reports every
rejection. **On a run where nobody spoke, the correct output is zero events**, and
seeing that is the point; do not relax the thresholds to manufacture annotations.

Two further cautions once you do have a transcript:

- **Read it before using it.** In-cabin audio is noisy and homophones are common
  ("braking" → "breaking"), so a `--map` pattern must be tolerant or it drops the
  event silently. The script lists accepted segments that matched no pattern.
- **Speech is a human reference**, so it carries the same reaction lag as a button
  press, in the same direction: the words TRAIL the event. Keep the lag search on,
  and treat a bus transition that *follows* its narration as suspicious.

`--words` emits one event per word instead of per segment, which times a cue more
precisely — a spoken cue lands on its final word, not on the start of the sentence.

**IMU** (`imu.csv`) → `sidecar_imu_<channel>_*.csv`. LONG format: one row per
sample with a `sensor` column selecting which triple is populated.

```
utc,sensor,ax_mps2,ay_mps2,az_mps2,gx_rads,gy_rads,gz_rads
```

`sensor` is `accel` or `gyro`. Gyro is emitted in deg/s.

The raw axes are in the PHONE's frame, at whatever angle the mount holds, so no raw
axis is "yaw" or "longitudinal" without knowing that angle. The importer therefore
derives:

- **`yaw_rate_dps`** — the gyro vector projected onto GRAVITY, estimated from the
  accelerometer. Rotation about the vertical axis is yaw however the phone is
  turned, so this needs no mount calibration. It reports the estimated mount tilt,
  which is worth sanity-checking.

Two cautions:

- **Yaw rate only means anything while MOVING.** A stationary vehicle does not yaw
  however far the wheel is turned, so any steering exercise done parked is invisible
  to it.
- **A phone mount is not a rigid sensor platform.** Vibration adds broadband noise
  to the gyro, and the mount can be knocked. Compare the raw and smoothed signal
  before assuming the reference is good enough: if smoothing changes correlations
  substantially, noise was dominating and the reference is the limiting factor.

## Capture health — read this before trusting any result

A wireless capture can lose most of its frames without losing any structure: the
CSV still parses, every ID is still present, and correlation still returns a
confident top candidate computed from whatever survived. Nothing downstream
notices.

So:

- `import_ccap.py` prints a health report and writes `health_<tag>.json`.
- `--auto-window` clips to the last sustained full-rate region.
- `common.warn_if_degraded()` fires a banner from `correlate.py`, `bitsearch.py`
  and `verify.py`, scoped to the span the reference actually covers.

Treat the banner as a stop sign, not a note. Also read `run.json`'s
`interruptions` — but do not trust its silence, since an app can fail to notice an
outage it did not detect.

## Always plot the candidate against time before believing it

Every scoring statistic here — correlation, anchor symmetry, constant-in-hold, even
"it ramps smoothly" — can be satisfied by a field that is not the signal. Plot the
decoded series over the exercise and look at its SHAPE before believing any score.

Two failure modes to expect, both of which pass scores convincingly:

- **A slice across unrelated sub-records.** A message whose whole payload changes
  can yield a field with near-perfect symmetry and a beautiful fit, while its time
  series shows flat plateaus with instantaneous jumps and no intermediate values. A
  continuously-varying physical quantity must sweep through the values in between.
- **A rolling counter.** Counters score well on smoothness tests *because* they
  take small even steps and visit their whole range. Check whether the bytes under
  your field actually vary: a field spanning a constant byte is really a narrower
  field multiplied by a power of two. Note `survey.py` classifies counters PER
  BYTE, so a sub-byte counter sharing a byte with real data is not flagged.

The test that settles it: does the series do something only the real physical
quantity could do?

## Worked example

```bash
# 1. inspect, then re-import clipped to a healthy window if the report warrants it
python scripts/import_ccap.py --run <path-to>/run-NNN
python scripts/import_ccap.py --run <path-to>/run-NNN \
    --auto-window --tag steady \
    --state-values config/<vehicle>-states.json

# 2. what is on the bus, and what is plumbing
python scripts/survey.py --trace temp-output/trace_steady.csv
python scripts/structure.py --trace temp-output/trace_steady.csv

# 3. locate a candidate, then pin the field
python scripts/correlate.py --trace temp-output/trace_steady.csv \
    --sidecar temp-output/sidecar_<ref>_<tag>.csv --type continuous
python scripts/bitsearch.py --trace temp-output/trace_steady.csv \
    --sidecar temp-output/sidecar_<ref>_<tag>.csv --id 0x<ID>

# 4. decode + gate
python scripts/build_dbc.py --trace temp-output/trace_steady.csv \
    --sidecar temp-output/sidecar_<ref>_<tag>.csv --id 0x<ID> \
    --name <SignalName> \
    --out decoding-output/<application>/<signal>/<signal>.dbc
python scripts/verify.py --trace temp-output/trace_steady.csv \
    --dbc decoding-output/<application>/<signal>/<signal>.dbc \
    --sidecar temp-output/sidecar_<ref>_<tag>.csv

# 5. measure progress
python scripts/coverage.py --trace temp-output/trace_steady.csv \
    --dbc-dir decoding-output/<application> \
    --structure temp-output/structure.json
```

Discrete states want `build_dbc.py --scale 1 --offset 0` — there is no line to fit.
