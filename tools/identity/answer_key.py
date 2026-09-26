#!/usr/bin/env python3
"""Draw the answer key for «хто водить» — the one class the doctrine names and nothing validates.

    python3 tools/identity/answer_key.py [--per-man 5] [--window-days 30]

Across the whole frozen archive only ~15 callsigns carry a leading/escorting role phrase, of which
10-11 are audible in any window. So every accuracy figure quoted for «хто водить» — including the
54.3% — is scored against a taxonomy answering a different question. This buys a real one, in one
sitting of the owner's attention, and it becomes the regression set forever after.

**Four strata, and they matter more than the size** (from the swarm,
`knowledge/upstream/identity-evidence-brainstorm-2026-09-12.md`):

  * top of the asymmetry ranking;
  * the middle band;
  * LOUD but low-asymmetry — the control that catches a loudness detector wearing the doctrine's
    words. Without it a metric that merely ranks the noisiest man passes;
  * drawn at random from men heard on many days regardless of rank — without this the key measures
    our precision and can NEVER measure our recall, and would happily certify a metric that
    silently misses half the class.

**The scores are hidden from the sheet on purpose.** A key marked while looking at our own ranking
is not a key, it is our ranking with a signature. Order is shuffled under a fixed seed; the mapping
back to callsign, stratum and score lives in the separate `_key.json`, which is NOT for the marker.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT_DIR = REPO / "knowledge" / "upstream" / "callsigns"
SEED = 20260912


def load_run():
    spec = importlib.util.spec_from_file_location(
        "analytics2_run", REPO / "tools" / "analytics2" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def strata(rank: list[dict], n: int, rng) -> list[tuple[str, str]]:
    """(callsign, stratum) pairs. Disjoint by construction — a man is drawn once."""
    taken: set[str] = set()
    out: list[tuple[str, str]] = []

    def take(pool, label, k):
        for r in pool:
            if len(out) >= 0 and sum(1 for x in out if x[1] == label) >= k:
                break
            if r["callsign"] in taken:
                continue
            taken.add(r["callsign"])
            out.append((r["callsign"], label))

    ordered = sorted(rank, key=lambda r: -r["asym"])
    take(ordered, "top", n)
    mid = ordered[len(ordered) // 3: 2 * len(ordered) // 3]
    take(sorted(mid, key=lambda r: -r["air"]), "mid", n)
    # the control: loud, but the direction says people key at him rather than he at them
    loud_low = sorted([r for r in ordered if r["asym"] < 0.25], key=lambda r: -r["air"])
    take(loud_low, "loud-low", n)
    rest = [r for r in rank if r["callsign"] not in taken and r["air_days"] >= 5]
    rng.shuffle(rest)
    take(rest, "random", n)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--layer", default=str(OUT_DIR / "guide_layer.json"))
    ap.add_argument("--per-stratum", type=int, default=10)
    ap.add_argument("--per-man", type=int, default=5)
    ap.add_argument("--window-days", type=int, default=30,
                    help="how far back to pull the verbatim intercepts from")
    ap.add_argument("--to", default="2026-09-12 15:00")
    a = ap.parse_args()

    layer = json.loads(Path(a.layer).read_text())
    rng = random.Random(SEED)
    picked = strata(layer["rank"], a.per_stratum, rng)
    if not picked:
        sys.exit("рейтинг порожній — спершу прожени guide_layer.py")
    want = {c for c, _ in picked}
    print(f"відібрано {len(picked)} людей: " +
          ", ".join(f"{s}={sum(1 for _, x in picked if x == s)}"
                    for s in ("top", "mid", "loud-low", "random")), file=sys.stderr)

    M = load_run()
    hi = datetime.strptime(a.to, "%Y-%m-%d %H:%M")
    lo = hi - timedelta(days=a.window_days)
    recs = M.fetch(f"{lo:%Y-%m-%d %H:%M}", f"{hi:%Y-%m-%d %H:%M}")
    print(f"перехоплень у вікні: {len(recs)}", file=sys.stderr)

    # Intercepts where the man KEYED the mic — that is where his own behaviour is visible.
    his = defaultdict(list)
    for r in recs:
        st = r.get("stations") or []
        if not st:
            continue
        for name in {n for n in _slot(st[0], M)} & want:
            his[name].append(r)

    rng2 = random.Random(SEED)
    sheet, key = [], []
    sheet += ["ЕТАЛОН «ХТО ВОДИТЬ» - розмітка",
              "",
              "Для кожного позивного нижче: 5 перехоплень, де він сам виходив в ефір.",
              "Постав одну позначку: ВОДИТЬ / НЕ ВОДИТЬ / НЕ ВИДНО.",
              "«Водить» = його голосом ведуть людей по місцевості або він ставить їм задачу руху.",
              "«Не видно» = з цих п'яти зрозуміти неможливо.",
              "",
              "Порядок перемішано, наші оцінки навмисно НЕ показані - інакше це буде не еталон,",
              "а наш же рейтинг з твоїм підписом.",
              "=" * 72, ""]

    order = list(picked)
    rng2.shuffle(order)
    n_out = 0
    for idx, (name, stratum) in enumerate(order, 1):
        pool = his.get(name) or []
        if not pool:
            continue
        by_day = defaultdict(list)
        for r in pool:
            by_day[r["date"]].append(r)
        chosen = []
        for day in sorted(by_day, key=lambda d: -len(by_day[d])):   # spread across days
            chosen.append(by_day[day][len(by_day[day]) // 2])
            if len(chosen) >= a.per_man:
                break
        while len(chosen) < a.per_man and len(chosen) < len(pool):
            extra = pool[rng2.randrange(len(pool))]
            if extra not in chosen:
                chosen.append(extra)
        n_out += 1
        key.append({"n": idx, "callsign": name, "stratum": stratum,
                    "shown": len(chosen)})
        sheet += [f"--- {idx} ---   {name}", "",
                  "    [ ] ВОДИТЬ      [ ] НЕ ВОДИТЬ      [ ] НЕ ВИДНО", ""]
        for r in sorted(chosen, key=lambda x: (x["date"], x["time"])):
            who = " -> ".join(x for x in (r.get("stations") or [])[:2] if x)
            mark = " ".join(x for x in (r.get("comment_above"), r.get("comment_below")) if x)
            sheet.append(f"  {r['date']} {r['time']} · {r.get('freq') or '?'} · {who}")
            if mark:
                sheet.append(f"     мітка: {mark[:150]}")
            for line in (r.get("speech") or [])[:6]:
                sheet.append(f"     {line[:300]}")
            sheet.append("")
        sheet += ["=" * 72, ""]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sheet_p = OUT_DIR / "ETALON_vodyt_rozmitka.txt"
    key_p = OUT_DIR / "ETALON_vodyt_key.json"
    sheet_p.write_text("\n".join(sheet), encoding="utf-8")
    key_p.write_text(json.dumps({"seed": SEED, "window": [f"{lo:%Y-%m-%d}", f"{hi:%Y-%m-%d}"],
                                 "entries": key}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{sheet_p}   ({n_out} людей)", file=sys.stderr)
    print(f"{key_p}   (НЕ для того, хто розмічає)", file=sys.stderr)


def _slot(raw, M):
    import re
    out = []
    for part in re.split(r"[,/]| та ", raw or ""):
        n = re.sub(r"^\d{1,2}\s*-\s*", "", part.strip().strip(".").upper())
        if n and n not in {"НВ", "НП", "БП"} and M.names(n):
            out.append(n)
    return out


if __name__ == "__main__":
    main()
