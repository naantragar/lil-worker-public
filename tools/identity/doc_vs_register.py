#!/usr/bin/env python3
"""Measure the analyst's register against the captured documents. Changes nothing.

    python3 tools/identity/doc_vs_register.py [--tech <IH_POZYVNI_*_tech.txt>]

The question this answers is NOT «what is this man's post» — it is «how often do two independent
sources say the same thing about the same callsign». The register is built from the air (behaviour);
the glossary is built from debriefs and rosters (paperwork). Where they agree, the pairing
callsign+net is trustworthy enough to carry weight. Where they disagree, one of three things is
true and all three are worth knowing: the man rotated, the callsign is shared by two people, or one
of the sources is wrong (a prisoner under stress misremembers, and a document can be planted).

Nothing here is a verdict. It is a count.

The comparison is made on a SENIORITY LEVEL, not on wording, because the two vocabularies differ:
the analyst writes «ком склад», the roster writes «Командир роты». Level is the one axis both speak.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "knowledge" / "upstream" / "glossary" / "glossary.db"
DEFAULT_TECH = REPO / "knowledge" / "upstream" / "callsigns" / "IH_POZYVNI_23.09.2026_tech.txt"

# Seniority ladder. -1 means «the wording names a function, not a level» — those are compared on
# function instead, never on level, and never counted as a disagreement.
LEVELS = [
    (5, r"командир\s*(бригад|полк)|комбриг|командир\s*\d+\s*(омсбр|мсп|бр)\b|нш\s*(бригад|полк)"),
    (4, r"командир\s*батальйон|комбат|командир\s*\d*\s*мсб\b|заступник\s*командира\s*батальйон|нш\s*батальйон"),
    (3, r"командир\s*рот|\bкр\b|командир\s*\d*\s*(мср|шр)\b|заступник\s*командира\s*рот|старшина\s*рот"),
    (2, r"командир\s*взвод|\bкв\b|нач\.?\s*рас|начальник\s*розрахунк|начальник\s*расч|мол\s*ком\s*склад"),
    (1, r"командир\s*відділ|старший\s*стріл|старший\s*групи|снайпер-?інструктор"),
    (0, r"рядов|стріл|оператор|водій|\bвод\b|сапер|сапёр|механік|навідник|номер\s*розрахунк|"
        r"кулеметник|гранатометник|санітар|технік|стрелок"),
]
FUNCTIONS = {
    "накопичувач": r"накопичув",
    "БпЛА": r"бпла|бпак|бас\b|оператор\s*бпл|дрон",
    "медик": r"медик|санітар|фельдшер",
    "логістика": r"логіст|постачан|підвіз|тил\b",
    "зв'язок": r"зв'яз|связист|радист",
}
# «ком склад» is the analyst's catch-all for "a commander, level unspecified". It is not a level
# and must not be compared as one — but it DOES assert «this man commands», which a document
# calling the same callsign «рядовий стрілець» contradicts. That specific pair is the finding.
COMMANDER_VAGUE = re.compile(r"ком\s*склад|командн", re.I)


def level_of(text: str) -> int:
    t = (text or "").lower()
    for lv, rx in LEVELS:
        if re.search(rx, t):
            return lv
    return -1


def functions_of(text: str) -> set[str]:
    t = (text or "").lower()
    return {name for name, rx in FUNCTIONS.items() if re.search(rx, t)}


def norm(s: str) -> str:
    return re.sub(r"[^А-ЯЁЇІЄҐA-Z]", "", (s or "").upper())


def register_rows(tech: Path) -> list[tuple[str, str, str]]:
    """(callsign, role, net) for every line the ANALYST supplied a role for."""
    out, net = [], ""
    for line in tech.read_text(encoding="utf-8").splitlines():
        if line.startswith("УКХ") or line.startswith("КХ"):
            net = line.strip()
            continue
        if not line.startswith("   ") or "джерело: архів аналітика" not in line:
            continue                       # only HIS roles; ours would be marking our own homework
        body = line.strip().split(" [")[0]
        if " - " not in body:
            continue                       # a bare name: the role lives nowhere, nothing to compare
        name, role = body.split(" - ", 1)
        out.append((name.strip(), role.strip(), net))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tech", default=str(DEFAULT_TECH))
    ap.add_argument("--show", type=int, default=40)
    a = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    docs: dict[str, list[dict]] = defaultdict(list)
    # EXCLUDE the analyst's own files from the document side. `позивні 05.08.docx` and `Еталонка`
    # were ingested into the glossary like everything else — comparing the register against them
    # would be marking his homework with his own answer sheet, and on the first run it produced 12
    # of the 24 "agreements". An independent source is a debrief, a roster, a code table.
    for r in con.execute(
            "SELECT e.term, e.meaning, e.unit, e.note, e.source, e.dated FROM entries e "
            "JOIN sources s ON s.file = e.source WHERE e.kind='person' AND s.kind != 'register'"):
        docs[norm(r["term"])].append(dict(r))

    reg = register_rows(Path(a.tech))
    verdicts = Counter()
    agree, clash, extra = [], [], []
    for name, role, net in reg:
        hits = docs.get(norm(name)) or []
        if not hits:
            verdicts["немає в документах"] += 1
            continue
        rl, rf = level_of(role), functions_of(role)
        vague = bool(COMMANDER_VAGUE.search(role))
        best = None
        for h in hits:
            dl, df = level_of(h["meaning"]), functions_of(h["meaning"])
            # «командний склад ППС» is a COMMANDER, but the ladder saw «стрільба» inside it and
            # called him a private. A vague commander on the document side outranks the ladder.
            if COMMANDER_VAGUE.search(h["meaning"] or "") and not re.search(r"рядов", (h["meaning"] or "").lower()):
                dl = -1
                if vague:
                    df = df | rf | {"командир"}
                    rf = rf | {"командир"}
            if rl >= 0 and dl >= 0:
                verdict = "збіг рівня" if rl == dl else ("документ СТАРШЕ" if dl > rl else "документ МОЛОДШЕ")
            elif vague and dl == 0:
                verdict = "СУПЕРЕЧКА: у нас командир, у документі рядовий"
            elif rf & df:
                verdict = "збіг функції"
            else:
                verdict = "незрівнянно"
            row = (name, role, net[:46], h["meaning"], h["unit"], h["note"], h["source"], verdict)
            # keep the most informative of several document rows for this callsign
            rank = {"збіг рівня": 0, "збіг функції": 1, "СУПЕРЕЧКА: у нас командир, у документі рядовий": 2,
                    "документ СТАРШЕ": 3, "документ МОЛОДШЕ": 4, "незрівнянно": 5}
            if best is None or rank[verdict] < rank[best[-1]]:
                best = row
        verdicts[best[-1]] += 1
        (agree if best[-1].startswith("збіг") else clash if "СУПЕР" in best[-1] or "СТАРШЕ" in best[-1]
         or "МОЛОДШЕ" in best[-1] else extra).append(best)

    print(f"позивних з роллю ВІД АНАЛІТИКА у файлі: {len(reg)}")
    for k, v in verdicts.most_common():
        print(f"   {k:<48} {v}")
    print(f"\n=== ЗБІГИ (дві незалежні дороги кажуть те саме) — {len(agree)} ===")
    for r in agree[:a.show]:
        print(f"  {r[0]:<12} наше: {r[1][:40]:<40} | документ: {(r[3] or '')[:60]}\n"
              f"               {r[5] or ''} · {r[6][:46]}")
    print(f"\n=== РОЗБІЖНОСТІ — {len(clash)} ===")
    for r in clash[:a.show]:
        print(f"  {r[0]:<12} [{r[7]}]\n               наше: {r[1][:56]}\n"
              f"               док:  {(r[3] or '')[:70]}  ({r[4] or 'підрозділ не вказано'})\n"
              f"               {r[5] or ''} · {r[6][:46]}")


if __name__ == "__main__":
    main()
