#!/usr/bin/env python3
"""Lift the numeric/signal codes out of the glossary into a flat file the alerter can consult.

    python3 tools/alerts/build_codes.py          # rewrites tools/alerts/codes.json

Why a generated file and not a live query: the alerter runs every three minutes from cron and must
not depend on the glossary's schema or on it being writable. Re-run this after new documents land.

The codes are UNIT-SCOPED on purpose, and that is the whole point of the exercise: `412` is an FPV
in 1198 мсп and a «Баба-Яга» in 38 омсбр. Handing the model the wrong unit's meaning would be worse
than handing it nothing.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GLOSSARY = REPO / "knowledge" / "upstream" / "glossary" / "glossary.db"
OUT = Path(__file__).resolve().parent / "codes.json"

# a formation as it is written in a net header: «38 омсбр», «1198 мсп», «42 мсд», «305 абр»
FORMATION = re.compile(r"(\d{1,4})\s*(омсбр|мсбр|мсп|мсд|абр|бр|мбр|полк|а\b)", re.I)


def formations(text: str) -> set[str]:
    return {m.group(1) for m in FORMATION.finditer(text or "")}


def main() -> None:
    con = sqlite3.connect(f"file:{GLOSSARY}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT term, meaning, coalesce(unit,'') FROM entries "
        "WHERE kind IN ('numeric_code','signal_code')").fetchall()
    con.close()

    codes: dict[str, list[dict]] = {}
    for term, meaning, unit in rows:
        key = re.sub(r"\D", "", str(term))
        if not key or not (2 <= len(key) <= 3):
            continue
        entry = {"meaning": str(meaning).strip(), "unit": unit.strip(),
                 "formations": sorted(formations(unit))}
        bucket = codes.setdefault(key, [])
        if not any(e["meaning"] == entry["meaning"] and e["unit"] == entry["unit"] for e in bucket):
            bucket.append(entry)

    OUT.write_text(json.dumps({"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "source": str(GLOSSARY.relative_to(REPO)),
                               "codes": codes}, ensure_ascii=False, indent=1), encoding="utf-8")
    multi = {k: v for k, v in codes.items() if len({e["meaning"] for e in v}) > 1}
    print(f"кодів {len(codes)}, з них зі СУПЕРЕЧЛИВИМИ значеннями між підрозділами {len(multi)}: "
          f"{', '.join(sorted(multi)[:12])}")
    print(OUT)


if __name__ == "__main__":
    main()
