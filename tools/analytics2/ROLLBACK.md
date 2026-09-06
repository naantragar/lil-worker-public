# Rollback points for the report pipeline

`run.py` snapshots itself, `rules.md` and the glossary into `.versions/<YYYYMMDD>-<hash8>/` on every
run, and prints the id as `версія правил:` on the first line of the run and in the report header.
So any shipped report can be traced to the exact three files that produced it, and any of them can
be restored.

## ★ Known-good baseline — `20260831-98910151`

The state in which the 31.08 report was built, accepted by the owner, and shipped with the footer as
`РЕР_31.08.2026.docx`. It contains the log-truncation fix (readable network names, full dropped-
callsign lists, untruncated archive headers in `_dubli.txt`) and nothing after it.

**This is the point to return to if the stable-source-reference work (started 2026-08-31) turns out
badly.** Restore:

```
cp tools/analytics2/.versions/20260831-98910151/run.py       tools/analytics2/run.py
cp tools/analytics2/.versions/20260831-98910151/rules.md     tools/analytics2/rules.md
cp tools/analytics2/.versions/20260831-98910151/10_glossary.md tools/analytics2/10_glossary.md
```

Then re-render any affected day with `--render-only` (no model time):

```
python3 tools/analytics2/run.py --day 2026-08-31 --render-only
```

A rollback is safe for the saved events: `<out>_events.json` from a newer run stays readable by the
older code — the newer code only ADDS fields (`_src_ref`), and the old renderer ignores unknown
keys. What is lost on rollback is the source index (`<out>_sources.json`) and the seam-duplicate
merge, not any event.

## Current state — `20260901-d54223a5` (stable source references)

What the baseline gained: every event carries `_src_ref`, the stable addresses of the intercepts it
rests on (`DD.MM HH:MM:SS FREQ`), resolved from the model's chunk-local indices the moment an
answer returns; a companion `<out>_sources.json` holding those intercepts verbatim; and
`merge_by_source()`, which collapses the duplicate an overlapping chunk seam produces.

Measured before shipping, on the 85 events of the 31.08 report: with all four gates only three
pairs qualify, and all three are genuine duplicates (ГРИБАН's trench twice, the МУК shelter twice,
БОРЗЫЙ `через т БОБА` / `повз т БОБА`). The known trap — МУК's move and СОВА's move, which share an
intercept and score 0.80 on text — is correctly refused.

## `20260902-e33050e0` — captured comms equipment is its own line

Rules only, no code. §12 gained «Захоплення НАШИХ засобів зв'язку» (a separate, urgent line, no
named radio type required), §9 gained the requirement to name WHOSE the losses are in a clash line,
and the glossary gained `рдшка`/`радейка`/`замародерити`, `20` as spoken «200», `по чистоте`, and
`воробушек` as the escort UAV of the 445.5000 net rather than a callsign.

Verified on the real clash of 02.09 11:18 (a 45-minute re-run): the clash line came back as
`бойове зіткнення гр РЕЗВЫЙ - 2 в\с СОУ 200 (ім)` instead of the ambiguous `з 2 в\с СОУ - 2 200`,
and the capture appeared as its own line — `гр РЕЗВЫЙ вилучила 2 р\с («Рдшки») у 2 в\с СОУ 200`.
The shipped 02.09 report predates this and does not contain that second line.

## ★ Composed header is now the DEFAULT (2026-09-02, `--layout composed`)

The block header is no longer the most frequent raw variant of the day. It is composed from ALL the
variants of that cluster by union — every subunit any of them named, the division, the army, the
direction built from the settlements they mention ordered by how often, the `трофей` marker last.
Same report shape as always (frequency line, header, register, events); only the header changed.

Why: the old name was elected daily by a majority that shifts, so one net was called three different
things in a week and continuity broke. A union does not vote — it moves only when the attributions
themselves move. Deliberately NOT a model call: that would re-decide the wording every day, which is
the same instability with a bill attached.

A cluster holding two formations now says so («… 60 омсбр 5 А + шг 38 омсбр 35 А») instead of
printing whichever was more frequent — that is the frequency clustering having welded two formations
together, and it should be visible until the analyst rules on the shared channels.

To go back: `--layout current` restores the old elected header exactly.

## Layout trial (2026-09-02, `--layout`) — the other three modes, not default

`--layout current|denominator|registry` picked the block header and grouping. `current` is the old
behaviour bit-for-bit and stays the default, so nothing ships differently until we choose. The other
two are pure presentation (`apply_layout()`), and combine with `--render-only`, so all three
versions of one day are built from ONE events file in seconds and differ by nothing but the rule.

Baseline of the day under comparison: `reports_out/.baseline_02.09/` (the shipped 02.09 report and
its footer copy). Trial output: `LAYOUT_denom.*`, `LAYOUT_reg.*`.

To abandon the trial: drop the `--layout` argument and `apply_layout()`, and restore the two lines
in `build_report()` that print the frequency line and the net name. Nothing else references it.

## Earlier points

- `20260831-8cc98690` — the same rules, before the log-truncation fix. No reason to go back this far.
- Older ids under `.versions/` correspond to the register/footer work of 24-30.08.
