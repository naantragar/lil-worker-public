#!/usr/bin/env python3
"""Sum the per-day slices into an answer: how many people moved on each frequency this week.

Two numbers, never one: men identified by callsign (counted ONCE for the whole week no matter how
many times they moved), and separately the groups given only as a size. They must not be added
blindly — whoever in an "8 малых" group also has a callsign would be counted twice — so the second
number is reported as what it is: additional people whose names were never said.
"""
import json
import re
from collections import defaultdict
from pathlib import Path

OUT = Path("/tmp/movecount")
# station-field noise: "1-ДОК" is ДОК on channel 1, not a second man
PREFIX = re.compile(r"^\d+\s*[-.]\s*")


def norm(name: str) -> str:
    n = PREFIX.sub("", str(name or "").strip().upper()).strip(" .,")
    return n


def variants(name: str) -> set[str]:
    """`СЕРП/СЕРБ` is one man written two ways."""
    return {norm(v) for v in re.split(r"[/]", name or "") if norm(v)}


def main() -> None:
    per_freq = defaultdict(lambda: {"named": defaultdict(list), "unnamed": [], "eps": 0,
                                    "days": defaultdict(set)})
    for f in sorted(OUT.glob("*.json")):
        freq, day = f.stem.split("_", 1)
        try:
            eps = json.loads(f.read_text())
        except json.JSONDecodeError:
            print(f"!! не розібрав {f.name}")
            continue
        d = per_freq[freq]
        for e in eps:
            d["eps"] += 1
            for who in (e.get("named") or []):
                for v in variants(who):
                    if len(v) < 2:
                        continue
                    d["named"][v].append((day, e.get("time"), e.get("what")))
                    d["days"][day].add(v)
            n = e.get("unnamed")
            if isinstance(n, int) and n > 0:
                d["unnamed"].append((day, e.get("time"), n, (e.get("basis") or "")[:90]))

    for freq in sorted(per_freq):
        d = per_freq[freq]
        named = d["named"]
        print(f"\n{'='*72}\nЧАСТОТА {freq}")
        print(f"епізодів переміщення: {d['eps']}")
        print(f"ПОІМЕННО за тиждень (кожен рахується один раз): {len(named)}")
        for day in sorted(d["days"]):
            print(f"   {day}: {len(d['days'][day])} осіб — {', '.join(sorted(d['days'][day]))}")
        print("   усі імена:", ", ".join(sorted(named)))
        tot = sum(n for _d, _t, n, _b in d["unnamed"])
        print(f"БЕЗ ІМЕН: епізодів {len(d['unnamed'])}, разом названо {tot} осіб")
        for day, time, n, basis in d["unnamed"]:
            print(f"   {day} {time}: {n} — {basis}")
        print(f"ПІДСУМОК {freq}: щонайменше {len(named)} осіб поіменно; "
              f"плюс до {tot} згаданих лише числом (частина з них — ті самі люди)")


if __name__ == "__main__":
    main()
