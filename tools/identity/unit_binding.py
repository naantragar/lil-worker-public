#!/usr/bin/env python3
"""Which formation does each callsign belong to — counted from the net headers, no model.

    python3 tools/identity/unit_binding.py --days 15
    python3 tools/identity/unit_binding.py --days 15 --group "Invisible Hand"

The unit is written in the header in plain text — `УКХ р/м 1 мсб 38 омсбр 35 А (МИРНЕ, ЧАРІВНЕ)` —
so the binding is arithmetic: collect the headers a man was heard under, pull the formation out of
each, count. That is the whole method, and it is free.

Measured 14.09.2026 over 15 days: of 294 people heard 20+ times under a header naming a formation,
**121 sit at 95-100% under ONE** — БАЛАБОЛ 551/551 under 60 омсбр, ГУСЕЙН 436/436 under 38 омсбр,
ХАМУС 240/240. Including men the analyst's archive has never named, whose formation still falls out
instantly.

**Nothing is merged, split or overwritten here.** Three states are reported and the disputed ones
keep their numbers:

    bound     >= 95% of his air under one formation
    likely    >= 75%
    disputed  < 75%  — either he moved, or TWO men share the word (ВОВАН: 7 formations in 46
              appearances). These are not noise, they are the candidate list for the identity rule,
              and they go to a human, never to an automatic split.

WHICH GROUP TO READ. `PATAGONIA_GP` is several collectors pooled, so it carries months of history
but also other people's traffic. `Invisible Hand` is our own direction only — the right source in
principle — but recording started 13.09.2026 12:14, and purity needs weeks to mean anything. So:
Patagonia while the history matters, Invisible Hand once it has depth. The switch is one flag, and
the same code serves both.
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = REPO / "knowledge" / "upstream" / "callsigns"
UNIT = re.compile(r"\b(\d{1,4})\s*(омсбр|омбр|мсбр|мсп|мсд|тп|оп|бр|пдп|дшб)\b", re.I)
BOUND, LIKELY = 0.95, 0.75


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=15)
    ap.add_argument("--to", default=None)
    ap.add_argument("--group", default=None, help="corpus group; default PATAGONIA_GP")
    ap.add_argument("--min-heard", type=int, default=20,
                    help="below this a share is arithmetic noise, not evidence")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    if a.group:
        os.environ["UPSTREAM_CORPUS_GROUP"] = a.group
    M = _load(REPO / "tools" / "analytics2" / "run.py", "analytics2_run")
    G = _load(HERE / "guide_layer.py", "identity_guide_layer")

    hi = datetime.strptime(a.to, "%Y-%m-%d %H:%M") if a.to else datetime.now()
    lo = hi - timedelta(days=a.days)
    recs = M.fetch(f"{lo:%Y-%m-%d %H:%M}", f"{hi:%Y-%m-%d %H:%M}")
    print(f"група: {a.group or M.CORPUS_GROUP}   вікно {lo:%d.%m} - {hi:%d.%m}   "
          f"перехоплень {len(recs)}", file=sys.stderr)

    per: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    nets_of: dict[str, set] = collections.defaultdict(set)
    no_unit = collections.Counter()
    for r in recs:
        hdr = (r.get("network") or "").strip()
        units = {f"{n} {u.lower()}" for n, u in UNIT.findall(hdr)}
        names = {n for raw in (r.get("stations") or []) for n in G.clean_slot(raw, M)}
        for n in names:
            nets_of[n].add(hdr)
            if units:
                per[n].update(units)
            else:
                no_unit[n] += 1

    rows = []
    for n, c in per.items():
        total = sum(c.values())
        if total < a.min_heard:
            continue
        top, cnt = c.most_common(1)[0]
        share = cnt / total
        rows.append({"callsign": n, "unit": top, "share": round(share, 3),
                     "heard_under_unit": total, "heard_no_unit": no_unit.get(n, 0),
                     "state": "bound" if share >= BOUND else
                              ("likely" if share >= LIKELY else "disputed"),
                     "all_units": dict(c.most_common()), "nets": len(nets_of[n])})
    rows.sort(key=lambda r: (-r["share"], -r["heard_under_unit"]))
    tally = collections.Counter(r["state"] for r in rows)

    OUT.mkdir(parents=True, exist_ok=True)
    path = Path(a.out) if a.out else OUT / "unit_binding.json"
    path.write_text(json.dumps({"group": a.group or M.CORPUS_GROUP,
                                "window": [f"{lo:%Y-%m-%d}", f"{hi:%Y-%m-%d}"],
                                "min_heard": a.min_heard, "tally": dict(tally),
                                "people": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"людей із підрозділом: {len(rows)}   "
          f"певних {tally['bound']} · ймовірних {tally['likely']} · спірних {tally['disputed']}",
          file=sys.stderr)
    print(f"{path}", file=sys.stderr)

    print(f"\n{'позивний':<14}{'підрозділ':<14}{'частка':>8}{'чути':>7}  стан")
    for r in rows[:20]:
        print(f"{r['callsign']:<14}{r['unit']:<14}{r['share']*100:>7.0f}%{r['heard_under_unit']:>7}"
              f"  {r['state']}")
    dis = [r for r in rows if r["state"] == "disputed"]
    if dis:
        print(f"\nспірні (кандидати на розділення, до людини - не автоматично): {len(dis)}")
        for r in sorted(dis, key=lambda r: -len(r["all_units"]))[:8]:
            print(f"  {r['callsign']:<12} {len(r['all_units'])} формувань, найчастіше "
                  f"{r['unit']} лише {r['share']*100:.0f}% з {r['heard_under_unit']}")


if __name__ == "__main__":
    main()
