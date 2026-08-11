# Working on this repo

This is a **public fork** of [CSS-Electronics/can-bus-reverse-engineering-skills]
(https://github.com/CSS-Electronics/can-bus-reverse-engineering-skills), MIT
licensed. It is a Claude Code skill for reverse engineering CAN bus data into a DBC.
Fork changes are by Power Gauge Pro LLC.

Read this before changing anything. The rules below are not style preferences —
each one is here because breaking it caused a real problem.

## The one rule that matters most

**Nothing vehicle-specific, run-specific, or finding-specific goes in this repo.**
Not in code, comments, docs, examples, test fixtures, or commit messages.

Two separate harms, and they point the same direction:

1. **Publication.** The repo is public. Captures are recorded in real vehicles and
   contain location traces, video of a cabin, and audio.
2. **Contamination.** This skill is meant to be pointed at *any* vehicle. A note
   recording what was found last time is an answer key. It biases the next run
   toward confirming it, and it silently invalidates any attempt to measure whether
   the skill is actually getting better.

Concretely, none of this belongs here: make/model/year, manufacturer or protocol
family names used as examples, CAN IDs or bit positions from a real bus, decoded
signal names or scales, capture paths, run IDs, real epoch timestamps, or prose
describing what a particular run showed.

**Vehicle work lives outside the repo**, in the parent project directory. Target
lists, results logs, per-vehicle state configs, captures, and score files are
already in `.gitignore` — leave those entries alone.

Generic is not the same as vague. The skill *should* know about every encoding
pattern anyone might plausibly implement — byte orders, sign conventions, BCD,
Gray code, counters, checksums, multiplexors, unit families. That knowledge is
mechanism, not answers, and more of it is better. What it must never carry is what
one manufacturer did on one bus.

**Before pushing, work through `docs/SANITIZATION.md`.** A grep is not sufficient
and has already missed leaks — the checklist explains why.

**Your own memory is a contamination source.** Session memories in this project
tree record real findings. They are fine for reasoning; they must never be
paraphrased into a repo file. When writing docs, source examples from the mechanism
being described, never from what a capture happened to show.

## Design principles

These explain why the fork's additions look the way they do. Read
`docs/DESIGN-PRINCIPLES.md` before adding a feature.

The short version: **the enemy is confident wrong output.** Every stage of CAN RE
will happily produce a plausible answer from bad input — a degraded capture still
correlates, a rolling counter passes smoothness tests, OCR misreads a digit,
speech recognition invents sentences on silence. So every component must report its
own reliability, and a result that cannot be trusted must be *loud*, not silent.

## Layout and conventions

```
.claude/skills/cansub-reverse-engineering/   the skill (SKILL.md + scripts + references)
.claude/skills/cansub-knowledge/             upstream reference skill
.claude/skills/combine-dbc/                  upstream DBC merge skill
docs/                                        fork docs (this file's companions)
requirements.txt                             superset, used by install.bat
```

- Scripts run from the **repo root** via `.venv/bin/python`, and write
  `temp-output/` and `decoding-output/` relative to the working directory.
- There are **two** requirements files — the root superset and the skill's own.
  A new dependency goes in both.
- Dependencies must be **local, offline-capable, and PyTorch-free.** The skill
  already avoids PyTorch for OCR (`rapidocr`) and speech (`faster-whisper` via
  CTranslate2); a multi-gigabyte torch pull would make the skill impractical to
  install. Note which system binaries are needed (`ffmpeg` for the media scripts).
- Shared helpers live in `scripts/common.py`. Bit extraction, field enumeration,
  interpretations, unit families and capture-health checks are all there — use
  them rather than re-implementing, because ad-hoc extraction is how the search
  space silently narrows.

## Adding or changing a script

1. Put shared mechanism in `common.py`, not in the script.
2. Make it report its own failure modes in its own output. If a result can be
   junk, the script says so where the user is already looking.
3. Extend `scripts/selftest_geometry.py` if the change pins a contract that could
   regress silently.
4. Test **both directions**: it finds what is there, *and* it stays quiet when
   nothing is there. The second half is the one that gets skipped, and it is the
   half that catches fabrication.
5. Document it in three places, or it does not exist: `SKILL.md` (the script
   inventory and the relevant workflow section), `references/` where it concerns
   the capture format, and the README's fork table.
6. Run the regression: `selftest_geometry.py`, plus the pipeline scripts against a
   real capture from outside the repo.
7. Sanitize (`docs/SANITIZATION.md`), then commit.

## Git

- `master` → `origin/master`, the public fork. This is what you push.
- `upstream-base` → `upstream/master`, for diffing against CSS Electronics.
- `private-run1-history` is a **local archive containing vehicle data. Never push
  it.** It exists only as a record of early work and should stay local.

Keep the diff against upstream **purely additive** where practical. Upstream's
README, license and existing scripts stay intact; the fork's changes are marked as
fork additions so a reader can tell whose work is whose. Verify with
`git diff upstream/master --stat` that you are not removing upstream content.

Commit messages explain **why** a change was needed — the failure it prevents —
not just what moved. They are public and permanent, so the leak rules apply to
them too. A commit cannot be un-leaked without a history rewrite.

## Editing pitfalls that have actually bitten

- **`Edit` anchors match substrings.** An edit keyed on `## This is a fork` matched
  inside `### This is a fork` and destroyed 46 lines of README. After any doc edit,
  check `git diff --stat`: if a change meant to add content reports removed lines,
  stop and look at them.
- **Timestamp resolution.** Pandas parses microsecond stamps at `us` resolution, so
  `astype("int64") / 1e9` is wrong by 1000×. Subtract a
  `Timestamp("1970-01-01", tz="UTC")` instead.
- **`numpy` 2.x removed the `.ptp()` method.** Use `np.ptp(x)`.
- **Real example values leak.** An epoch or path copied from a working session into
  a docstring is a leak. Use `<placeholders>`.
