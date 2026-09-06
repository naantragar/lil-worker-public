#!/usr/bin/env python3
"""Freeze the answer key: every role the live analyst ever wrote next to a callsign.

This file is the ONLY thing that makes the module measurable rather than plausible, so it is built
by its own code path and never by the one that produces predictions. It is read-only over
`reports.db` and writes a single JSON.

    python3 tools/callsign_roles/build_gold.py [-o gold.json]

A callsign may carry more than one reading - `ТИХИЙ` is `ком склад` one day and `накопичувач`
another. That is kept, not cleaned: at evaluation time ANY of his recorded readings counts as
correct, because we cannot tell from here whether the man changed job, the analyst was describing a
different aspect, or two men share the name on two nets.
"""

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REPORTS_DB = REPO / "knowledge" / "upstream" / "reports.db"

# The analyst's own vocabulary, grouped into the classes an evaluator can fairly compare.
# Derived from the frequency table of his actual wording, not invented: `ком склад` 261 rows,
# `БпЛА логістики` 44, `накопичувач` 30, `проміжний накопичувач` 23, `кр` 17, `мол ком склад` 15...
# The order matters - the first pattern that matches wins, so the more specific ones come first.
# ПРІОРИТЕТ У СЕРЕДИНІ ОДНІЄЇ ФРАЗИ, і він не довільний: людина класифікується за тим, ЯКУ ПОСАДУ
# вона обіймає, а не за тим, чим завідує. `ком склад БпЛА` - це командний склад над розрахунком
# БпЛА, а не розрахунок. Поки БпЛА стояв першим, ми писали `ком склад БпЛА`, аналітик писав
# `ком склад`, і звірка рахувала це ПОМИЛКОЮ - хоча ми сказали те саме, тільки точніше (БАЗА,
# БОЛЬШОЙ, 05.09.2026). Тому командний склад тепер попереду всіх.
CLASSES = [
    # `км склад` is his own typo for `ком склад`; `ком розрахунку`, `командир`, `кр 2мср` are the
    # same rank written differently. Every one of these was landing in `інше` and scoring a correct
    # answer as a miss (МАНЬЯК, СОБОЛЬ, 05.09.2026).
    ("командний склад", r"(ком|км)\s*склад|ком\s*розрахунк|командув|командир|\bкр\b|"
                        r"^ст мережі$|начальник|комбат|комроти|старшин|старший|керівн"),
    ("БпЛА",          r"бпла|дрон|mavic"),
    ("накопичувач",   r"накопич"),
    ("укриття/СП",    r"укритт|^сп$|спостережн"),
    ("зв'язок",       r"зв'?язк|радист|ретрансл|старлінк|starlink|інтернет|реб\b"),
    ("медицина",      r"медик|мед\b|евакуац"),
    ("транспорт",     r"водій|транспортув|перевез|переміщенн\w* на (тз|ат)\b|на ат\b"),
    ("логістика",     r"логіст|доставк|підвіз|мтз"),
]

# Text that landed in the role column at import time but is NOT a role: the analyst's free note
# about a man, or a bare number. Grading ourselves against these is grading against nothing -
# `регулярне прибуття/вибуття на позиції СМИРНИЙ` cannot be inferred from the air by anybody.
NOT_A_ROLE = re.compile(r"^\d+$|^[\d\s.,;-]+$|прибуття|вибуття|^замість\b|^\W*$", re.I)


def classes_of(role: str) -> set:
    """Every class the wording covers - counting the analyst's compounds as ONE thing.

    The distinction that matters is not which words appear, it is whether the wording is one of HIS
    phrases or two of them glued together:

        `БпЛА логістики`                     - one phrase, one class (БпЛА). He writes it 44 times.
        `ком склад БпЛА`                     - one phrase, one class (командний склад over drones)
        `мол ком склад, проміжний накопичувач` - TWO phrases joined by a comma. That is a hedge:
                                               the model could not choose, so it named both.

    So the role is split on the joiners first, and each PART is classified on its own by priority
    (БпЛА beats логістика, because drone logistics is a drone job). More than one distinct class
    across the parts means the answer refused to choose.
    """
    parts = [p for p in re.split(r"[,;/]|\bта\b|\bі\b|\band\b",
                                 " ".join((role or "").lower().split())) if p.strip()]
    out = set()
    for part in parts or [""]:
        for name, pat in CLASSES:          # first match wins WITHIN one phrase
            if re.search(pat, part):
                out.add(name)
                break
        else:
            out.add("інше")
    # A tail that names no class of its own is a QUALIFIER, not a second role. The analyst writes
    # `ком склад, супровід шг` 15 times and `ком склад шг, ім кв.` 11 times - the comma there says
    # more about the same man, it does not name a second job. Only two SUBSTANTIVE classes make a
    # hedge.
    substantive = out - {"інше"}
    return substantive or {"інше"}


