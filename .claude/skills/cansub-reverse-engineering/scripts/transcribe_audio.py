#!/usr/bin/env python3
"""transcribe_audio.py - turn spoken narration into timestamped annotations.

FORK ADDITION (not upstream).

An operator narrating a run ("braking now", "shifting to reverse") is a better
annotation source than buttons: speech is hands-free, so it can be given while
driving, and it lands closer to the event than a press does. This transcribes the
capture's audio locally and emits the same sidecar format the rest of the pipeline
consumes.

    THE FAILURE MODE THAT MATTERS

Speech recognition does not return "nothing" on non-speech audio - it INVENTS.
Road and engine noise reliably produces confident, fluent, entirely fabricated
sentences, most notoriously stock phrases from the training data ("Thanks for
watching", "Subscribe to the channel"). On a capture where nobody spoke, a naive
transcription yields annotations that look perfectly plausible and are pure
fiction. Fabricated references are worse than no references: they will correlate
with something, and the result gets believed.

So the defaults here are deliberately strict, and every rejection is reported:

  * voice-activity detection gates the audio before recognition (--no-vad to
    disable, but read the count it prints before you do),
  * segments are dropped on the model's own no-speech probability and on average
    token log-probability,
  * a blocklist catches the stock hallucination phrases, which are fluent enough
    to pass both of the above,
  * the summary states how many segments were rejected and why, so silence looks
    like silence rather than like a clean transcript.

If a run genuinely has no narration, the correct output is ZERO events, and this
should say so plainly.

Alignment: audio is extracted from the video, so it shares the video's timebase.
Pass --start-epoch from `run.video.started_utc` exactly as with vision_reference.py.

Examples:
    python transcribe_audio.py --video <run>/video.mp4 --start-epoch <video.started_utc>
    python transcribe_audio.py --video ... --start-epoch ... --model small.en
    python transcribe_audio.py --video ... --start-epoch ... --words --map phrases.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# Stock phrases speech models emit on non-speech audio. They are fluent, score
# well, and are always fabricated in this context.
HALLUCINATION_PATTERNS = [
    r"thanks? for watching", r"subscribe", r"like and share", r"see you in the next",
    r"^\s*you\s*$", r"^\s*thank you\.?\s*$", r"^\s*bye\.?\s*$", r"^\s*\.+\s*$",
    r"amara\.org", r"transcri(bed|ption) by", r"^\s*\[.*\]\s*$", r"^\s*music\s*$",
    r"^\s*applause\s*$", r"^\s*foreign\s*$", r"^\s*okay\.?\s*$",
]


def looks_hallucinated(text: str) -> str | None:
    t = text.strip().lower()
    if not t:
        return "empty"
    for pat in HALLUCINATION_PATTERNS:
        if re.search(pat, t):
            return f"stock phrase (~{pat})"
    if len(t) < 3:
        return "too short to be a usable annotation"
    return None


def extract_audio(video: str, out_wav: str) -> None:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ERROR: ffmpeg not found on PATH; it is needed to extract audio.")
    cmd = ["ffmpeg", "-v", "error", "-i", video, "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", out_wav, "-y"]
    subprocess.run(cmd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", required=True, help="video (or audio) file to transcribe")
    ap.add_argument("--start-epoch", type=float, required=True,
                    help="epoch seconds of media t=0 (run.video.started_utc)")
    ap.add_argument("--model", default="base.en",
                    help="faster-whisper model: tiny.en/base.en/small.en/medium.en. "
                         "Larger is slower but hallucinates less on noisy audio.")
    ap.add_argument("--label", default="speech", help="sidecar label")
    ap.add_argument("--out", default=None)
    ap.add_argument("--transcript", default=None,
                    help="human-readable timestamped transcript (default alongside --out)")
    ap.add_argument("--no-vad", action="store_true",
                    help="disable voice-activity gating (raises hallucination risk a lot)")
    ap.add_argument("--max-no-speech", type=float, default=0.5,
                    help="reject a segment whose no-speech probability exceeds this")
    ap.add_argument("--min-logprob", type=float, default=-1.0,
                    help="reject a segment below this average token log-probability")
    ap.add_argument("--words", action="store_true",
                    help="emit one event per WORD rather than per segment; a spoken cue "
                         "lands on its final word, so this times events more precisely")
    ap.add_argument("--map", dest="mapping",
                    help='JSON {"regex": "label"} turning matched phrases into '
                         'kind=event rows with that label, e.g. {"brak|break": "brake"}. '
                         'Write TOLERANT patterns: recognition returns homophones and '
                         'near-misses ("braking"->"breaking"), and a strict pattern '
                         'drops the event silently.')
    ap.add_argument("--keep-rejected", action="store_true",
                    help="also write rejected segments to the transcript, marked")
    args = ap.parse_args()

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("ERROR: faster-whisper is not installed.\n"
              "  Install with:  .venv/bin/pip install faster-whisper\n"
              "  It runs locally on CPU via CTranslate2 and needs no PyTorch; the model "
              "downloads on first use.", file=sys.stderr)
        return 2

    out = args.out or os.path.join("temp-output", f"sidecar_{args.label}.csv")
    transcript = args.transcript or os.path.splitext(out)[0] + "_transcript.txt"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    tmpdir = tempfile.mkdtemp(prefix="ccap_audio_")
    wav = os.path.join(tmpdir, "audio.wav")
    try:
        print(f"Extracting audio from {os.path.basename(args.video)} ...")
        extract_audio(args.video, wav)

        print(f"Loading model '{args.model}' (downloads on first use) ...")
        model = WhisperModel(args.model, device="cpu", compute_type="int8")

        print(f"Transcribing (vad={'off' if args.no_vad else 'on'}) ...")
        segments, info = model.transcribe(
            wav, beam_size=5, word_timestamps=True,
            vad_filter=not args.no_vad,
            condition_on_previous_text=False,   # stops one hallucination seeding more
        )
        segments = list(segments)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    mapping = json.load(open(args.mapping)) if args.mapping else {}

    accepted, rejected = [], []
    for s in segments:
        text = s.text.strip()
        why = None
        if s.no_speech_prob > args.max_no_speech:
            why = f"no-speech prob {s.no_speech_prob:.2f}"
        elif s.avg_logprob < args.min_logprob:
            why = f"low confidence {s.avg_logprob:.2f}"
        else:
            why = looks_hallucinated(text)
        (rejected if why else accepted).append((s, text, why))

    rows, unmapped = [], []
    for s, text, _ in accepted:
        if mapping and not any(re.search(p, text, re.I) for p in mapping):
            unmapped.append(text)
        if args.words and s.words:
            for w in s.words:
                wt = w.word.strip()
                if wt:
                    rows.append((args.start_epoch + w.start, "event",
                                 args.label, wt))
        else:
            rows.append((args.start_epoch + s.start, "event", args.label, text))
        for pat, lab in mapping.items():
            if re.search(pat, text, re.I):
                # a cue lands on the END of the phrase that names it
                rows.append((args.start_epoch + s.end, "event", lab, text))

    rows.sort(key=lambda r: r[0])
    with open(out, "w") as f:
        f.write("epoch;kind;label;value\n")
        for ep, kind, lab, val in rows:
            f.write(f"{ep:.6f};{kind};{lab};{val}\n")

    with open(transcript, "w") as f:
        f.write(f"# transcript of {args.video}\n")
        f.write(f"# media t=0 is epoch {args.start_epoch:.6f}\n")
        f.write(f"# times below are MEDIA-RELATIVE seconds\n\n")
        for s, text, why in sorted(accepted + (rejected if args.keep_rejected else []),
                                   key=lambda x: x[0].start):
            tag = "" if why is None else f"   [REJECTED: {why}]"
            f.write(f"[{s.start:8.2f} - {s.end:8.2f}] {text}{tag}\n")

    print()
    print("=" * 72)
    print("TRANSCRIPTION")
    print("=" * 72)
    print(f"  language           : {info.language} (p={info.language_probability:.2f})")
    print(f"  segments accepted  : {len(accepted)}")
    print(f"  segments rejected  : {len(rejected)}")
    for _, text, why in rejected[:8]:
        print(f"      - {why:<34} {text[:36]!r}")
    if len(rejected) > 8:
        print(f"      ... {len(rejected) - 8} more")
    print(f"  events written     : {len(rows)}")
    print(f"  sidecar            : {out}")
    print(f"  transcript         : {transcript}")

    if unmapped:
        print()
        print(f"  {len(unmapped)} accepted segment(s) matched NO --map pattern and became")
        print("  generic narration only. Recognition returns homophones, so check these")
        print("  before assuming the operator did not say the thing you mapped:")
        for t in unmapped[:6]:
            print(f"      - {t[:60]!r}")
        if len(unmapped) > 6:
            print(f"      ... {len(unmapped) - 6} more")

    if not accepted:
        print()
        print("  NO SPEECH ACCEPTED. If nobody narrated this run that is the correct")
        print("  result, and the empty sidecar is the honest one - do not lower the")
        print("  thresholds to manufacture events. If you expected narration, check")
        print("  the audio is not silent (ffmpeg volumedetect) before relaxing them.")
    else:
        print()
        print("  Speech timing is a HUMAN reference: a spoken cue trails the event it")
        print("  describes, so keep the lag search enabled and expect the bus")
        print("  transition to come FIRST. Read the transcript before using these as")
        print("  references - recognition errors on noisy in-cabin audio are common,")
        print("  and a misheard phrase becomes a mistimed annotation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
