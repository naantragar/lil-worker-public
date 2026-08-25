#!/usr/bin/env python3
"""Show the raw intercepts a report line was built from.

Every event the model emits carries `src` — the indices of the intercepts it rests on, numbered
within its own unit of work — and `_net`, the network. That is enough to walk back from a finished
line to the actual speech, provided the same window is rebuilt: `units_of_work` is deterministic, so
re-running it over the same window reproduces the same numbering.

    python3 tools/analytics2/trace.py --events knowledge/upstream/reports_out/ZVIT_24.08_v2h_events.json \
        --from '2026-08-23 15:00' --to '2026-08-24 15:00' --grep ВОРОН [--context 2]

Without --grep every event of the file is traced, which is long; it is meant for one line at a time.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_run():
    spec = importlib.util.spec_from_file_location("analytics2_run", HERE / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--from", dest="dt_from", required=True)
    ap.add_argument("--to", dest="dt_to", required=True)
    ap.add_argument("--grep", help="substring of the report line to trace")
    ap.add_argument("--context", type=int, default=0,
                    help="also print N intercepts either side of each source")
    a = ap.parse_args()

    run = _load_run()
    events = json.loads(Path(a.events).read_text())
    if a.grep:
        events = [e for e in events if a.grep.lower() in str(e.get("text", "")).lower()]
    if not events:
        sys.exit("нічого не знайдено за цим фрагментом")

    units = run.units_of_work(run.fetch(a.dt_from, a.dt_to))
    by_net: dict[str, list[dict]] = {}
    for u in units:
        by_net.setdefault(u["net"], []).append(u)

    for e in events:
        print("=" * 90)
        print(f"РЯДОК ЗВІТУ: {e.get('time','')} {e.get('text','')}")
        print(f"мережа: {e.get('_net')}   src: {e.get('src')}   confidence: {e.get('confidence')}")
        cands = by_net.get(e.get("_net") or "", [])
        if not cands:
            print("  (мережу не знайдено — вікно або база змінилися)")
            continue
        # A network split into parts numbers its intercepts per part, and the event does not record
        # which part it came from. Print the candidate from every part and let the timestamp decide.
        for u in cands:
            part = f" [частина {u['part'][0]}/{u['part'][1]}]" if u.get("part") else ""
            for i in e.get("src") or []:
                lo, hi = max(0, i - a.context), min(len(u["items"]), i + a.context + 1)
                for k in range(lo, hi):
                    r = u["items"][k]
                    who = " / ".join(r["stations"]) if r["stations"] else "НВ"
                    mark = ">>>" if k == i else "   "
                    print(f"\n{mark} [{k}]{part} {r['date']}, {r['time'][:5]} — {who}"
                          f"   (msg {r.get('msg_id')})")
                    for tag in ("comment_above", "comment_below"):
                        if r.get(tag):
                            print(f"      (позначка аналітика: {r[tag]})")
                    for s in r["speech"]:
                        print(f"      {s}")
        print()


if __name__ == "__main__":
    main()
