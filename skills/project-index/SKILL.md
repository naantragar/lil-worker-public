---
name: project-index
description: Build or refresh a compact, evidence-backed "living memory" for a repository — six primitive docs (REQ, CONTEXT, STATE, TDD, DESIGN, DECISIONS) produced by parallel read-only discovery agents plus an adversarial verifier. Use when asked to index/map a repo, bootstrap or recover project context, or refresh stale primitives after big changes. Great before starting real work in an unfamiliar or drifted project.
user-invocable: true
---

# Project Index

Give a repository a normal, current source of truth — not a README that last told the
truth eight months ago. Three read-only discovery agents draft evidence-backed sections,
one adversarial verifier tries to disprove them, and you (the calling agent) integrate
ONLY the survivors into six non-overlapping docs.

The orchestration lives in `index_workflow.js` (this skill's dir). It maps the Codex-native
Dale Index onto this runtime: `Workflow` parallel agents instead of visible Codex threads.

## Six primitives (distinct, non-overlapping — never duplicate a fact, link to its owner)
| File | Owns | Nature |
|---|---|---|
| `REQ.md` | product intent, users, scope, requirements, acceptance criteria | durable |
| `CONTEXT.md` | architecture, repo map, contracts, constraints, operations | durable |
| `STATE.md` | current objective, active work, blockers, risks, next actions | **volatile** |
| `TDD.md` | test strategy, commands, quality gates, missing coverage | durable |
| `DESIGN.md` | visual language, tokens, states, accessibility (or "no UI" + evidence) | durable |
| `DECISIONS.md` | dated decisions, alternatives, evidence, consequences, status | **durable** |

## How to run

1. **Resolve the target repo** (absolute path). Confirm it with the user if ambiguous.
   Output goes to `<repo>/.project-index/` (create it) unless the user names another dir.
2. **Launch the workflow.** It is multi-phase (3 discovery + 1 verify + 1 synthesize = 5
   agents) — per the durable-jobs rule, run it as a **durable job**, not inline, so it
   survives the turn and never hits the 30-min turn cap:
   ```
   echo '{"repo":"<absolute repo path>"}' > /tmp/pidx-args.json
   python3 tools/workflow_job.py launch --script skills/project-index/index_workflow.js \
       --args-file /tmp/pidx-args.json --label project-index
   ```
   (For a TINY repo where you want the result in-turn, an inline `Workflow` with the same
   script is acceptable — but default to the durable job.)
3. **The workflow writes the six docs itself** (its synthesis agent writes to
   `<repo>/.project-index/*.md` — a bounded local-doc exception; nothing is pushed/deployed/
   committed). It RETURNS `{ docs:{written,unknowns,summary}, verifier, discovery }`.
4. **Validate before declaring done** (Dale's checklist):
   - all six files exist together; no TODO/placeholder markers, fake dates, or invented owners;
   - referenced commands/paths exist or are labelled unverified;
   - `REQ` acceptance criteria map to `TDD` checks where evidence exists;
   - `CONTEXT` and `DECISIONS` don't disagree; `STATE` is current state, not a roadmap;
   - `DESIGN` reflects the real UI surface or explicitly records non-applicability.
   Run a narrow non-mutating command only to verify a specific material claim — never broad
   builds/test-suites just for indexing.
5. **Report:** which primitives were written, what the verifier REJECTED, what stays UNKNOWN,
   and the exact checks you ran.

## Discipline (why this is trustworthy, not decorative)
- Discovery agents are strictly read-only and label every claim VERIFIED/INFERRED/UNKNOWN/STALE
  with a source pointer; code/tests/schemas outrank prose.
- The verifier is adversarial — it does not accept a claim just because reports repeated it;
  its FINAL_GATE + reject/revise lists gate what reaches the docs.
- Unknowns and conflicts are stated, never guessed away.

## Keeping it fresh
Re-run after substantial changes, or schedule a periodic refresh (e.g. the `schedule` skill /
a cron) so `STATE.md` especially stays current. Refresh updates the existing set in place —
never create a second competing set.
