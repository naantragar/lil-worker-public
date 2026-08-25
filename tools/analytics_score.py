#!/usr/bin/env python3
"""Score a run against the analyst's own report for the same day — the project's ONE ruler.

    python3 tools/analytics_score.py --day 22.08 --events <file.json> [--events ...] [--misses]

Every tuning decision in this pipeline is made on two numbers: how much of the analyst's report we
covered, and how much extra we wrote. They were being recomputed ad hoc in each session with slightly
different regexes, which produced three different "coverage" figures for the same run and wasted a
round of work chasing the difference. This file is the single implementation.

WHY THE MATCH IS NOT JUST "shared callsign + ±25 min":
that version reported false pairs. His `початок переміщення в\\с ЛЕЛИК у супроводі БпЛА КРЫ` was
counted as covered by our `в\\с ЛЕЛИК відмовляється продовжувати переміщення через поранення ноги` —
same man, same quarter hour, different events. So a pair must ALSO agree on what KIND of event it is.
Categories are ordered by priority because one line can mention several things: a casualty outranks
the fire that caused it, which outranks the movement it happened during.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RAW = REPO / "knowledge" / "upstream" / "reports_raw"

TIME_TOL_MIN = 25

# Ukrainian/Russian letter pairs the transcriber mixes freely; folded before comparing names.
_FOLD = str.maketrans({"І": "И", "Ї": "И", "Є": "Е", "Ґ": "Г"})
_NAME = re.compile(r"\b([А-ЯЁЇІЄҐ]{3,})\b")
_NAME_STOP = {"ВУ", "СОУ", "РОВ", "БПЛА", "МТЗ", "ФПВ", "БК", "ДРГ", "СП", "НВ", "ОР", "ЛС",
              "ДМР", "УКХ", "ДТП", "МСП", "ОМСБР"}

# Highest priority first — the first pattern that hits names the line.
_CATEGORIES = [
    ("200",          r"\b200\b|загин|смерт|реаніма"),
    ("300",          r"\b300\b|поранен|контуз|осколков"),
    ("ВУ",           r"\bВУ\b|ураженн|скид|обстріл|камікадзе|міномет"),
    ("постачання",   r"постачанн|\bМТЗ\b|доставк|евакуац|транспортув|поломк|паливо"),
    ("накопичення",  r"скупченн|накопичув|збір о\\с"),
    ("зв'язок",      r"зв'язк|рація|р\\с|канал|позивн.{0,12}змін"),
    ("розташування", r"розташуванн|знаходить|місцезнаходж|перебува"),
    ("план",         r"заплан|планує|має намір|наказано"),
    ("рух",          r"переміщенн|перемішенн|висуван|маршрут|заведен|підход|\bрух|відкат|зупинк"),
]


def names(text: str | None) -> set[str]:
    return {n.translate(_FOLD) for n in _NAME.findall(str(text or "")) if n not in _NAME_STOP}


def category(text: str | None) -> str:
    t = str(text or "")
    for label, pat in _CATEGORIES:
        if re.search(pat, t, re.I):
            return label
    return "інше"


def minutes(text: str | None) -> int | None:
    m = re.search(r"(\d{1,2}):(\d{2})", str(text or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def load_report(day: str) -> list[dict]:
    """The analyst's own lines for a day. Continuation lines carry only HH:MM, so the date is
    optional in the pattern — dropping them lost two thirds of a real report."""
    path = RAW / f"REP_{day}.2026.txt"
    line = re.compile(r"^\s*(?:\d{2}\.\d{2}(?:\.\d{4})?,?\s+)?(\d{1,2})[:.](\d{2})(?![\d.])\s*(.*)$")
    out, net = [], None
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s:
            continue
        if "УКХ" in s:
            net = s[:45]
            continue
        m = line.match(s)
        if m and len(s) > 20:
            out.append({"t": int(m.group(1)) * 60 + int(m.group(2)),
                        "text": m.group(3).strip(), "net": net})
    return out


def matches(his: dict, ours: dict) -> bool:
    t = minutes(ours.get("time"))
    if t is None or abs(t - his["t"]) > TIME_TOL_MIN:
        return False
    if not (names(his["text"]) & names(ours.get("text"))):
        return False
    return category(his["text"]) == category(ours.get("text"))


def score(day: str, events: list[dict]) -> dict:
    his = load_report(day)
    covered, missed = [], []
    for h in his:
        hit = next((o for o in events if matches(h, o)), None)
        (covered if hit else missed).append((h, hit))
    used = {id(o) for _, o in covered if o}
    extra = [o for o in events if id(o) not in used]
    return {"his": len(his), "ours": len(events), "covered": len(covered),
            "pct": round(100 * len(covered) / len(his)) if his else 0,
            "missed": [h for h, _ in missed], "extra": extra}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="DD.08 — picks knowledge/upstream/reports_raw/REP_<day>.2026.txt")
    ap.add_argument("--events", action="append", required=True, help="events/candidates .json (repeatable)")
    ap.add_argument("--misses", action="store_true", help="print the analyst's lines we did not cover")
    ap.add_argument("--extra", action="store_true", help="print our lines that have no counterpart")
    a = ap.parse_args()

    print(f"{'файл':<44}{'наших':>7}{'його':>6}{'покрито':>9}{'%':>5}{'зайвих':>8}")
    last = None
    for f in a.events:
        p = Path(f)
        r = score(a.day, json.loads(p.read_text()))
        print(f"{p.name[:43]:<44}{r['ours']:>7}{r['his']:>6}{r['covered']:>9}{r['pct']:>5}"
              f"{len(r['extra']):>8}")
        last = r

    if a.misses and last:
        print("\nЙОГО рядки без нашої пари:")
        for h in last["missed"]:
            print(f"   {h['t']//60:02d}:{h['t']%60:02d} [{category(h['text'])}] {h['text'][:85]}")
    if a.extra and last:
        print("\nНАШІ рядки без його пари:")
        for o in last["extra"]:
            print(f"   {str(o.get('time',''))[-5:]} [{category(o.get('text'))}] {str(o.get('text'))[:85]}")


if __name__ == "__main__":
    main()
