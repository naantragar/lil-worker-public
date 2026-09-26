#!/usr/bin/env python3
"""The stitch: what captured documents are allowed to say about a callsign in the register.

    python3 tools/identity/doc_layer.py --net "УКХ р/м 1 мсб 38 омсбр 35 А ( ... )" ПСИХ ФОКС
    python3 tools/identity/doc_layer.py --audit knowledge/upstream/callsigns/IH_POZYVNI_23.09.2026_tech.txt

Imported by `callsign_file.py`. Until this module existed the two halves of the work never met:
the register was built from the air plus the analyst's archive, and everything read out of the
debriefs and rosters sat in `glossary.db` where nothing looked at it.

The admission rules, in the owner's words (26.09.2026) and with the measurement behind each:

1. **A callsign is matched together with its unit, never alone.** Measured: roughly one in three
   cross-source matches on the bare name is a different man (43 same-unit against 22 other-unit).
   The net header names the formation; the entry names the formation; the numbers must intersect.
   A document entry with no unit cannot clear this bar and stays in the technical copy.
2. **A contradiction admits NEITHER side.** Two documents that disagree about the same
   callsign+unit, or a document that disagrees with the role already in the register, produce no
   clean line at all — both versions go to the technical copy with their dates, because the
   disagreement is usually a rotation and is worth more than either version alone.
3. **No contradiction is not the same as useful.** A document saying «рядовий, стрілець» about a
   man we have nothing on adds a word, not an answer. Only a POST, a LEVEL or a FUNCTION is worth
   a line — that is what the file is asked for.
4. **The analyst's own files are not evidence.** `позивні *.docx` and `Еталонка` were ingested into
   the glossary like everything else; comparing the register against them is marking his homework
   with his own answer sheet. Sources of kind `register` are excluded here, as in
   `doc_vs_register.py`.

What a document may do, in order of value:
  - supply a LEVEL to a level-less role («ком склад» -> «ком склад, командир роти»), which is the
    material the hierarchy is built from;
  - supply a role where the register has none;
  - confirm a role we already hold (no new line, but it makes the pairing trustworthy).
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
GLOSS_DB = REPO / "knowledge" / "upstream" / "glossary" / "glossary.db"

UNIT_NUM = re.compile(r"\b(\d{2,4})\s*(омсбр|мсп|мсбр|отбр|абр|мсд)")
# The seniority ladder, shared with doc_vs_register.py in spirit: the one axis on which the
# analyst's vocabulary («ком склад», «кр», «кв») and a roster's vocabulary («Командир роты») meet.
LEVELS = [
    (5, "командир бригади/полку", r"командир\s*(бригад|полк)|комбриг|командир\s*\d+\s*(омсбр|мсп|бр)\b"),
    (4, "командир батальйону", r"командир\s*батальйон|комбат|командир\s*\d*\s*мсб\b"),
    (3, "командир роти", r"командир\s*рот|командир\s*\d*\s*(мср|шр)\b|\bкр\b"),
    (2, "командир взводу", r"командир\s*взвод|\bкв\b|нач\.?\s*рас|начальник\s*розрахунк"),
    (1, "командир відділення", r"командир\s*відділ|старший\s*стріл"),
    (0, "виконавець", r"рядов|стріл|оператор|водій|сапер|сапёр|механік|навідник|кулеметник|санітар"),
]
FUNCTIONS = {
    "накопичувач": r"накопичув",
    "БпЛА": r"бпла|бпак|оператор\s*бпл|дрон",
    "медик": r"медик|санітар|фельдшер",
    "логістика": r"логіст|постачан|підвіз",
    "зв'язок": r"зв'яз|связист|радист",
}
VAGUE = re.compile(r"ком\s*склад|командн", re.I)


def norm(s: str) -> str:
    return re.sub(r"[^А-ЯЁЇІЄҐA-Z]", "", (s or "").upper())


def units_in(text: str) -> set[str]:
    return {m[0] for m in UNIT_NUM.findall((text or "").lower())}


def level_of(text: str) -> tuple[int, str]:
    t = (text or "").lower()
    for lv, name, rx in LEVELS:
        if re.search(rx, t):
            return lv, name
    return -1, ""


def functions_of(text: str) -> set[str]:
    t = (text or "").lower()
    return {n for n, rx in FUNCTIONS.items() if re.search(rx, t)}


def load(db: Path = GLOSS_DB) -> dict[str, list[dict]]:
    """Every person the DOCUMENTS know about, keyed by normalised callsign. The analyst's own
    files are excluded — see rule 4."""
    if not db.exists():
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out: dict[str, list[dict]] = defaultdict(list)
    for r in con.execute(
            "SELECT e.term, e.meaning, e.unit, e.note, e.source, e.dated, s.kind AS skind "
            "FROM entries e JOIN sources s ON s.file = e.source "
            "WHERE e.kind='person' AND s.kind != 'register'"):
        d = dict(r)
        d["level"], d["level_name"] = level_of(d["meaning"])
        d["funcs"] = functions_of(d["meaning"])
        out[norm(d["term"])].append(d)
    return out


def verdict(name: str, role: str, net_header: str, docs: dict) -> dict:
    """What the documents are allowed to contribute for this callsign on this net.

    Returns {'add': str|None, 'why': str, 'status': str, 'sources': [...]}.
    `add` is the text to append to the clean line — None means the clean copy is untouched.
    """
    hits = docs.get(norm(name)) or []
    if not hits:
        return {"add": None, "status": "немає в документах", "why": "", "sources": []}

    net_units = units_in(net_header)
    same = [h for h in hits if net_units and units_in(h["unit"]) and net_units & units_in(h["unit"])]
    if not same:
        other = sorted({(h["unit"] or "?") for h in hits})
        return {"add": None, "status": "інший підрозділ - не наш чоловік",
                "why": "документ каже про " + ", ".join(other)[:80], "sources": [h["source"] for h in hits]}

    # rule 2 — the document side must agree with ITSELF first
    levels = {h["level"] for h in same if h["level"] >= 0}
    if len(levels) > 1:
        return {"add": None, "status": "документи суперечать один одному",
                "why": "; ".join(f"{(h['meaning'] or '')[:40]} [{h['source'][:24]}]" for h in same)[:200],
                "sources": [h["source"] for h in same]}

    # A claim that names NO unit cannot promote a man (rule 1) — but it can still CONTRADICT one,
    # and that must count. БУДАН, 26.09.2026: one debrief made him a company commander with the
    # unit named, another made him a BATTALION commander with no unit, and the unit filter hid the
    # second — the stitch was one line away from printing a rank the sources disagree about.
    unitless = [h for h in hits if h["level"] >= 0 and not units_in(h["unit"])]
    if unitless and levels and {h["level"] for h in unitless} - levels:
        return {"add": None, "status": "суперечать документи (друга версія без підрозділу)",
                "why": "; ".join(f"{(h['meaning'] or '')[:40]} [{h['source'][:24]}]"
                                 for h in same + unitless)[:200],
                "sources": [h["source"] for h in same + unitless]}

    doc_level = max(levels) if levels else -1
    doc_funcs = set().union(*[h["funcs"] for h in same]) if same else set()
    best = max(same, key=lambda h: (h["level"], len(h["meaning"] or "")))
    reg_level, _ = level_of(role)
    reg_funcs = functions_of(role)
    reg_vague = bool(VAGUE.search(role or ""))

    # rule 2 again — now against what the register already says
    if reg_level >= 0 and doc_level >= 0 and reg_level != doc_level:
        return {"add": None, "status": "суперечить ролі з ефіру",
                "why": f"у нас рівень {reg_level}, у документі {doc_level}: {(best['meaning'] or '')[:60]}",
                "sources": [best["source"]]}
    if reg_vague and doc_level == 0:
        return {"add": None, "status": "суперечить: у нас командир, у документі виконавець",
                "why": (best["meaning"] or "")[:70], "sources": [best["source"]]}

    # THE VALUABLE CASE: the register knows he commands but not what — the document says what.
    if reg_vague and doc_level > 0:
        _, lname = level_of(best["meaning"])
        return {"add": lname, "status": "документ дав РІВЕНЬ", "why": (best["meaning"] or "")[:70],
                "sources": [best["source"]]}
    if not (role or "").strip():
        if doc_level > 0 or doc_funcs:
            txt = best["meaning"] or ""
            return {"add": txt[:70].strip(" ,"), "status": "документ дав роль порожньому",
                    "why": txt[:70], "sources": [best["source"]]}
        return {"add": None, "status": "документ знає його, але ролі не дає (рядовий)",
                "why": (best["meaning"] or "")[:60], "sources": [best["source"]]}
    if reg_funcs & doc_funcs or reg_level == doc_level:
        return {"add": None, "status": "підтверджує наше", "why": (best["meaning"] or "")[:60],
                "sources": [best["source"]]}
    return {"add": None, "status": "незрівнянно", "why": (best["meaning"] or "")[:60],
            "sources": [best["source"]]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("callsigns", nargs="*")
    ap.add_argument("--net", default="")
    ap.add_argument("--audit", help="a *_tech.txt to run the whole file through")
    a = ap.parse_args()
    docs = load()
    print(f"позивних у документах: {len(docs)}", file=sys.stderr)

    if a.audit:
        from collections import Counter
        net, tally, gains = "", Counter(), []
        for line in Path(a.audit).read_text(encoding="utf-8").splitlines():
            if line.startswith(("УКХ", "КХ")):
                net = line.strip()
                continue
            if not line.startswith("   "):
                continue
            body = line.strip().split(" [")[0]
            name, role = (body.split(" - ", 1) + [""])[:2] if " - " in body else (body, "")
            v = verdict(name.strip(), role.strip(), net, docs)
            tally[v["status"]] += 1
            if v["add"]:
                gains.append((name.strip(), role.strip(), v["add"], v["status"], net[:40]))
        for k, n in tally.most_common():
            print(f"  {k:<46} {n}")
        print(f"\n=== що додалося б у чистовик: {len(gains)} ===")
        for n, r, add, st, net in gains:
            print(f"  {n:<12} {r or '(порожньо)':<26} -> +{add}   [{st}]")
        return

    for cs in a.callsigns:
        v = verdict(cs, "", a.net, docs)
        print(f"{cs}: {v['status']}  {v['why']}  -> {v['add']}")


if __name__ == "__main__":
    main()
