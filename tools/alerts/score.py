#!/usr/bin/env python3
"""Score a bench run against the hand-written ground truth.

    python3 tools/alerts/score.py knowledge/upstream/alerts/runs/2026-09-27_*.json

The whole day is the negative set: everything the run reported that is not in `must_fire` and not
in `borderline` is a false positive. `must_not_fire` is the annotated subset of the negatives, so a
trap that was tripped gets named rather than merely counted.

Two recall numbers, because they mean different things:
  stories    — did it notice the situation at all (this is what wakes a human)
  intercepts — did it pull the whole thread (this is what he reads)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GOLD = json.loads((REPO / "tools" / "alerts" / "golden.json").read_text(encoding="utf-8"))


def score(path: Path) -> dict:
    run = json.loads(path.read_text(encoding="utf-8"))
    flagged = {a["id"]: a for a in run["alerts"]}
    # a run may name a dropped duplicate; credit it to the row that was actually read
    must, story_of = {}, {}
    for sid, st in GOLD["must_fire"].items():
        for mid, why in st["ids"].items():
            must[mid] = why
            story_of[mid] = sid
    traps = GOLD["must_not_fire"]
    border = set(GOLD["borderline"])

    hit = {m for m in must if m in flagged}
    miss = {m for m in must if m not in flagged}
    stories_hit = {story_of[m] for m in hit}
    stories_all = set(GOLD["must_fire"])
    fp = {i: a for i, a in flagged.items() if i not in must and i not in border}
    tripped = {i: traps[i] for i in fp if i in traps}

    return {"path": path, "run": run, "must": must, "story_of": story_of,
            "hit": hit, "miss": miss, "stories_hit": stories_hit, "stories_all": stories_all,
            "fp": fp, "tripped": tripped, "flagged": flagged}


def report(s: dict) -> None:
    r, run = s, s["run"]
    tag = f"{run['model']}{' ' + run['effort'] if run.get('effort') else ''}"
    print("=" * 78)
    print(f"{tag}   вікно {run['window']}   ${run['cost_usd']}   {run['seconds']}с   "
          f"прочитано {run['intercepts_read']} з {run['intercepts_raw']}")
    print(f"  тривог видано: {len(r['flagged'])}")
    print(f"  історії:    {len(r['stories_hit'])}/{len(r['stories_all'])}  "
          f"({', '.join(sorted(r['stories_hit'])) or 'жодної'})")
    print(f"  перехоплення: {len(r['hit'])}/{len(r['must'])}")
    print(f"  хибних тривог: {len(r['fp'])}   з них іменованих пасток: {len(r['tripped'])}")
    if r["miss"]:
        print("  ПРОПУЩЕНО:")
        for m in sorted(r["miss"], key=lambda x: r["must"][x]):
            print(f"    - [{r['story_of'][m]}] {r['must'][m]}")
    if r["tripped"]:
        print("  ПАСТКИ, В ЯКІ ВЛЕТІЛА:")
        for i, why in r["tripped"].items():
            a = r["flagged"][i]
            print(f"    - {why}")
            print(f"        сказала: {a.get('class')} / {a.get('why')}")
    other = {i: a for i, a in r["fp"].items() if i not in r["tripped"]}
    if other:
        print(f"  інші хибні ({len(other)}), перші 8:")
        for i, a in list(other.items())[:8]:
            print(f"    - {a.get('hhmmss')} {a.get('freq')} [{a.get('class')}] {a.get('why')}")


def main() -> None:
    paths = [Path(p) for p in sys.argv[1:]]
    if not paths:
        print(__doc__)
        sys.exit(1)
    scored = [score(p) for p in paths if p.exists()]
    for s in scored:
        report(s)
    if len(scored) > 1:
        print("\n" + "=" * 78)
        print(f"{'модель':28} {'істор':>6} {'перех':>7} {'хибн':>6} {'пастк':>6} {'$':>7}")
        for s in scored:
            run = s["run"]
            tag = f"{run['model']}{' ' + run['effort'] if run.get('effort') else ''}"
            print(f"{tag:28} {len(s['stories_hit'])}/{len(s['stories_all']):<4} "
                  f"{len(s['hit'])}/{len(s['must']):<5} {len(s['fp']):>6} {len(s['tripped']):>6} "
                  f"{run['cost_usd']:>7}")


if __name__ == "__main__":
    main()
