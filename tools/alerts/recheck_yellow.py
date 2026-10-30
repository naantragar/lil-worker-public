#!/usr/bin/env python3
"""Second, focused read of the day's YELLOWs (owner, 16.10.2026).

The daily sweep reads every window once. A line the model judged «yellow» may in fact be red — not
by a keyword (the safety net covers those) but by meaning: a position given away, an order to move
on our people, a thread whose gravity only shows two transmissions later. This re-asks the model
about exactly those intercepts, and nothing else, so it costs a fraction of the sweep.

How it differs from the first read, on purpose:
  * the window is CENTRED on the yellow intercept (6 before, the rest after) instead of falling on a
    fixed grid — the follow-up that decides a capture or a hit is usually AFTER the line;
  * the doctrine gets one extra paragraph telling the model these were borderline and asking for a
    strict red-or-not — it may only RAISE, never lower;
  * only verdicts that turn a yellow into red are kept; everything else is the first read's job.

    python3 tools/alerts/recheck_yellow.py <run.json> --out <recheck.json> [--hours 24]

Output is run-shaped (`{"alerts": [...]}`), each alert carrying `escalated`, so push_tg.py delivers
it like the model's own reds with the reason shown under the header.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench import OURS_SRC, ask, dedup, doctrine, load_day, render  # noqa: E402

BEFORE = 6          # lines of the net before the yellow one
SIZE = 14           # whole window, same as the daily sweep

RECHECK_NOTE = """

ПЕРЕПРОХІД. Це вікно взяте навколо перехоплень, які перший прохід оцінив як yellow. Перечитай
їх суворо й цілком, разом із подальшими передачами в цьому ж вікні: чи немає серед них такого,
що за цією доктриною є red. Рівень можна лише ПІДВИЩИТИ, знижувати не треба - якщо не red,
просто не включай. Відповідай тим самим JSON-форматом.
"""


def centred_windows(rows: list[dict], yellow_ids: set[str]) -> list[list[dict]]:
    by_freq: dict[str, list[dict]] = {}
    for r in rows:
        by_freq.setdefault(r["freq"], []).append(r)
    wins: list[list[dict]] = []
    for rs in by_freq.values():
        rs.sort(key=lambda r: r["ts"])
        covered: set[str] = set()
        for i, r in enumerate(rs):
            rid = str(r["id"])
            if rid not in yellow_ids or rid in covered:
                continue
            lo = max(0, i - BEFORE)
            win = rs[lo:lo + SIZE]
            covered.update(str(x["id"]) for x in win)
            wins.append(win)
    return wins


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--out", required=True)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    run = json.loads(Path(a.run).read_text(encoding="utf-8"))
    alerts = run.get("alerts", [])
    yellow_ids = {str(x["id"]) for x in alerts if x.get("level") == "yellow"}
    red_ids = {str(x["id"]) for x in alerts if x.get("level") == "red"}
    model, effort = run.get("model", "claude-opus-5"), run.get("effort", "")
    rules = doctrine(run.get("doctrine", "v7")) + RECHECK_NOTE

    rows = [r for r in load_day("", hours=a.hours) if r["src"] in OURS_SRC]
    kept, _ = dedup(rows)
    wins = centred_windows(kept, yellow_ids)
    print(f"перепрохід: жовтих {len(yellow_ids)} -> вікон {len(wins)}", file=sys.stderr)

    results = []
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for res in ex.map(lambda w: ask(render(w), model, effort, rules), wins):
            results.append(res)

    by_id = {str(r["id"]): r for r in kept}
    raised: dict[str, dict] = {}
    cost = 0.0
    for res in results:
        cost += float(res.get("_cost") or 0)
        for al in res.get("alerts", []):
            mid = str(al.get("id", "")).strip()
            if al.get("level") != "red" or mid not in yellow_ids or mid in red_ids:
                continue
            r = by_id.get(mid)
            if not r:
                continue
            raised[mid] = {"id": mid, "level": "red", "class": al.get("class"),
                           "why": al.get("why"), "confidence": al.get("confidence"),
                           "hhmmss": r["hhmmss"], "freq": r["freq"], "src": r["src"], "ts": r["ts"],
                           "escalated": "перепрохід жовтих"}

    out = sorted(raised.values(), key=lambda x: x.get("hhmmss", ""))
    Path(a.out).write_text(json.dumps({"alerts": out, "source": "recheck_yellow",
                                       "windows": len(wins), "cost_usd": round(cost, 4)},
                                      ensure_ascii=False), encoding="utf-8")
    print(f"перепрохід: підвищено до red {len(out)}, ${cost:.2f}", file=sys.stderr)
    print(a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
