---
name: adversarial-diff-review
description: Adversarially review your OWN uncommitted diff with a swarm before shipping — independent finders split by dimension, then refute-first verification of every finding; keep only confirmed blockers. Use after a non-trivial code change, especially UI/CSS/stateful/infra work, because the bugs it catches compile, typecheck and lint perfectly clean. Not for trivial or already-verified changes.
user-invocable: true
args:
  - name: scope
    description: Path/dir the diff touches, or a note on what changed (optional; defaults to the working-tree diff)
    required: false
---

Turn a swarm loose on the change you just wrote, from perspectives you don't hold while writing. The
premise: the dangerous bugs in UI/CSS, cascade/specificity, React state, z-index/stacking, and infra
glue **build, typecheck and lint clean** — they only surface under hostile reading. You are biased
toward your own diff; independent finders + refute-first verifiers remove that bias.

Use it when the change is non-trivial and about to ship (commit/deploy). Skip it for trivial edits,
pure docs, or a change you already exercised end-to-end.

## Method

1. **Capture the diff.** `git diff` (+ `git status` for new files). Read it as the input the swarm
   must attack — not "confirm it's fine", but "find where it breaks."

2. **Split into 3–5 dimensions (lenses).** Pick the ones that fit the change, e.g.:
   - *regression* — does it silently alter behaviour the change was supposed to leave intact? (grep
     for every consumer of the shared file/token/selector you touched)
   - *mechanics* — the core new logic under its real constraints (cascade order & specificity for
     CSS; effect deps & stale closures for React; stacking context for z-index; transactions for DB)
   - *a11y / desktop / edge* — the path you didn't manually try
   - *real-device / real-runtime* — "walk it on an actual phone / actual browser / actual account"
   - *data / API contract* — request/response shape vs the backend, idempotency, permission gates

3. **Fan out finders (parallel), one per lens.** Each gets the diff, is told to attack only its
   lens, cite `file:line`, and return an empty list if the code is correct — **do not manufacture
   findings.** A workflow pipeline fits: `pipeline(lenses, finder, verify)`.

4. **Verify every non-minor finding, refute-first.** For each claim spawn 1–3 skeptics whose job is
   to **prove it WRONG** — a parent rule, cascade order, a React guarantee, an existing guard may
   already handle it. Default the verdict to *not real* unless confirmed from the actual code. Kill
   anything a majority refutes.

5. **Report only what survived**, most-severe first, each with a concrete failure scenario
   (inputs/state → wrong output). Drop minor/style noise. Then **fix the confirmed blockers yourself**
   and re-run if the fixes were themselves non-trivial.

## Rules that make it work (learned, load-bearing)

- **Behavioural symptom → reproduce before theorising.** If the report is "it feels stuck / frozen /
  doesn't update", stand up the real thing (headless browser, real runtime) and MEASURE — don't
  reason about the cause. (A "frozen map" was a panel covering it; only a real-browser probe showed
  `elementFromPoint` hit the panel, not the canvas.)
- **Finders must be allowed to find nothing.** Manufactured findings waste the verify pass and erode
  trust in the swarm. Empty is a valid, good result.
- **Refute-first, not confirm-first.** A verifier told to "check if real" rubber-stamps; one told to
  "refute, default to false" kills plausible-but-wrong claims.
- **The clean build is the point, not reassurance.** Every real blocker this method has caught passed
  `tsc`/`lint`/`build`. "It compiles" is not evidence it's correct.
- **Verify is bias removal, not a second opinion from you.** Run the finders as separate agents;
  self-review from the same head that wrote the diff reproduces the same blind spots.

Scope note: `{{scope}}` — if given, point the finders at that path/area; else review the full
working-tree diff.
