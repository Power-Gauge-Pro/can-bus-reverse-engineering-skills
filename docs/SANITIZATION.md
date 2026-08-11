# Sanitization checklist

Run this before every push. The repo is public, and the skill is meant to be
pointed at vehicles it has never seen — both reasons demand that nothing specific
to one vehicle, one capture, or one set of results survives into it.

## Why a grep is not enough

The first audit of this repo grepped for identifiers: vehicle names, signal names,
CAN IDs, file paths. It came back clean. The repo still leaked, because **the worst
leaks are prose**, and prose has no distinctive tokens to match:

- a reference doc that stated which of the two video rotations was "the" correct
  one — true for one phone mount, wrong guidance for everyone else, and an
  unmistakable fingerprint of one setup;
- an observation that the mount drifted partway through, phrased as a general
  caution but describing one specific run;
- a worked example whose steps were the exact sequence one operator drove;
- an assertion about what a particular OCR region contained.

None of those contain a searchable identifier. All of them are run-specific, and
the ones phrased as advice are the most damaging, because a future run reads them
as method and follows them.

**So the check is a read, not a grep.** Grep first to catch the easy cases, then
read every changed line and ask the question below.

## The question to ask of every line

> Would this sentence still be true, and still be useful, for a vehicle nobody here
> has ever tested — and for a completely different capture setup?

- **Yes** → it is mechanism. Keep it.
- **No** → it is a finding. It belongs in the project directory outside this repo.
- **"It's true for ours"** → that is a no.

The reliable tell is a definite article where a variable belongs: "the cluster is
landscape", "the correct rotation", "the main bus". Mechanism says *if*, *when*,
*check which*; findings say *the*.

## Categories that must not appear

| category | examples |
|---|---|
| vehicle identity | make, model, year, platform or protocol-family names used as examples |
| bus findings | CAN IDs, bit offsets, lengths, scales, decoded signal names, frame rates from a real bus |
| capture identity | run IDs, bundle or capture paths, real epoch timestamps, adapter serials |
| results | scores, coverage percentages, counts of what was decoded, comparisons between runs |
| setup specifics | mount orientation, video rotation, OCR regions, which sensors worked |
| narrative | what one drive did, which manoeuvre revealed what, which run failed and how |

Manufacturer names are fine in one place only: describing that a *format* or
*standard* exists. They are not fine as examples of what to look for.

## Procedure

```bash
# 1. what changed
git status --porcelain
git diff --stat                      # a doc edit that REMOVES lines is a red flag

# 2. cheap pass — identifiers (extend the pattern for your vehicle/run names)
{ git diff; git diff --cached; cat <new files>; } \
  | grep -inE "<make>|<model>|<platform>|captures?/|run-[0-9]{3}|0x[0-9A-F]{3}\b|[0-9]{10}\.[0-9]"

# 3. the pass that matters — READ every changed line against the question above
git diff
```

Then confirm the structural rules still hold:

```bash
git ls-files | grep -E "decoding-output|temp-output|captures|TARGETS|baseline|config/"   # must be empty
git diff upstream/master --stat | tail -1                                               # additive; no upstream content removed
git log origin/master..HEAD --format='%s%n%b'                                           # messages are public too
```

## Scope: it is not only the docs

Leaks have appeared in every one of these, so check all of them:

- **docstrings and `--help` text** — an example command copied from a live session
  carries a real path and a real epoch;
- **default argument values** — a threshold tuned to one capture, hard-coded;
- **test fixtures and self-tests** — a "known good" value taken from real data;
- **commit messages** — public, permanent, and not fixable without a history
  rewrite, which is why sanitizing *before* commit matters more than after;
- **the README and SKILL.md** — the most-read files, so the most damaging place for
  a finding disguised as guidance.

## If you find a leak

- **Not yet committed** — fix it and move on.
- **Committed, not pushed** — amend or rebase it out. Cheap; do it now.
- **Already pushed** — tell the user. Rewriting published history is their call,
  and if the content is sensitive, deletion alone is not sufficient because forks
  and caches persist. Do not quietly patch it forward and leave the history.

## Keeping the repo clean by construction

The best defence is that vehicle work never enters the tree at all. `.gitignore`
already excludes `captures/`, `TARGETS.md`, `baseline/`, `config/`,
`decoding-output/`, `temp-output/` and media files. When adding a workflow that
produces per-vehicle artifacts, add its output path there too, and default the
script to writing under one of the existing ignored directories rather than
somewhere new.
