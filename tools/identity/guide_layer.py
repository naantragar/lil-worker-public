#!/usr/bin/env python3
"""Who LEADS people over the ground — direction, not volume.

    python3 tools/identity/guide_layer.py --days 60 [--min-edges 10] [--out FILE]

Built 12.09.2026 out of the specialist swarm's one surviving finding
(`knowledge/upstream/identity-evidence-brainstorm-2026-09-12.md`). Two fields nobody had read:

  * `stations[0]` is who keyed the mic, `stations[1]` is who was called. Verified four independent
    ways (agreement with a spoken «я X»: 82-91%).
  * 70% of intercepts carry the collector's own one-line mark, and a subset of those marks name an
    act of GUIDING — корегування, супровід, заведення, під наглядом.

Cross the two and a directed `guided` edge falls out mechanically, over the whole corpus including
the unsigned nets the report never prints, at zero model cost.

**Why not breadth.** Ranking by "how many different people, over how many days" was proposed by four
lenses because it is the doctrine's own wording — and killed by four volume controls: it correlates
0.811 with sheer air volume, raw loudness beats it against the archive (p@10 0.80 vs 0.60), and
МАЛОЙ — the doctrine's canonical pawn — lands 28th of 2846. Breadth is degree wearing the doctrine's
words. Asymmetry survived the same controls (correlation with volume 0.256) and separates inside
every volume band.

**Honest limits, printed rather than discovered later.** Both slots carry a real callsign in only
about a fifth of guide-marked intercepts, so the directed layer rests on thousands of intercepts, not
the corpus. Slot direction is ~85-91% right on a single name and ~70% on a list, so roughly one edge
in nine is reversed — below MIN_EDGES the sign is noise and no score is printed.

Nothing here is asserted: every edge keeps its day, net, frequency and msg_id, and the store holds
observations, never conclusions. Read-only on the corpus; touches nothing under `tools/analytics2/`.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT_DIR = REPO / "knowledge" / "upstream" / "callsigns"

# The collector's vocabulary for an act of leading someone over the ground.
GUIDE = re.compile(r"корегув|коригув|супровід|супровод|заведенн|завод|наглядом|веде|ведуть|"
                   r"провід|маршрут", re.I)

SPLIT = re.compile(r"[,/]| та ")
UNKNOWN = {"НВ", "НП", "БП", "Б/П", "НВ."}
# A net header that leaked into a station slot: it names a unit, a band or the mode.
LEAK = re.compile(r"\bУКХ\b|\bКХ\b|\bDMR\b|Р/М|ОМСБР|ОМБР|МСП|МСД|МСБ|МСР|\bА\)\s*$", re.I)
# `1-ДОК` — a speaker index glued to the name. 670 station lines carry one; the digit is real
# information (which operator of that station), the glued token is a ghost node.
PREFIX = re.compile(r"^(\d{1,2})\s*-\s*(?=[А-ЯЁЇІЄҐ])")
# The template whose slots are the other way round.
REVERSED = re.compile(r"отримувач|відправник|получатель|отправитель", re.I)


def load_run():
    spec = importlib.util.spec_from_file_location(
        "analytics2_run", REPO / "tools" / "analytics2" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def clean_slot(raw: str, M) -> list[str]:
    """Callsigns in one station slot, hygiene applied. Empty list means 'nothing usable here'."""
    if not raw or LEAK.search(raw):
        return []
    out = []
    for part in SPLIT.split(raw):
        name = part.strip().strip(".").upper()
        name = PREFIX.sub("", name)                 # 1-ДОК -> ДОК
        if not name or name in UNKNOWN:
            continue
        if M.names(name):
            out.append(name)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--to", default=None, help="YYYY-MM-DD HH:MM, default = now")
    ap.add_argument("--min-edges", type=int, default=10)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    M = load_run()
    hi = datetime.strptime(a.to, "%Y-%m-%d %H:%M") if a.to else datetime.now()
    lo = hi - timedelta(days=a.days)
    print(f"вікно {lo:%Y-%m-%d %H:%M} - {hi:%Y-%m-%d %H:%M}", file=sys.stderr)
    recs = M.fetch(f"{lo:%Y-%m-%d %H:%M}", f"{hi:%Y-%m-%d %H:%M}")
    print(f"перехоплень: {len(recs)}", file=sys.stderr)

    air = defaultdict(int)                       # how loud each man is, the control column
    air_days = defaultdict(set)
    out_e = defaultdict(lambda: defaultdict(set))   # keyer -> counterpart -> days   (he guides)
    in_e = defaultdict(lambda: defaultdict(set))    # counterpart -> keyer -> days   (he is guided)
    edges = []                                    # the observation store, one row per act
    stats = dict(guide_marked=0, both_named=0, leaked=0, reversed_tpl=0, prefixed=0)

    for r in recs:
        st = r.get("stations") or []
        day = r.get("date")
        for raw in st:
            if PREFIX.search(raw or ""):
                stats["prefixed"] += 1
            if LEAK.search(raw or ""):
                stats["leaked"] += 1
            for n in clean_slot(raw, M):
                air[n] += 1
                air_days[n].add(day)

        mark = " ".join(x for x in (r.get("comment_above"), r.get("comment_below")) if x)
        if not mark or not GUIDE.search(mark):
            continue
        stats["guide_marked"] += 1
        if len(st) < 2:
            continue
        first, second = st[0] or "", st[1] or ""
        if REVERSED.search(first) or REVERSED.search(second):
            first, second = second, first          # the template that writes the slots backwards
            stats["reversed_tpl"] += 1
        keyers, called = clean_slot(first, M), clean_slot(second, M)
        if not keyers or not called:
            continue
        stats["both_named"] += 1
        shape = "single" if len(called) == 1 else "list"
        for k in keyers:
            for c in called:
                if k == c:
                    continue
                out_e[k][c].add(day)
                in_e[c][k].add(day)
                edges.append({"keyer": k, "counterpart": c, "day": day, "freq": r.get("freq"),
                              "net": r.get("network"), "msg_id": r.get("msg_id"),
                              "shape": shape, "mark": mark[:120]})

    people = set(out_e) | set(in_e)
    rows = []
    for n in people:
        o = sum(len(v) for v in out_e[n].values())
        i = sum(len(v) for v in in_e[n].values())
        if o + i < a.min_edges:
            continue                              # below this the sign is noise, print nothing
        rows.append({
            "callsign": n,
            "asym": round((o - i) / (o + i), 3),
            "guides": o, "guided": i,
            "led": len(out_e[n]), "led_by": len(in_e[n]),
            "days": len({d for v in out_e[n].values() for d in v}),
            "air": air.get(n, 0), "air_days": len(air_days.get(n, ())),
        })
    rows.sort(key=lambda r: (-r["asym"], -r["guides"]))

    print(f"позначено веденням: {stats['guide_marked']}   з обома іменами: {stats['both_named']}",
          file=sys.stderr)
    print(f"гігієна: префікс N- {stats['prefixed']}, заголовок у слоті {stats['leaked']}, "
          f"перевернутий шаблон {stats['reversed_tpl']}", file=sys.stderr)
    print(f"ребер: {len(edges)}   людей понад поріг {a.min_edges}: {len(rows)}", file=sys.stderr)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = Path(a.out) if a.out else OUT_DIR / "guide_layer.json"
    path.write_text(json.dumps({"window": [f"{lo:%Y-%m-%d %H:%M}", f"{hi:%Y-%m-%d %H:%M}"],
                                "stats": stats, "min_edges": a.min_edges,
                                "rank": rows, "edges": edges},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{path}", file=sys.stderr)

    print(f"\n{'позивний':<16}{'асим':>7}{'веде':>6}{'ведуть':>8}{'кого':>6}{'діб':>5}{'ефір':>7}")
    for r in rows[:25]:
        print(f"{r['callsign']:<16}{r['asym']:>7}{r['guides']:>6}{r['guided']:>8}"
              f"{r['led']:>6}{r['days']:>5}{r['air']:>7}")


if __name__ == "__main__":
    main()