def is_hedge(role: str) -> bool:
    """Two of his phrases welded with a comma is not a reading, it is an unwillingness to choose."""
    return len(classes_of(role)) > 1


def classify(role: str) -> str:
    """One class, for tables that need a single label."""
    c = classes_of(role)
    return next(iter(c)) if len(c) == 1 else "склейка(" + "+".join(sorted(c)) + ")"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default=str(Path(__file__).with_name("gold.json")))
    ap.add_argument("--db", default=str(REPORTS_DB))
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        sys.exit(f"немає бази звітів: {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    entries = defaultdict(lambda: {"roles": {}, "networks": {}, "rows": 0})
    for cs, role, net_id in con.execute(
            "SELECT callsign, role, network_id FROM roster"):
        name = " ".join(str(cs or "").split()).upper()
        if not name or len(name) < 2 or re.fullmatch(r"[\d.,/ +-]+", name):
            continue           # a frequency that slipped into the callsign column is not a man
        e = entries[name]
        e["rows"] += 1
        r = " ".join(str(role or "").split()).lstrip(",-–— ").strip()
        if r and not NOT_A_ROLE.match(r):
            e["roles"][r] = e["roles"].get(r, 0) + 1
        elif r:
            e.setdefault("notes", []).append(r)      # kept visible, never graded against

    # which networks each callsign was written on, and under what header
    for cs, net_id, header, freqs in con.execute(
            "SELECT ro.callsign, n.id, n.header, n.freqs FROM roster ro "
            "JOIN networks n ON n.id = ro.network_id"):
        name = " ".join(str(cs or "").split()).upper()
        if name in entries:
            entries[name]["networks"][str(net_id)] = {
                "header": " ".join(str(header or "").split()),
                "freqs": freqs,
            }

    # `СПАРТАК` and `СПАРТАК/ПАРТАК` are one man the analyst wrote two ways. The register already
    # folds them (`fold_variants` in run.py); the answer key must not count him twice, or he is
    # graded twice on the same evidence and skews every number he touches.
    def variants(n):
        return {v.strip().strip("«»\"'") for v in re.split(r"[,/]", n) if v.strip()}

    merged: dict[str, dict] = {}
    for name in sorted(entries, key=lambda n: (-len(n), n)):     # widest spelling wins
        e = entries[name]
        vs = variants(name)
        for kept, ke in merged.items():
            if vs & variants(kept):
                ke["rows"] += e["rows"]
                for r, n in e["roles"].items():
                    ke["roles"][r] = ke["roles"].get(r, 0) + n
                ke["networks"].update(e["networks"])
                ke.setdefault("aliases", []).append(name)
                ke.setdefault("notes", []).extend(e.get("notes", []))
                break
        else:
            merged[name] = e
    entries = merged

    gold, unlabelled = {}, {}
    for name, e in entries.items():
        rec = {
            "callsign": name,
            "roles": sorted(e["roles"], key=lambda r: -e["roles"][r]),
            "classes": sorted({classify(r) for r in e["roles"]}),
            "roster_rows": e["rows"],
            "networks": e["networks"],
            "aliases": e.get("aliases", []),
            "notes_not_roles": e.get("notes", []),
        }
        (gold if rec["roles"] else unlabelled)[name] = rec

    by_class = defaultdict(int)
    for rec in gold.values():
        by_class[rec["classes"][0] if len(rec["classes"]) == 1 else "кілька класів"] += 1

    out = {
        "_note": [
            "Answer key for tools/callsign_roles. Written by the live analyst, imported from his",
            "own reports - NEVER produced by the inference path. Any listed reading counts as",
            "correct at evaluation time.",
        ],
        "source_db": str(db),
        "counts": {
            "callsigns_total": len(entries),
            "with_role": len(gold),
            "without_role": len(unlabelled),
            "multi_reading": sum(1 for r in gold.values() if len(r["roles"]) > 1),
            "by_class": dict(sorted(by_class.items(), key=lambda kv: -kv[1])),
        },
        "gold": dict(sorted(gold.items())),
        "unlabelled": dict(sorted(unlabelled.items())),
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    c = out["counts"]
    print(f"позивних усього: {c['callsigns_total']}")
    print(f"  з роллю від аналітика: {c['with_role']}   (еталон)")
    print(f"  без ролі:              {c['without_role']}   (кандидати на опис)")
    print(f"  з кількома прочитаннями: {c['multi_reading']}")
    print("\nрозподіл еталону по класах:")
    for k, n in c["by_class"].items():
        print(f"   {n:>4}  {k}")
    print(f"\nзаписано: {args.out}")


if __name__ == "__main__":
    main()
