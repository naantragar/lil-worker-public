#!/usr/bin/env python3
"""Find the intercepts where somebody is WALKING and somebody is DIRECTING them.

    python3 tools/identity/movement_screen.py --days 3 [--group "Invisible Hand"]

Why this exists. `guide_layer.py` scores who leads whom — but its vocabulary
(`корегував`, `супровід`, `веде`, `маршрут`) is UKRAINIAN, so it reads our own analyst's comment on
the intercept, not the enemy's speech. A man is counted as a handler when a human already wrote that
he was one. That is circular, and it silently misses every net whose intercepts arrive without such
a comment.

This screen reads THEIR words instead, and it is built to be MEASURED rather than trusted, by
comparing two independently-built sets over the same window:

    A — intercepts behind the report's own movement lines (`_src_ref` → `_sources.json`).
        High precision: a human-grade pipeline already decided a march happened there.
    B — intercepts matched by the lexicon below.

    A ∩ B   both agree — the lexicon works here
    A \\ B   the report saw movement, the lexicon did not → WORDS WE ARE MISSING, read them and
            extend LEX. This is the whole point of the comparison.
    B \\ A   the lexicon fired where the report wrote no movement line → either noise, or movement
            the report dropped. Both are worth knowing; the report is not the ground truth, it is
            the other opinion.

The screen deliberately does NOT decide who guides whom. It narrows tens of thousands of intercepts
to a few hundred worth a model's attention, and the model's verdict is a separate step — the same
split that made the report cheap: machinery selects, the model reads.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CORPUS = Path("~/wa-monitor/messages.db")
REPORTS = REPO / "knowledge" / "upstream" / "reports_out"
OUT_DIR = REPO / "knowledge" / "upstream" / "callsigns"

# Their speech, not ours. Grouped so a hit can be explained, and so the groups can be weighted
# later: an imperative plus a direction is a much stronger sign than a lone «правее».
LEX = {
    "команда руху": r"начина\w*\s+движ|двигай|двигат|выдвигай|выходи\b|выход\w*\s+(?:в|на)\b|"
                    r"иди\b|идёшь|идешь|пошёл|пошел\b|беги\b|ползи|работай\s+вперед|"
                    r"продолжай\s+движ|возвращайся|возвращай|отходи|отступай|разворачивай|"
                    r"перелаз|перелез|переполз|обход\w*\s+(?:её|ее|его)|подойди|подходи|"
                    r"дойд\w+|доберись|прибыл|прибыва\w+|поверн\w+|сворачивай|заходи\b|"
                    r"выбирайся|уходи\b|топай|топим\b|шуруй",
    "зупинка":      r"стой\b|стоп\b|остановись|замри|не\s+двигай|стоим\b|жди\b|ожидай",
    "напрямок":     r"на\s+север|на\s+юг|на\s+запад|на\s+восток|севернее|южнее|западнее|восточнее|"
                    r"левее|правее|налево|направо|прямо\b|назад\b|обратно\b|в\s+обход",
    "темп":         r"быстре\w|шустр\w|не\s+торопись|не\s+спеши|аккуратн\w|полшаг|пол\s*шаг|"
                    r"медленн\w|ускор\w",
    "відстань":     r"\d{1,4}\s*метр|\d{1,3}\s*м\b|немного\s+остал|чуть\s*,?\s*чуть|близко\b|"
                    r"далеко\b|подход\w+\s+к\b|совсем\s+немного|осталось\s+немного|потерпи",
    "орієнтир":     r"лесопол\w|посадк\w|открытк\w|поле\b|дорог\w|перекрест\w|балк\w|овраг\w|"
                    r"блиндаж|окоп\w|траншея|кусты\b",
    "супровід":     r"веду\s+теб|веди\s+ег|я\s+тебя\s+вижу|вижу\s+теб|подсвеч|подсвет|"
                    r"кориктир|корректир|наблюдаю\s+за\s+тоб|над\s+тобой|сориентир|ориентир\w*\s+ег|"
                    r"подведем|подведём|подвед\w+\s+теб|я\s+тебя\s+встречу|встречу\s+теб|"
                    r"тебя\s+ведет|тебя\s+ведёт|голосом\s+к\s+себе|я\s+тебя\s+скоректир",
}
LEX_RE = {k: re.compile(v, re.I) for k, v in LEX.items()}
# Two different kinds of sign are normally demanded — a lone «правее» is not a march. But two of
# the groups are strong enough alone: telling a man where to go, and saying you are watching him
# move. Measured on the first 2-day run: demanding two signs threw away «чуть-чуть осталось, давай»
# and «сориентируй его голосом к себе», which are as pure a case of guidance as the corpus holds.
MIN_GROUPS = 2
STRONG = {"супровід", "команда руху"}
MOVE_LINE = re.compile(r"^\s*(переміщення|рух|маршрут переміщення|почато переміщення)", re.I)


def corpus_rows(days: int, group: str) -> list[dict]:
    lo = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
    con = sqlite3.connect(f"file:{CORPUS}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(
        "SELECT id, timestamp, COALESCE(text,'') AS text, COALESCE(caption,'') AS caption "
        "FROM messages WHERE group_name=? AND timestamp>? ORDER BY timestamp", (group, lo))]


def screen(text: str) -> list[str]:
    """Which KINDS of movement sign this intercept carries. Empty = not a candidate."""
    return [k for k, rx in LEX_RE.items() if rx.search(text)]


def report_movement_ids(days: int) -> tuple[set[str], dict[str, str]]:
    """Message ids behind the report's own movement lines, with the line that cited them."""
    ids: set[str] = set()
    why: dict[str, str] = {}
    cutoff = datetime.now() - timedelta(days=days + 1)
    for ev_path in sorted(REPORTS.glob("ZVIT_*_events.json")):
        src_path = ev_path.with_name(ev_path.name.replace("_events.json", "_sources.json"))
        if not src_path.exists():
            continue
        try:
            ev = json.loads(ev_path.read_text())
            src = json.loads(src_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        ev = ev["events"] if isinstance(ev, dict) else ev
        for e in ev:
            if not MOVE_LINE.match(e.get("text") or ""):
                continue
            for ref in (e.get("_src_ref") or []):
                v = src.get(ref)
                if not v or not v.get("msg_id"):
                    continue
                try:
                    if datetime.strptime(v["date"], "%d.%m.%Y") < cutoff:
                        continue
                except (ValueError, KeyError):
                    pass
                ids.add(v["msg_id"])
                why.setdefault(v["msg_id"], e["text"][:90])
    return ids, why


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--group", default="PATAGONIA_GP")
    ap.add_argument("--out", default=None)
    ap.add_argument("--show", type=int, default=6, help="how many misses to print for reading")
    a = ap.parse_args()

    rows = corpus_rows(a.days, a.group)
    if not rows:
        sys.exit(f"у групі {a.group} за {a.days} діб немає перехоплень")
    A, why = report_movement_ids(a.days)
    B: dict[str, list[str]] = {}
    for r in rows:
        hits = screen(r["text"] + " " + r["caption"])
        if len(hits) >= MIN_GROUPS or (hits and set(hits) & STRONG):
            B[r["id"]] = hits
    by_id = {r["id"]: r for r in rows}
    A_here = {i for i in A if i in by_id}          # only what this window/group actually holds
    inter = A_here & set(B)
    only_a = A_here - set(B)
    only_b = set(B) - A_here

    print(f"вікно {a.days} діб, група {a.group}: перехоплень {len(rows)}")
    print(f"A - за рядками переміщення зі звіту : {len(A_here)}")
    print(f"B - спіймано решетом                : {len(B)}")
    print(f"A ∩ B  згодні                        : {len(inter)}"
          + (f"  ({100*len(inter)//len(A_here)}% від A)" if A_here else ""))
    print(f"A \\ B  решето ПРОПУСТИЛО             : {len(only_a)}   <- читати й розширювати LEX")
    print(f"B \\ A  решето знайшло понад звіт     : {len(only_b)}")
    print("\nякі ознаки спрацьовують:", dict(Counter(k for v in B.values() for k in v).most_common()))

    if only_a and a.show:
        print(f"\n=== ПРОПУЩЕНІ решетом (перші {a.show}) — саме тут ховаються відсутні слова ===")
        for i in list(only_a)[:a.show]:
            print(f"\n  рядок звіту: {why.get(i,'')}")
            print("  мова:", " ".join(by_id[i]["text"].split())[:420])
    if only_b and a.show:
        print(f"\n=== ЗНАЙДЕНО решетом, у звіті рядка нема (перші {a.show}) ===")
        for i in list(only_b)[:a.show]:
            print(f"\n  ознаки: {', '.join(B[i])}")
            print("  мова:", " ".join(by_id[i]["text"].split())[:420])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(a.out) if a.out else OUT_DIR / f"MOVEMENT_SCREEN_{a.days}d.json"
    out.write_text(json.dumps({
        "window_days": a.days, "group": a.group, "built": datetime.now().isoformat(timespec="seconds"),
        "counts": {"corpus": len(rows), "A": len(A_here), "B": len(B),
                   "both": len(inter), "screen_missed": len(only_a), "screen_only": len(only_b)},
        "candidates": [{"msg_id": i, "signs": B[i], "in_report": i in A_here,
                        "text": by_id[i]["text"][:1200]} for i in B],
        "screen_missed": [{"msg_id": i, "report_line": why.get(i, ""),
                           "text": by_id[i]["text"][:1200]} for i in only_a],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{out}")


if __name__ == "__main__":
    main()
