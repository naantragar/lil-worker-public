#!/usr/bin/env python3
"""Collect what a callsign's own traffic says about him - the only thing the model may see.

    python3 tools/callsign_roles/gather_evidence.py --days 21 [--out evidence.json]
    python3 tools/callsign_roles/gather_evidence.py --days 21 --stats-only

Two kinds of evidence, kept apart because they mean different things:

  * SPOKE     - he is a station in the exchange (talking or being talked to)
  * MENTIONED - somebody else names him while talking to a third party

The second is often the more telling of the two. A man who is *reported to* is command; a man who
is *asked to bring something* is logistics; a man whose position others walk past is a location.
His own words tell you how he talks; other people's words tell you what he is for.

Reads the corpus through `analytics2.run`'s own door - same parser, same read-only connection, so
this can never drift from what the report sees.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "analytics2"))
import run as A                                    # noqa: E402  the single door to the corpus

GOLD = Path(__file__).with_name("gold.json")


def variants(name: str) -> set[str]:
    """`СПАРТАК/ПАРТАК` is one man written two ways - both spellings have to match."""
    return {v.strip().strip("«»\"'").upper()
            for v in re.split(r"[,/]", name or "") if v.strip().strip("«»\"'")}


def station_names(rec: dict) -> set[str]:
    out = set()
    for s in rec.get("stations") or []:
        for part in re.split(r"[,/]", str(s)):
            p = part.strip().strip("«»\"'").upper()
            if p:
                out.add(p)
    return out


def collect(days: int, dt_to: str | None = None):
    hi = datetime.strptime(dt_to, "%Y-%m-%d %H:%M") if dt_to else datetime.now()
    lo = hi - timedelta(days=days)
    recs = A.fetch(lo.strftime("%Y-%m-%d %H:%M"), hi.strftime("%Y-%m-%d %H:%M"))

    # index once: every record's station set and its upper-cased speech blob
    prepared = []
    for r in recs:
        speech = " ".join(str(x) for x in (r.get("speech") or []))
        prepared.append((r, station_names(r), speech.upper()))
    return recs, prepared, lo, hi


def evidence_for(name: str, prepared: list) -> dict:
    vs = variants(name)
    spoke, mentioned = [], []
    for rec, stations, blob in prepared:
        if vs & stations:
            spoke.append(rec)
        elif any(re.search(rf"(?<![А-ЯЄІЇҐA-Z]){re.escape(v)}(?![А-ЯЄІЇҐA-Z])", blob) for v in vs):
            mentioned.append(rec)
    days = {r["_dt"].date().isoformat() for r in spoke + mentioned if r.get("_dt")}
    freqs = {str(r.get("freq")) for r in spoke + mentioned if r.get("freq")}
    chars = sum(len(" ".join(str(x) for x in (r.get("speech") or [])))
                for r in spoke + mentioned)
    return {"callsign": name, "spoke": spoke, "mentioned": mentioned,
            "n_spoke": len(spoke), "n_mentioned": len(mentioned),
            "days": sorted(days), "freqs": sorted(freqs), "chars": chars}


def as_material(ev: dict, max_intercepts: int = 40) -> str:
    """The evidence as the model will see it. Nothing else is ever passed."""
    out = [f"ПОЗИВНИЙ: {ev['callsign']}",
           f"частоти: {', '.join(ev['freqs']) or '-'}",
           f"перехоплень: він у станціях {ev['n_spoke']}, згаданий іншими {ev['n_mentioned']}",
           f"діб з ефіром: {len(ev['days'])}", ""]
    for tag, recs in (("[ВІН У СТАНЦІЯХ]", ev["spoke"]), ("[ЗГАДАНИЙ ІНШИМИ]", ev["mentioned"])):
        for r in recs[:max_intercepts]:
            dt = r["_dt"].strftime("%d.%m %H:%M") if r.get("_dt") else "??"
            out.append(f"{tag} {dt} {r.get('freq', '')} | станції: {r.get('stations')}")
            for line in (r.get("speech") or []):
                out.append(f"    {line}")
            out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--to", dest="dt_to", help="end of the window, YYYY-MM-DD HH:MM")
    ap.add_argument("--only", nargs="*", help="just these callsigns")
    ap.add_argument("--stats-only", action="store_true")
    ap.add_argument("-o", "--out", default=str(Path(__file__).with_name("evidence.json")))
    args = ap.parse_args()

    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    names = args.only or sorted(set(gold["gold"]) | set(gold["unlabelled"]))

    recs, prepared, lo, hi = collect(args.days, args.dt_to)
    print(f"вікно {lo:%d.%m %H:%M} - {hi:%d.%m %H:%M}: {len(recs)} перехоплень у корпусі")

    buckets = defaultdict(list)
    keep = {}
    for n in names:
        ev = evidence_for(n, prepared)
        total = ev["n_spoke"] + ev["n_mentioned"]
        labelled = n in gold["gold"]
        if total == 0:
            buckets["0 - німий"].append(n)
        elif total < 3:
            buckets["1-2 - надто мало"].append(n)
        elif total < 10:
            buckets["3-9 - тонко"].append(n)
        elif total < 30:
            buckets["10-29 - робочий обсяг"].append(n)
        else:
            buckets["30+ - багато"].append(n)
        if not args.stats_only and total:
            keep[n] = {k: v for k, v in ev.items() if k not in ("spoke", "mentioned")}
            keep[n]["material"] = as_material(ev)
            keep[n]["gold_roles"] = gold["gold"].get(n, {}).get("roles", [])

    order = ["30+ - багато", "10-29 - робочий обсяг", "3-9 - тонко",
             "1-2 - надто мало", "0 - німий"]
    print(f"\nматеріалу на позивного ({len(names)} позивних):")
    for b in order:
        v = buckets.get(b, [])
        lab = sum(1 for n in v if n in gold["gold"])
        print(f"   {len(v):>4}  {b:<22} з них з еталонною роллю: {lab}")

    workable = sum(len(buckets.get(b, [])) for b in order[:3])
    lab_workable = sum(1 for b in order[:3] for n in buckets.get(b, []) if n in gold["gold"])
    print(f"\nпридатних до аналізу (>=3 перехоплення): {workable}, "
          f"з них з еталоном для калібрування: {lab_workable}")

    if not args.stats_only:
        Path(args.out).write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"записано: {args.out}  ({len(keep)} позивних з матеріалом)")


if __name__ == "__main__":
    main()
