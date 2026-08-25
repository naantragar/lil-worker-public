#!/usr/bin/env python3
"""Pass B: harvest entities across a whole window, with their contexts.

This is the layer the per-thread extraction structurally cannot reach. A code word appears ONCE
inside a thread and is undecodable there; across a day it appears eight times in different sentences
and becomes readable. A callsign's function is invisible in one exchange and obvious across twenty.

Everything here is deterministic — regex and counting — so it costs nothing. Its output is the
compressed table that the expensive reasoning pass (Opus, high effort) then works on: a day of raw
speech is ~530 KB, this is ~30-50 KB.

    python3 tools/analytics_entities.py --from '2026-08-20 15:00' --to '2026-08-21 15:00' \
        [--out entities.json] [--max-context 4]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from analytics_run import fetch, group, norm_net  # noqa: E402

GLOSSARY = HERE.parent / "matrix" / "bot" / "prompts" / "refraz" / "10_glossary.md"

# Callsigns come from the STATION lines of the header — that is the one place they are marked as
# callsigns rather than guessed. Everything else in this file hangs off that authoritative list.
#
# What CANNOT be harvested with regex, learned the hard way on real data (2026-08-22): code words and
# points. The notation `т12`, `ор КОМПАС`, `«кубок»` is the ANALYST'S language in the report — in the
# raw air it is just "идём на кубок", unquoted and unmarked. A first attempt to grep for it returned
# zero codes and zero points, while a case-insensitive callsign match returned "ТАМ", "СЕЙЧАС" and
# "ТЕБЯ" as the most frequent callsigns of the day. Decoding new words is a reasoning task, and it
# belongs to pass C with this table as its scaffolding.
RE_STATION = re.compile(r"[А-ЯЁЇІЄҐA-Z][А-ЯЁЇІЄҐA-Z0-9'\- ]{1,24}")
STOP_CALLSIGN = {"НВ", "НВ.", "ОК", "БПЛА", "СОУ", "РОВ", "МТЗ", "ТМ", "ФПВ"}


def stations_of(rec: dict) -> list[str]:
    """Callsigns declared in this intercept's header, cleaned."""
    out = []
    for raw in rec.get("stations", []):
        for part in re.split(r"[,/]| та ", raw):
            name = part.strip().strip(".").upper()
            if 2 < len(name) <= 24 and name not in STOP_CALLSIGN and RE_STATION.fullmatch(name):
                out.append(name)
    return out


def harvest(threads: list[dict], max_ctx: int) -> dict:
    calls: dict[str, dict] = defaultdict(lambda: {
        "as_station": 0, "mentioned": 0, "networks": set(), "freqs": set(),
        "partners": defaultdict(int), "contexts": []})

    # 1. authoritative pass: who was on the air, in which network, on which frequency, with whom
    for th in threads:
        net = th["net"] or "(без шапки)"
        for rec in th["items"]:
            names = stations_of(rec)
            for name in names:
                e = calls[name]
                e["as_station"] += 1
                e["networks"].add(net)
                if rec.get("freq"):
                    e["freqs"].add(rec["freq"])
                for other in names:
                    if other != name:
                        e["partners"][other] += 1

    # 2. contexts: find those known names inside the speech of the whole window
    known = sorted(calls, key=len, reverse=True)
    if known:
        pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in known) + r")\b", re.I)
        for th in threads:
            for rec in th["items"]:
                for line in rec.get("speech", []):
                    for m in set(x.upper() for x in pattern.findall(line)):
                        e = calls.get(m)
                        if e is None:
                            continue
                        e["mentioned"] += 1
                        if len(e["contexts"]) < max_ctx:
                            e["contexts"].append(line.strip()[:170])

    rows = []
    for name, e in calls.items():
        total = e["as_station"] + e["mentioned"]
        if total < 2:                      # heard once is noise
            continue
        rows.append({
            "name": name,
            "as_station": e["as_station"],
            "mentioned": e["mentioned"],
            "networks": sorted(e["networks"]),
            "freqs": sorted(e["freqs"]),
            "partners": [k for k, _ in sorted(e["partners"].items(), key=lambda x: -x[1])[:6]],
            "contexts": e["contexts"],
        })
    rows.sort(key=lambda r: -(r["as_station"] + r["mentioned"]))
    return {"callsigns": rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dt_from", required=True)
    ap.add_argument("--to", dest="dt_to", required=True)
    ap.add_argument("--max-context", type=int, default=4)
    ap.add_argument("--out")
    a = ap.parse_args()

    threads = group(fetch(a.dt_from, a.dt_to))
    data = harvest(threads, a.max_context)
    blob = json.dumps(data, ensure_ascii=False, indent=1)

    print(f"позивних: {len(data['callsigns'])}   обсяг: {len(blob)/1024:.0f} КБ", file=sys.stderr)
    print("\nнайактивніші (в ефірі / згадувань / мереж / частот):", file=sys.stderr)
    for r in data["callsigns"][:12]:
        print(f"   {r['name']:16} {r['as_station']:4} / {r['mentioned']:4} / "
              f"{len(r['networks'])} / {len(r['freqs'])}   партнери: {', '.join(r['partners'][:4])}",
              file=sys.stderr)
    multi = [r for r in data["callsigns"] if len(r["networks"]) > 1]
    print(f"\nпозивних більш ніж в одній мережі: {len(multi)} (кандидати на ком склад або збіг імен)",
          file=sys.stderr)

    if a.out:
        Path(a.out).write_text(blob)
        print(f"\n{a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
