#!/usr/bin/env python3
"""import_ccap.py - convert a can-capture (ccap) mobile-app run into skill inputs.

FORK ADDITION (not upstream). The mobile app exports a run bundle:

    <run>/can.csv          utc,bus,id_hex,ide,rtr,err,dlc,len,dir,fdf,brs,esi,data
    <run>/gps.csv          utc,...,speed_mps,course_deg,...
    <run>/annotations.json {"events": [{utc, kind, label, button_id, step_id, source}]}
    <run>/run.json         started_utc/ended_utc + per-source metadata
    <run>/video.mp4        instrument-cluster video (feed to vision_reference.py)

This emits the two formats the rest of the pipeline already speaks:

  * a webCAN-format trace CSV (';'-separated, the 12 WEBCAN_COLUMNS), and
  * sidecar CSVs (epoch;kind;label;value) built from GPS speed and annotations.

It also prints a CAPTURE HEALTH report. That is not decoration: a run whose CAN
stream collapsed mid-drive will still correlate happily against a reference and
produce a confident, wrong answer, because correlate/bitsearch see only the frames
that survived. Read the health report before trusting any result, and use
--start/--end to restrict analysis to a window where the bus was actually healthy.

Examples:
    python import_ccap.py --run captures/r6/experiments/driving/run-001
    python import_ccap.py --run <run> --start 0 --end 160 --tag steady
    python import_ccap.py --run <run> --gps-unit km/h
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402  (for WEBCAN_COLUMNS - single source of truth)

MPS_TO = {"m/s": 1.0, "mph": 2.2369362920544, "km/h": 3.6}


_UNIX_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


def _epoch(series: pd.Series) -> np.ndarray:
    """ISO8601 UTC strings -> float epoch seconds.

    Subtracting the epoch rather than astype("int64") keeps this correct whatever
    resolution pandas infers from the strings (ours parse as microseconds, so a
    fixed /1e9 would be wrong by 1000x).
    """
    t = pd.to_datetime(series, format="ISO8601", utc=True)
    return (t - _UNIX_EPOCH).dt.total_seconds().to_numpy()


def load_run(run_dir: str) -> dict:
    with open(os.path.join(run_dir, "run.json")) as f:
        run = json.load(f)
    run["_t0"] = pd.Timestamp(run["started_utc"]).timestamp()
    run["_t1"] = pd.Timestamp(run["ended_utc"]).timestamp()
    return run


# ---------------------------------------------------------------- CAN -> trace

def convert_can(run_dir: str, t0: float, start: float | None,
                end: float | None) -> tuple[pd.DataFrame, dict]:
    """Read ccap can.csv and return (webCAN-shaped DataFrame, health stats)."""
    src = pd.read_csv(os.path.join(run_dir, "can.csv"), dtype=str).fillna("")
    need = {"utc", "bus", "id_hex", "ide", "rtr", "err", "dlc", "len", "dir",
            "fdf", "brs", "esi", "data"}
    missing = need - set(src.columns)
    if missing:
        raise ValueError(f"can.csv missing columns: {sorted(missing)}")

    ep_utc = _epoch(src["utc"])
    clock = {"source": "utc", "n_corrected": 0, "offset": None, "excursion": 0.0}

    # Prefer the monotonic clock when the bundle carries one. The wall clock can
    # step mid-run (the app records a `clock_stepped` interruption when it does),
    # which scrambles frame order and corrupts every cycle-time measurement.
    # t_mono_us cannot step, so rebuild the timeline from it and re-anchor to wall
    # time with the MEDIAN offset - that keeps cross-source alignment with GPS,
    # annotations and video while discarding the step.
    if "t_mono_us" in src.columns:
        mono = pd.to_numeric(src["t_mono_us"], errors="coerce").to_numpy(float) / 1e6
        good = np.isfinite(mono)
        if good.sum() > 100:
            off = (ep_utc - t0) - mono
            med = float(np.median(off[good]))
            dev = np.abs(off - med)
            clock = {
                "source": "t_mono_us",
                "n_corrected": int((dev > 1.0).sum()),
                "offset": med,
                "excursion": float(np.nanmax(dev)) if len(dev) else 0.0,
                "monotonic": bool(np.all(np.diff(mono[good]) >= 0)),
                "utc_backsteps": int((np.diff(ep_utc) < 0).sum()),
            }
            ep_utc = t0 + med + mono

    ep = ep_utc
    rel = ep - t0

    n_all = len(src)
    err_mask = src["err"].astype(str).str.strip().isin(("1", "true", "True"))
    n_err = int(err_mask.sum())

    keep = ~err_mask
    if start is not None:
        keep &= rel >= start
    if end is not None:
        keep &= rel <= end

    src = src[keep].reset_index(drop=True)
    ep = ep[keep.to_numpy()]
    rel_kept = ep - t0

    out = pd.DataFrame()
    out["TimestampEpoch"] = [f"{v:.6f}" for v in ep]
    # "bus_1" -> 1; anything unparseable -> 0
    out["BusChannel"] = (
        src["bus"].str.extract(r"(\d+)$", expand=False).fillna("0").astype(int)
    )
    out["ID"] = src["id_hex"].str.upper().str.replace("^0X", "", regex=True)
    out["IDE"] = src["ide"]
    out["DLC"] = src["dlc"]
    out["DataLength"] = src["len"]
    # ccap uses R/T; webCAN uses 0=rx, 1=tx.
    out["Dir"] = np.where(src["dir"].str.upper().str.startswith("T"), "1", "0")
    out["EDL"] = src["fdf"]
    out["BRS"] = src["brs"]
    out["ESI"] = src["esi"]
    out["RTR"] = src["rtr"]
    out["DataBytes"] = src["data"].str.upper()

    assert list(out.columns) == common.WEBCAN_COLUMNS, "column order drifted"

    health = _health(rel_kept, n_all, n_err)
    health["clock"] = clock
    return out, health


def _health(rel: np.ndarray, n_all: int, n_err: int) -> dict:
    """Frame-rate coverage stats - the guard against silently sparse windows."""
    if len(rel) == 0:
        return {"n": 0}
    span = float(rel.max() - rel.min())
    bucket = 10.0
    edges = np.arange(rel.min(), rel.max() + bucket, bucket)
    hist, _ = np.histogram(rel, bins=edges)
    fps = hist / bucket
    peak = float(np.percentile(fps, 95)) if len(fps) else 0.0

    uniq = np.unique(rel)
    gaps = np.diff(uniq) if len(uniq) > 1 else np.array([0.0])
    dead = float(gaps[gaps > 1.0].sum())

    healthy = fps > 0.6 * peak if peak > 0 else np.zeros(len(fps), bool)
    last_ok = float(edges[np.where(healthy)[0].max() + 1]) if healthy.any() else 0.0

    return {
        "n": len(rel), "n_all": n_all, "n_err": n_err,
        "t_min": float(rel.min()), "t_max": float(rel.max()), "span": span,
        "mean_fps": len(rel) / span if span else 0.0,
        "peak_fps": peak, "frames_per_stamp": len(rel) / max(len(uniq), 1),
        "dead_s": dead, "n_gaps": int((gaps > 1.0).sum()),
        "max_gap": float(gaps.max()), "last_healthy": last_ok,
        "edges": edges, "fps": fps,
    }


def print_health(h: dict, run_dur: float, interruptions=None) -> None:
    print("=" * 72)
    print("CAPTURE HEALTH")
    print("=" * 72)
    c = h.get("clock") or {}
    if c.get("source") == "t_mono_us":
        print(f"  timebase           : t_mono_us (monotonic={c.get('monotonic')}), "
              f"anchored to wall clock at +{c['offset']:.4f}s")
        if c["n_corrected"]:
            print(f"  [!] WALL CLOCK STEPPED - {c['n_corrected']:,} frame(s) carried a "
                  f"utc stamp up to {c['excursion']:.1f}s out.")
            print(f"      Rebuilt from the monotonic clock, so those frames are "
                  f"fixed rather than dropped.")
        if c.get("utc_backsteps"):
            print(f"      ({c['utc_backsteps']:,} backward steps in `utc` avoided by "
                  f"not using it.)")
    else:
        print("  timebase           : utc (no t_mono_us column in this bundle)")
    if interruptions:
        print(f"  app-reported outages: {len(interruptions)}")
        for it in interruptions:
            span = ""
            if it.get("from_utc") and it.get("to_utc"):
                a = pd.Timestamp(it["from_utc"]); b = pd.Timestamp(it["to_utc"])
                span = f"  ({(b - a).total_seconds():.1f}s)"
            print(f"      - {it.get('kind','?')}{span}")
    if not h.get("n"):
        print("  NO FRAMES in the selected window.")
        return
    print(f"  frames kept        : {h['n']:,} of {h['n_all']:,} "
          f"({h['n_err']:,} error frames dropped)")
    print(f"  CAN span           : {h['t_min']:.1f}..{h['t_max']:.1f}s "
          f"of a {run_dur:.0f}s run")
    print(f"  mean / peak rate   : {h['mean_fps']:,.0f} / {h['peak_fps']:,.0f} fps")
    fps_stamp = h["frames_per_stamp"]
    note = ("per-frame timestamps - cycle time and jitter are meaningful"
            if fps_stamp < 1.5 else
            "host-side BATCH stamping - survey's period/jit columns are artifacts")
    print(f"  frames per stamp   : {fps_stamp:.1f} ({note})")
    print(f"  gaps > 1s          : {h['n_gaps']} "
          f"(dead {h['dead_s']:.0f}s, worst {h['max_gap']:.1f}s)")

    if h["t_max"] < run_dur - 5:
        print(f"  [!] CAN STOPS {run_dur - h['t_max']:.0f}s BEFORE the run ends - "
              f"the last {(run_dur - h['t_max'])/run_dur*100:.0f}% has no bus data.")
    if h["last_healthy"] < h["t_max"] - 5:
        print(f"  [!] rate collapses after t={h['last_healthy']:.0f}s; "
              f"{h['last_healthy']:.0f}..{h['t_max']:.0f}s is heavily decimated.")
        print(f"      Correlating past t={h['last_healthy']:.0f}s risks a confident "
              f"WRONG answer. Prefer --end {h['last_healthy']:.0f}.")

    print("\n  rate profile (10s buckets, '#' = % of peak):")
    for i, f in enumerate(h["fps"]):
        pct = 100 * f / h["peak_fps"] if h["peak_fps"] else 0
        print(f"    {h['edges'][i]:6.0f}s {f:6.0f} fps {pct:4.0f}% "
              f"{'#' * int(pct / 2.5)}")


# ------------------------------------------------------------ GPS -> sidecar

def convert_gps(run_dir: str, t0: float, unit: str, start: float | None,
                end: float | None) -> pd.DataFrame | None:
    path = os.path.join(run_dir, "gps.csv")
    if not os.path.exists(path):
        return None
    g = pd.read_csv(path)
    if "speed_mps" not in g.columns:
        return None
    # `utc` is when the phone RECORDED the fix; `t_fix_utc` is when the fix was
    # actually taken (~0.3 s earlier here). Using the record time injects a
    # systematic lag into every correlation, so prefer the fix time.
    tcol = "utc"
    if "t_fix_utc" in g.columns and g["t_fix_utc"].notna().all():
        tcol = "t_fix_utc"
    ep = _epoch(g[tcol])
    rel = ep - t0
    v = g["speed_mps"].to_numpy(float) * MPS_TO[unit]

    m = np.isfinite(v)
    if start is not None:
        m &= rel >= start
    if end is not None:
        m &= rel <= end

    return pd.DataFrame({
        "epoch": [f"{x:.6f}" for x in ep[m]],
        "kind": "value",
        "label": "gps_speed",
        "value": [f"{x:.4f}" for x in v[m]],
    })


# ---------------------------------------------------- annotations -> sidecar

# ccap imu.csv is LONG format: one row per sample, a `sensor` column selecting
# which triple is populated.
#   utc,sensor,ax_mps2,ay_mps2,az_mps2,gx_rads,gy_rads,gz_rads
# `sensor` selects which triple is populated; the other three columns are empty.
IMU_RAW = {
    "ax_mps2": "accel X (phone frame)",
    "ay_mps2": "accel Y (phone frame)",
    "az_mps2": "accel Z (phone frame)",
    "gx_rads": "gyro X (phone frame)",
    "gy_rads": "gyro Y (phone frame)",
    "gz_rads": "gyro Z (phone frame)",
}
RAD_TO_DEG = 57.29577951308232


def convert_imu(run_dir: str, run: dict, t0: float, start: float | None,
                end: float | None) -> dict[str, tuple]:
    """Emit IMU reference sidecars: raw phone-frame channels plus derived ones.

    The raw axes are in the PHONE's frame, and the phone is clamped at whatever
    angle the mount happens to sit at, so no single raw axis is "yaw" or
    "longitudinal". The derived channels fix that without needing to know the
    mount angle:

      yaw_rate_dps - the gyro vector projected onto GRAVITY (estimated from the
        accelerometer). Rotation about the vertical axis is yaw whatever way the
        phone is turned. This is the reference STEERING ANGLE needs - but only
        while the vehicle is MOVING; a stationary vehicle does not yaw however
        far you turn the wheel.

    Returns {channel: (DataFrame, note)}.
    """
    name = None
    for key in ("imu", "motion", "sensors"):
        if isinstance(run.get(key), dict) and run[key].get("file"):
            name = run[key]["file"]
            break
    if name is None:
        for cand in ("imu.csv", "motion.csv", "sensors.csv"):
            if os.path.exists(os.path.join(run_dir, cand)):
                name = cand
                break
    if name is None:
        return {}
    path = os.path.join(run_dir, name)
    if not os.path.exists(path):
        return {}

    d = pd.read_csv(path)
    tcol = next((c for c in ("t_sample_utc", "utc", "timestamp") if c in d.columns), None)
    if tcol is None:
        print(f"  [!] {name}: no recognisable time column, skipped")
        return {}
    ep = _epoch(d[tcol])
    rel = ep - t0

    def window(mask):
        m = mask.copy()
        if start is not None:
            m &= rel >= start
        if end is not None:
            m &= rel <= end
        return m

    out: dict[str, tuple] = {}
    for col, note in IMU_RAW.items():
        if col not in d.columns:
            continue
        v = pd.to_numeric(d[col], errors="coerce").to_numpy(float)
        m = window(np.isfinite(v))
        if m.sum() < 3:
            continue
        scale = RAD_TO_DEG if col.endswith("_rads") else 1.0
        out[col] = (pd.DataFrame({
            "epoch": [f"{x:.6f}" for x in ep[m]],
            "kind": "value",
            "label": col,
            "value": [f"{x * scale:.6g}" for x in v[m]],
        }), note + (" [deg/s]" if scale != 1.0 else " [m/s^2]"))

    # --- derived: yaw rate about the gravity axis ---------------------------
    acc_cols = ["ax_mps2", "ay_mps2", "az_mps2"]
    gyr_cols = ["gx_rads", "gy_rads", "gz_rads"]
    if all(c in d.columns for c in acc_cols + gyr_cols):
        A = d[acc_cols].to_numpy(float)
        G = d[gyr_cols].to_numpy(float)
        a_ok = np.isfinite(A).all(axis=1)
        g_ok = np.isfinite(G).all(axis=1)
        if a_ok.sum() > 50 and g_ok.sum() > 50:
            grav = np.median(A[a_ok], axis=0)
            n = np.linalg.norm(grav)
            if n > 1e-6:
                ghat = grav / n
                yaw = (G[g_ok] @ ghat) * RAD_TO_DEG
                gep, grel = ep[g_ok], rel[g_ok]
                m = np.isfinite(yaw)
                if start is not None:
                    m &= grel >= start
                if end is not None:
                    m &= grel <= end
                if m.sum() >= 3:
                    tilt = np.degrees(np.arccos(np.clip(abs(ghat[2]), -1, 1)))
                    out["yaw_rate_dps"] = (pd.DataFrame({
                        "epoch": [f"{x:.6f}" for x in gep[m]],
                        "kind": "value",
                        "label": "yaw_rate_dps",
                        "value": [f"{x:.6g}" for x in yaw[m]],
                    }), f"YAW RATE [deg/s], gravity-projected (mount tilt "
                        f"{tilt:.0f}deg off vertical) - reference for STEERING "
                        f"ANGLE while moving")
    return out


def _read_events(run_dir: str, t0: float, start: float | None,
                 end: float | None) -> list[dict]:
    path = os.path.join(run_dir, "annotations.json")
    if not os.path.exists(path):
        return []
    out = []
    for e in json.load(open(path)).get("events", []):
        ep = pd.Timestamp(e["utc"]).timestamp()
        rel = ep - t0
        if start is not None and rel < start:
            continue
        if end is not None and rel > end:
            continue
        out.append({**e, "_ep": ep, "_rel": rel})
    return out


def convert_annotations(evs: list[dict]) -> pd.DataFrame | None:
    """All annotations as point events (kind=event), for --type discrete."""
    rows = []
    for e in evs:
        kind = e.get("kind", "")
        label = (e.get("button_id") or e.get("label") or "").strip()
        if kind in ("toggle_on", "toggle_off"):
            rows.append((e["_ep"], "event", label, "1" if kind == "toggle_on" else "0"))
        else:
            rows.append((e["_ep"], "event", label, ""))
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["epoch", "kind", "label", "value"])
    df["epoch"] = df["epoch"].map(lambda x: f"{x:.6f}")
    return df


def build_state_series(evs: list[dict], overrides: dict | None = None,
                       initial: dict | None = None) -> dict[str, tuple]:
    """Turn labelled presses/toggles into sample-and-hold `value` references.

    A press like button_id "gear-reverse" is not a point event - it puts the
    vehicle into a STATE that holds until the next press in the same family. The
    upstream skill only knows point events and anchors, so those held states were
    unusable; this reconstructs them.

    Grouping is structural, not vehicle-specific: the family is the button_id up
    to the first '-', the state is the remainder ("gear-reverse" -> family "gear",
    state "reverse"). States are numbered in first-appearance order unless
    `overrides` supplies {family: {state: value}} - the ordering only has to be
    consistent for correlation, but a physically ordered mapping (e.g. steering
    left<centre<right) makes scale/offset meaningful.

    toggle_on/toggle_off pairs become a 1/0 series per label.

    `initial` seeds a state the vehicle was ALREADY in before the first press,
    as {family: (epoch, state)}. This matters more than it looks: a state that
    RECURS (park -> reverse -> ... -> park) is what lets segments.py reject
    rolling counters, and without the seed the opening state is invisible.

    Returns {family: (DataFrame, {state: value})}.
    """
    overrides = overrides or {}
    fams: dict[str, list[tuple[float, str]]] = {}
    toggles: dict[str, list[tuple[float, int]]] = {}
    for fam, (ep, state) in (initial or {}).items():
        fams.setdefault(fam, []).append((float(ep), str(state).lower()))

    for e in evs:
        kind = e.get("kind", "")
        if kind in ("toggle_on", "toggle_off"):
            lab = (e.get("label") or e.get("button_id") or "").strip().lower()
            if lab:
                toggles.setdefault(lab, []).append(
                    (e["_ep"], 1 if kind == "toggle_on" else 0))
            continue
        if kind != "press":
            continue                      # app markers are pacing, not state
        bid = (e.get("button_id") or "").strip().lower()
        if "-" not in bid:
            continue                      # no family -> not a state machine
        fam, state = bid.split("-", 1)
        fams.setdefault(fam, []).append((e["_ep"], state))

    out: dict[str, tuple] = {}

    for fam, items in fams.items():
        items.sort(key=lambda kv: kv[0])
        if len({s for _, s in items}) < 2:
            continue                      # a single state is not a reference
        mapping = dict(overrides.get(fam, {}))
        nxt = max(mapping.values(), default=-1) + 1
        for _, s in items:
            if s not in mapping:
                mapping[s] = nxt
                nxt += 1
        # Emit BOTH kinds. `value` is the sample-and-hold series, right for a
        # state that genuinely persists (gear stays in D until you move it).
        # `anchor` marks the instant the state was reached, which is what a
        # CONTINUOUSLY VARYING quantity needs - between "full left" and "centre"
        # the wheel is sweeping, so holding -1 across that span is simply wrong.
        # Consumers pick: --ref-window uses the anchors, its absence uses the
        # step series.
        rows = [(ep, "value", mapping[s]) for ep, s in items]
        rows += [(ep, "anchor", mapping[s]) for ep, s in items]
        rows.sort(key=lambda r: (r[0], r[1]))
        df = pd.DataFrame({
            "epoch": [f"{ep:.6f}" for ep, _, _ in rows],
            "kind": [k for _, k, _ in rows],
            "label": fam,
            "value": [f"{v:g}" for _, _, v in rows],
        })
        out[fam] = (df, mapping)

    for lab, items in toggles.items():
        if len(items) < 2:
            continue
        df = pd.DataFrame({
            "epoch": [f"{ep:.6f}" for ep, _ in items],
            "kind": "value",
            "label": lab,
            "value": [str(v) for _, v in items],
        })
        out[f"toggle_{lab}"] = (df, {"off": 0, "on": 1})

    return out


# ------------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", required=True, help="path to the run-NNN directory")
    p.add_argument("--out-dir", default="temp-output")
    p.add_argument("--tag", default=None,
                   help="filename tag (default: the run directory name)")
    p.add_argument("--start", type=float, default=None,
                   help="window start, seconds relative to run start")
    p.add_argument("--end", type=float, default=None,
                   help="window end, seconds relative to run start")
    p.add_argument("--gps-unit", default="mph", choices=sorted(MPS_TO),
                   help="unit for the GPS speed sidecar (default mph)")
    p.add_argument("--auto-window", action="store_true",
                   help="clip --end to where the CAN rate last sustained full "
                        "speed (use after seeing the health report)")
    p.add_argument("--state-values", default=None,
                   help='JSON {"family": {"state": value}} to order state '
                        'ordinals physically, e.g. steering left<centre<right')
    p.add_argument("--state-initial", action="append", default=[],
                   metavar="FAMILY=STATE@REL",
                   help="state the vehicle was already in before the first press, "
                        "at REL seconds from run start (e.g. gear=park@55). "
                        "Repeatable. A recurring state is what lets segments.py "
                        "reject counters.")
    p.add_argument("--no-health", action="store_true")
    args = p.parse_args()

    run = load_run(args.run)
    t0, run_dur = run["_t0"], run["_t1"] - run["_t0"]
    tag = args.tag or os.path.basename(os.path.normpath(args.run))
    os.makedirs(args.out_dir, exist_ok=True)

    trace, health = convert_can(args.run, t0, args.start, args.end)

    if args.auto_window and health.get("n") and health["last_healthy"] > 0:
        end = health["last_healthy"]
        if args.end is None or end < args.end:
            print(f"[auto-window] clipping to t<={end:.0f}s "
                  f"(last sustained full-rate bucket)\n")
            args.end = end
            trace, health = convert_can(args.run, t0, args.start, args.end)

    trace_path = os.path.join(args.out_dir, f"trace_{tag}.csv")
    trace.to_csv(trace_path, sep=";", index=False)

    if not args.no_health:
        print_health(health, run_dur, run.get("interruptions"))
        print()

    hj = {k: v for k, v in health.items() if k not in ("edges", "fps")}
    hj["run_duration_s"] = run_dur
    hj["window"] = {"start": args.start, "end": args.end}
    health_path = os.path.join(args.out_dir, f"health_{tag}.json")
    with open(health_path, "w") as f:
        json.dump(hj, f, indent=2)

    print("=" * 72)
    print("WROTE")
    print("=" * 72)
    print(f"  trace   {trace_path}  ({len(trace):,} frames)")
    print(f"  health  {health_path}")

    gps = convert_gps(args.run, t0, args.gps_unit, args.start, args.end)
    if gps is not None and len(gps):
        gp = os.path.join(args.out_dir, f"sidecar_gps_{tag}.csv")
        gps.to_csv(gp, sep=";", index=False)
        vals = gps["value"].astype(float)
        print(f"  gps     {gp}  ({len(gps):,} samples, "
              f"{vals.min():.1f}..{vals.max():.1f} {args.gps_unit})")
        if vals.max() < 5:
            print(f"          [!] barely any motion in this window - useless as a "
                  f"speed reference")

    for chan, (idf, note) in sorted(convert_imu(args.run, run, t0,
                                                args.start, args.end).items()):
        ip = os.path.join(args.out_dir, f"sidecar_imu_{chan}_{tag}.csv")
        idf.to_csv(ip, sep=";", index=False)
        print(f"  imu     {ip}  ({len(idf):,} samples) - {note}")

    evs = _read_events(args.run, t0, args.start, args.end)
    ann = convert_annotations(evs)
    if ann is not None and len(ann):
        ap = os.path.join(args.out_dir, f"sidecar_events_{tag}.csv")
        ann.to_csv(ap, sep=";", index=False)
        print(f"  events  {ap}  ({len(ann)} events, kind=event -> --type discrete)")

    overrides = json.load(open(args.state_values)) if args.state_values else None
    initial = {}
    for spec in args.state_initial:
        try:
            fam_state, rel = spec.split("@")
            fam, state = fam_state.split("=")
            initial[fam.strip().lower()] = (t0 + float(rel), state.strip().lower())
        except ValueError:
            print(f"ERROR: --state-initial must be FAMILY=STATE@REL, got {spec!r}",
                  file=sys.stderr)
            return 2
    states = build_state_series(evs, overrides, initial)
    for fam, (df, mapping) in sorted(states.items()):
        sp = os.path.join(args.out_dir, f"sidecar_state_{fam}_{tag}.csv")
        df.to_csv(sp, sep=";", index=False)
        order = ", ".join(f"{s}={v:g}" for s, v in
                          sorted(mapping.items(), key=lambda kv: kv[1]))
        print(f"  state   {sp}  ({len(df)} transitions; {order})")

    if states:
        print()
        print("  NEXT — run the categorical search against EVERY state sidecar above.")
        print("  A held state or an indicator will not be found by correlate/bitsearch:")
        print("  those fit a line, and a code point has no line to fit.")
        for fam in sorted(states):
            sp = os.path.join(args.out_dir, f"sidecar_state_{fam}_{tag}.csv")
            print(f"    python scripts/segments.py --trace {trace_path} "
                  f"--sidecar {sp}")
        print("  For an on/off condition (a lamp or indicator), add --mode indicator;")
        print("  if the real thing blinks, add --flash-hz <rate> so IDs too slow to")
        print("  show it are named - a negative on those proves nothing.")

    vid = os.path.join(args.run, run.get("video", {}).get("file", "video.mp4"))
    if os.path.exists(vid):
        v_start = run.get("video", {}).get("started_utc")
        ep = pd.Timestamp(v_start).timestamp() if v_start else t0
        print(f"\n  video   {vid}")
        print(f"          vision_reference.py --video {vid} --start-epoch {ep:.3f}")
        print(f"          (portrait file, landscape cluster - pre-rotate first, "
              f"see references/ccap-format.md)")
        if run.get("video", {}).get("has_audio"):
            print(f"          transcribe_audio.py --video {vid} --start-epoch {ep:.3f}")
            print(f"          (spoken narration -> events; expect ZERO if nobody "
                  f"talked, and do not relax its thresholds to get some)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
