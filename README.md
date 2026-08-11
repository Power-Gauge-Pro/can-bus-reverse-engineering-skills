# CAN bus reverse engineering skills



This repo contains [Claude Code](https://www.claude.com/claude-code) skills that help you reverse engineer raw CAN bus data into decoding rules stored as DBC files. 

Specifically, the skills help you leverage AI/LLM tools like Claude Code and [python-can](https://www.csselectronics.com/pages/python-can-usb-serial-api-stream) scripts to identify which CAN ID and data bits encode a
real-world value (speed, RPM, state of charge, ...), work out its start bit,
length, endianness, scale and offset, and verify the result. 

The skills assume that you are using a [CANsub](https://www.csselectronics.com/products/can-fd-usb-interface-ethernet-cansub-2) CAN bus interface from [CSS Electronics](https://www.csselectronics.com) to either record CSV log files via e.g. the [webCAN](https://www.csselectronics.com/pages/webcan-can-bus-streaming-software-browser) tool, or stream data in real-time via USB/Ethernet.

The repo bundles three skills (auto-discovered when you open the folder in Claude
Code):

- **cansub-reverse-engineering** - the workflow
- **combine-dbc** - merge per-signal DBCs into one
- **cansub-knowledge** - CANsub specs / API reference

<br>

**Note:** This is not a 'polished tool', but an illustration of how you can use the CANsub + Python + AI for CAN sniffing

**Note:** We strongly recommend reading our related article [CAN bus reverse engineering with AI](https://www.csselectronics.com/pages/can-bus-reverse-engineering-ai-llm-claude).

[![Watch the CAN bus reverse engineering demo](docs/vision-reference-demo.png)](https://cdn.shopify.com/videos/c/o/v/e384c5a75b7943e681dcbad2d10e230a.mp4)


## What this fork changes

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

### Intended companion app

The bundle format `import_ccap.py` reads is produced by a **mobile capture app**
currently in development at Power Gauge Pro. The app is not released yet, so this
section is a placeholder describing the intended workflow — the importer and the
format documentation in
[`references/ccap-format.md`](.claude/skills/cansub-reverse-engineering/references/ccap-format.md)
are already usable, and the bundle layout is documented well enough to be produced
by other means.

The idea is to make a capture session something one person can run in a vehicle
without a laptop:

- **Adapter** — a WiFi CAN interface such as the **MeatPi WiCAN**, or a Bluetooth
  ELM/STN adapter such as the **OBDLink MX+**, plugged into the OBD2 port. The
  format itself is adapter-agnostic; `run.json` simply records which was used.
- **Phone** — an iPhone records the bus alongside **GPS**, the **IMU**
  (accelerometer + gyroscope), operator **annotations**, and **video** of the
  instrument cluster, all on one clock.
- **Guided experiments** — the app walks the operator through a scripted sequence
  (ignition, gear positions, a steering sweep, a drive with speed holds, braking,
  turns), prompting each step and timestamping the button presses. Those presses
  become the `annotations.json` that `import_ccap.py` turns into references.

The point of the extra sensors is that **the hard part of reverse engineering is
usually the reference, not the search**. A recorded drive gives several independent
ones at once — GPS speed, IMU yaw rate, the cluster's own displayed values, and the
operator's markers — so a candidate field can be cross-checked rather than merely
correlated.

Two limits worth stating up front, both learned the hard way:

- **A wireless adapter can drop frames without the file looking wrong.** The CSV
  still parses and every ID is still present, so a search happily returns a
  confident answer computed from whatever survived. Hence the capture-health gate.
- **A phone is not a rigid sensor platform.** Mount vibration adds noise to the
  gyro, and a mount that shifts mid-run defeats a fixed video crop. Both are
  capture-quality problems, not analysis problems, and the tooling reports them
  rather than papering over them.

## Recommended hardware

- A [CANsub.2](https://www.csselectronics.com/products/can-fd-usb-interface-ethernet-cansub-2) CAN FD interface with USB/Ethernet
- An [OBD2-DB9 adapter cable](https://www.csselectronics.com/products/obd2-db9-adapter-cable) (and optionally a [contactless adapter](https://www.csselectronics.com/products/contactless-can-bus-reader-adapter))

<img src="https://www.csselectronics.com/cdn/shop/files/CANsub-can-bus-interface-stream-real-time-webcan.png" alt="CANsub CAN bus interface streaming real-time in webCAN" width="25%"> <img src="https://www.csselectronics.com/cdn/shop/products/OBD2-DB9-Adapter-Cable-CAN-Bus.jpg?v=1625644186" alt="OBD2-DB9 adapter cable" width="20%">

## 1. Get the code and install dependencies

1. **Clone the repo** (or download the ZIP from GitHub and extract it)
2. **Install Python** - [Python 3.10+](https://www.python.org/downloads/); on Windows, tick **"Add python.exe to PATH"**. Verify: `python --version`
3. **Install the dependencies** into a local virtual environment (`.venv`) so your system Python stays untouched:
   - **Windows:** double-click **`install.bat`** (or run `install.bat` in a terminal)
   - **macOS / Linux:** `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`

## 2. Set up Claude Code

The below is our recommended setup for new Claude Code users:

1. **Get a [Claude subscription](https://claude.ai)** - Claude Code is included with [Claude Pro / Max](https://www.claude.com/claude-code) (or API billing)
2. **Install [Visual Studio Code](https://code.visualstudio.com)**
3. **Install the Claude Code extension** - Extensions panel (`Ctrl+Shift+X`), search **"Claude Code"**, install, sign in
4. **Open this folder** - *File → Open Folder…* → the cloned repo; skills in `.claude/skills/` load automatically

## 3. Connect the hardware

Plug the CANsub into your computer (USB) and into the vehicle's OBD2 port using the
OBD2-DB9 adapter cable. Start the engine (or set the ignition on) so there's live
CAN traffic to capture. We recommend verifying via [webCAN](https://www.csselectronics.com/pages/webcan-can-bus-streaming-software-browser) that you can stream raw proprietary CAN bus data before proceeding. If not, consider our [contactless CAN reader](https://www.csselectronics.com/products/contactless-can-bus-reader-adapter). 

## Try it

Open the Claude Code panel in VS Code and ask, for example:

> I've connected my CANsub to my car via the OBD2-DB9 cable. Help me check if there is live proprietary CAN data available - and then help me reverse engineer my door locks

> Reverse engineer Speed and RPM from the proprietary CAN data found in Mercedes-E350-2010-obd2-can.csv (contains OBD2 reference data).

> I have a CANedge log with proprietary vehicle CAN data plus the CANedge's internal GPS/IMU on CAN9 (or a CANmod.gps GPS-to-CAN module). Use the GPS speed as the reference to reverse engineer the proprietary vehicle speed

> Help me reverse engineer Speed from my Opel Astra. I have put the raw CAN data in opel/ along with a video of the speed from my car's dashboard.

> I have a gauge-to-CAN module with 8 gauges connected to my CANsub - help me reverse engineer the 1st gauge position signal.


**Note:** You can use our [CANsub CAN+OBD2 sample data](https://www.csselectronics.com/pages/ai-can-bus-sniffer-data-pack) to test the skill


## Output structure and combining DBCs

Each confirmed signal is saved under `decoding-output/`, grouped by application
(the system under test) and signal:

```
decoding-output/
  <application>/                         e.g. mercedes-e350/
    <signal>/<signal>.dbc                e.g. engine-rpm/engine-rpm.dbc   (one DBC per signal)
    <signal>/<signal>.png                the verify plot (decoded vs reference)
    <signal>/analysis-plots/             survey / correlate / bit-search / fit plots
    <application>.dbc                     the combined DBC across all signals
```

If you've decoded several signals, ask Claude to merge them into one
application DBC (using the **combine-dbc** skill):

> Combine the decoded DBCs for mercedes-e350 into a single DBC.

This produces `decoding-output/<application>/<application>.dbc`. You can load
this combined DBC in [webCAN](https://www.csselectronics.com/pages/webcan-can-bus-streaming-software-browser)
and stream live from your CANsub to see your reverse-engineered signals decoded in
real time - a final, live confirmation of the results.

## License and attribution

These skills are fully open source under the [MIT License](LICENSE) - you are free to use, modify and distribute them in your own projects.

Original work © 2026 CSS Electronics. Modifications in this fork © 2026 Power Gauge Pro LLC, released under the same MIT licence.

If you use them in your projects, videos or blog posts, we'd appreciate a reference to our article: [CAN bus reverse engineering with AI](https://www.csselectronics.com/pages/can-bus-reverse-engineering-ai-llm-claude).
