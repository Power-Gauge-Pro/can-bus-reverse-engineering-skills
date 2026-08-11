# Measuring whether the skill actually got better

A skill like this is easy to improve in ways that feel like progress and are not.
The only honest measurement is a **blind run**: a fresh session that works the same
capture with the updated skill, knowing nothing about what a previous run found.

Self-evaluation does not work here. A session that already knows the answers will
find them, will call that a success, and will not notice which of its steps were
load-bearing. That is not a claim about diligence — it is that the information is
already in context and cannot be un-seen.

## What has to be isolated

An isolated workspace, containing only:

```
capture/<bundle>/        the capture, copied in
.claude/skills/          the skill, at the WORKSPACE ROOT
.venv/                   built from the skill's requirements
TASK.md                  the brief
```

`.claude/` must sit at the root of the workspace, not nested inside a
subdirectory — nested skills are not discovered, and the run silently proceeds
without the thing being tested.

Then confirm the session cannot reach the answers:

- **No reference DBCs** anywhere it can read, and a brief that forbids searching
  the wider filesystem for one.
- **No prior results** — no scores, target lists, or logs from earlier runs.
- **No memories.** Session memory is keyed to the project tree; a workspace outside
  that tree does not inherit it. Verify rather than assume.
- **The skill itself must be clean.** This is the failure that has actually
  happened: the skill's own reference docs contained findings from an earlier run,
  so the "blind" session was reading last time's answers as method. Work through
  `SANITIZATION.md` on the copy being tested, not just the repo.

## Keeping runs comparable

Whatever hints the previous run received mid-flight — a nudge about which
manoeuvre to look at, a caution about a sensor — later runs must receive too, or
the comparison measures the hints rather than the skill. Write them into `TASK.md`
up front instead of offering them interactively, so every run gets the same brief.

The brief should state what "good" means and let the run choose its method. Scoring
on exact geometry, precision, and coverage is enough; prescribing the steps tests
compliance rather than capability.

## Verifying afterwards that it stayed blind

Do not take the report's word for it. The session transcript is checkable:

- file reads resolving outside the workspace,
- any web search or fetch,
- reads of DBC or database files that were not produced by the run,
- verbatim manufacturer signal names appearing without derivation — the strongest
  tell, since an independent run has no way to guess an internal name,
- and positively: that the skill's scripts were actually used, rather than the
  session hand-rolling its own analysis and reporting the skill's results.

## Scoring

Compare produced DBCs against a reference set by **geometry**, not by name — an
independent run has no reason to choose the same names, and matching on names
undercounts real successes. `score_run.py` classifies each signal as exact,
partial, unmatched, or novel.

The **partial** class is the one to watch. A field sharing only the dominant byte
with the true one decodes to nearly the right values, passes correlation, passes
the verify gate, and reads as a win in any summary table. Counting it as a miss is
the whole point of scoring on geometry.

Two cautions on reading the result:

- Scoring geometry means **labels are not checked**. A run can locate a set of
  fields correctly and assign them to the wrong members of a symmetric group; the
  score will not notice. Read the labels before believing a headline.
- **Fewer, better-evidenced signals beat more hopeful ones.** Track precision
  alongside count, or the metric rewards guessing.

## What to do with the result

The valuable output is not the score, it is the **diagnosed misses**. For each
signal the run failed to find, work out which of these it was:

- the search space could not express it (fixable — widen it),
- the method for that *kind* of signal was not applied (fixable — make the skill
  route to it),
- the capture genuinely could not answer it, e.g. the message rate was below what
  the behaviour needed (not fixable in the skill; record it as a known limit),
- the reference was not good enough (fixable in the capture procedure, not here).

Only the first two are skill defects. Distinguishing them is what turns a test into
an improvement.

**Results and scores live outside this repo**, in the project directory — they are
run-specific, and committing them would defeat the next blind test.
