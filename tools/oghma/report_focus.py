#!/usr/bin/env python3
"""The third pass, bounded by a list instead of by perfectionism.

Passes one and two are closed and line-level: the model writes a line, the audit checks that line
against the intercepts the line itself cites. Between them they catch a lot - roughly a quarter of
the lines of a finished report came back changed on 11 and 12.10.2026. What neither of them can
see is anything the report does not already say:

  * an event that is not in the report at all - there is no line to check
  * anything past the window cut, where the story usually continues
  * a side the speech alone does not settle, which needs the whole corpus, not one thread
  * a frequency that talked and produced nothing

So a third run of the same comparison is waste - it would mostly surface run-to-run variance, the
class we decided not to chase. A third pass of a DIFFERENT kind is not waste, and on 12.10.2026 it
paid for itself immediately: asked about one subject, reading the whole frequency thread turned up
a false prisoner of ours, an unnamed brigade, a radio-deception plan and a second capture on
another net - none of it reachable by either automated pass.

This tool does not read anything. It does two mechanical things so the reading stays finite:

  1. SELECTS the lines where being wrong is expensive, ranked, and stops at --top.
  2. GATHERS, for each, the whole frequency thread around it - including intercepts the report
     never used and intercepts past the window - plus the corpus history of every callsign named.

The categories are deliberately few and change only on the owner's word. A list that grows is the
production hell this exists to avoid.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "knowledge" / "upstream" / "reports_out"
CORPUS_DB = Path(os.environ.get("UPSTREAM_MESSAGES_DB", "~/wa-monitor/messages.db"))
KYIV = dt.timezone(dt.timedelta(hours=3))
CORPUS_TZ = dt.timezone(dt.timedelta(hours=2))

CALLSIGN_RE = re.compile(r"\b[А-ЯЁЇІЄ]{3,}(?:[ -][А-ЯЁЇІЄ0-9]{1,})?\b")
NOT_A_NAME = {"РОВ", "СОУ", "БПЛА", "БПЛА СОУ", "МТЗ", "ФПВ", "ВУ", "ТЗ", "ТМ", "УКХ", "ДРГ",
              "КНП", "АКБ", "БК", "ПВН", "МГРС", "MGRS", "СІМ", "ЗСУ", "НРТК", "ЛЕП"}

# (weight, name, pattern). Highest cost of being wrong first - that ordering IS the bound: when the
# list is cut at --top, what survives is what matters most, not what came first in the day.
CATEGORIES = [
    (100, "полон / страта / вбивство",
     r"полон|полонен|затриман|страт|розстріл|обнул|зарізат|знищити|добит|ліквідув"),
    (80, "сторона: кажемо, що людина наша",
     r"(?:\bв с|\bо с|\bгр|бійц\w*|полонен\w*|затриман\w*|\bтіло|загибл\w*)\s+(?:\w+\s+){0,2}?соу\b"),
    (60, "іменна втрата",
     r"\b[23]00\b"),
    (40, "назване НАШЕ формування або позиція",
     r"\b\d{1,3}\s*(?:бригад|омбр|обрмп|одшбр|оп|мбр)|бригад[аи]\b|\bсоу\b.{0,30}\b\d{2,3}\b"),
    (25, "хто водить людей по місцевості",
     r"веде|проводк|під проводкою|водить|маршрут по радіо"),
]


def corpus_rows(con, lo_ms: int, hi_ms: int, freq: str) -> list:
    rows = []
    for ts, g, t in con.execute(
            "SELECT timestamp, group_name, text FROM messages WHERE timestamp BETWEEN ? AND ?",
            (lo_ms, hi_ms)):
        if freq and freq not in (t or ""):
            continue
        rows.append((ts, g, t))
    return rows


def air_of(t: str) -> str:
    m = re.search(r"(\d{2}\.\d{2}\.\d{4}),\s*(\d{2}:\d{2}:\d{2})", t or "")
    return m.group(2) if m else "?"


def speech_of(t: str) -> list:
    return [x.strip() for x in (t or "").splitlines() if re.match(r"^\s*\d*\s*[–—-]", x)]


def callsign_history(con, names: set, skip_day: str) -> dict:
    """Where each named callsign is heard as a station, over the whole corpus. The side test."""
    if not names:
        return {}
    pats = {n: re.compile(r"(?<![А-ЯЁЇІЄҐа-яёїієґ])" + re.escape(n) + r"(?![А-ЯЁЇІЄҐа-яёїієґ])")
            for n in names}
    st_re = re.compile(r"^\s*\d?\s*[-–—]?\s*[А-ЯЁЇІЄҐ][А-ЯЁЇІЄҐ\s\"'0-9.()-]{1,34}$")
    same_day = re.compile(r"\b" + skip_day.replace(".", r"\.") + r"\.\d{4}")
    out = {n: {"n": 0, "first": "", "nets": set()} for n in names}
    for (t,) in con.execute("SELECT text FROM messages WHERE text IS NOT NULL"):
        if same_day.search(t):
            continue
        lines = [x.strip() for x in t.splitlines() if x.strip()]
        st = [x for x in lines if st_re.match(x)]
        if not st:
            continue
        blob = " ".join(st)
        for n, p in pats.items():
            if not p.search(blob):
                continue
            r = out[n]
            r["n"] += 1
            net = next((x[:50] for x in lines if x.startswith("УКХ")), "")
            if net:
                r["nets"].add(net)
            d = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", t)
            if d:
                k = d.group(3) + d.group(2) + d.group(1)
                if not r["first"] or k < r["first"][0]:
                    r["first"] = (k, d.group(0))
    return {n: r for n, r in out.items() if r["n"] >= 3}


def main() -> int:
    ap = argparse.ArgumentParser(description="select the few lines worth reading in full, and gather their whole thread")
    ap.add_argument("day", help="DD.MM of the report, e.g. 12.10")
    ap.add_argument("--top", type=int, default=8, help="hard cap on how many lines come back")
    ap.add_argument("--around", type=int, default=45, help="minutes of thread to pull either side")
    ap.add_argument("--full", action="store_true", help="dump the speech, not just the map")
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args()

    events = json.loads((OUT / f"ZVIT_{a.day}_events.json").read_text(encoding="utf-8"))
    src = json.loads((OUT / f"ZVIT_{a.day}_sources.json").read_text(encoding="utf-8"))

    picked = []
    for e in events:
        low = re.sub(r"[^а-яёіїєa-z0-9 ]", " ", (e.get("text") or "").casefold())
        score, why = 0, []
        for w, name, pat in CATEGORIES:
            if re.search(pat, low):
                score += w
                why.append(name)
        if score:
            picked.append((score, e, why))
    picked.sort(key=lambda x: -x[0])
    total = len(picked)
    picked = picked[:a.top]

    con = sqlite3.connect(f"file:{CORPUS_DB}?mode=ro", uri=True)
    names: set = set()
    for _, e, _ in picked:
        names.update(w.strip() for w in CALLSIGN_RE.findall(e.get("text") or "")
                     if w.strip() not in NOT_A_NAME and len(w.strip()) >= 4)
    hist = callsign_history(con, names, a.day)

    L = [f"# третій прохід ZVIT_{a.day} — {len(picked)} рядків з {total} відібраних",
         "",
         "Це НЕ помилки. Це рядки, де ціна помилки найбільша, і під кожним — ВСЯ гілка частоти,",
         "включно з перехопленнями, яких у звіті нема, і тими, що за межею вікна. Читати самому.",
         ""]
    if total > a.top:
        L += [f"> Відібрано {total}, показано {a.top}. Якщо впирається у стелю регулярно — "
              f"це сигнал звузити категорії, а не читати все.", ""]

    for score, e, why in picked:
        refs = e.get("_src_ref") or []
        L += ["", "=" * 78, f"## [{score}] {e.get('time')} — {e.get('text')}",
              f"- чому обрано: {', '.join(dict.fromkeys(why))}"]
        for n in sorted(set(CALLSIGN_RE.findall(e.get("text") or "")) & set(hist)):
            r = hist[n]
            L.append(f"- «{n}» — кореспондент у мережах РОВ {r['n']} раз з "
                     f"{r['first'][1] if r['first'] else '?'} ({sorted(r['nets'])[0][:46]})")
        if not refs:
            L.append("- перехоплень під рядком НЕМА — гілку не підняти")
            continue

        freq = refs[0].split()[-1]
        times = []
        for r in refs:
            m = re.match(r"(\d{2})\.(\d{2}) (\d{2}):(\d{2}):(\d{2})", r)
            if m:
                times.append(m.groups())
        if not times:
            continue
        d0, mo0, h0, mi0, _ = times[0]
        base = dt.datetime(2026, int(mo0), int(d0), int(h0), int(mi0), tzinfo=CORPUS_TZ)
        # The DB timestamp is when the group POSTED it; the air time lives in the text and runs
        # minutes earlier. Windowing on the timestamp dropped the very intercepts the line cites, so
        # the timestamp is only a coarse prefilter and the real window is applied to the AIR time.
        lo = int((base - dt.timedelta(minutes=a.around + 180)).timestamp() * 1000)
        hi = int((base + dt.timedelta(minutes=a.around + 180)).timestamp() * 1000)
        air_lo = (base - dt.timedelta(minutes=a.around)).strftime("%H:%M:%S")
        air_hi = (base + dt.timedelta(minutes=a.around)).strftime("%H:%M:%S")

        cited = {r.split()[1][:8] for r in refs}
        seen, thread = set(), []
        for ts, g, t in corpus_rows(con, lo, hi, freq):
            sp = speech_of(t)
            if not sp:
                continue
            air = air_of(t)
            if air == "?" or not (air_lo <= air <= air_hi):
                continue
            k = (air, " ".join(sp)[-120:])
            if k in seen:
                continue
            seen.add(k)
            thread.append((air, air in cited, sp))
        L.append(f"- гілка {freq}, ±{a.around} хв: {len(thread)} перехоплень "
                 f"({sum(1 for x in thread if x[1])} у звіті, {sum(1 for x in thread if not x[1])} НІ)")
        L.append("")
        # Default is a MAP, not the text. A busy assault net gives ~55 intercepts per line, and six
        # lines of full speech came to 142 KB on 12.10 - the same production hell in another shape.
        # The map stays finite; `--full` dumps the speech once a thread is worth it.
        for air, in_report, sp in sorted(thread):
            mark = "  " if in_report else "НЕ"
            if a.full:
                L.append(f"  {air} {'·' if in_report else 'НЕ В ЗВІТІ ·'}")
                for x in sp:
                    L.append("      " + x[:230])
            else:
                head = re.sub(r"^\s*\d*\s*[–—-]\s*", "", " / ".join(sp))
                L.append(f"  {mark} {air}  {head[:118]}")

    L += ["", "", "## чого цей прохід НЕ дає",
          "- він нічого не читає — відбір і збір механічні, висновок завжди мій",
          "- гілка обмежена однією частотою; сусідня частота тієї ж мережі сюди не потрапляє",
          "- мовчазні мережі (`_silent.txt`) він не чіпає зовсім"]

    text = "\n".join(L)
    print(text if len(text) < 4000 else text[:4000] + "\n… (повний текст у файлі)")
    if not a.no_save:
        p = OUT / f"FOCUS_{a.day}.md"
        p.write_text(text, encoding="utf-8")
        print(f"\nзбережено: {p.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
