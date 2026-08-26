#!/usr/bin/env python3
"""Everything the corpus holds about ONE frequency — raw extraction, no analysis.

    python3 tools/analytics2/freq_dossier.py --freq 454.0700 [--tol-khz 5] [--out FILE]

Answers the questions that get asked about a newly-noticed channel: when it first appeared, who is
on it, who guides and who is guided, which points and shelters are named, where the casualties are,
and what the last traffic was. It only pulls and counts — every judgement is left to the reader.

Written after doing the same thing twice by hand. The parts that are easy to get wrong when
improvising, and are therefore fixed here:

  * the frequency is matched with a tolerance, because two readings of one channel differ by a
    couple of kHz;
  * callsigns are read from the intercept HEADER, the one place they are marked as callsigns rather
    than guessed — a case-insensitive scan of the speech returns «ТАМ» and «СЕЙЧАС» as the busiest
    callsigns of the day;
  * names inside the speech are matched Title-case, not upper-case: the transcript is written in
    ordinary sentence case, and an upper-case scan of 100 KB of it found exactly one token;
  * `я <Имя>` is counted separately — it is what identifies the guide, who is otherwise
    indistinguishable from the people he leads;
  * MGRS grids typed by the operator into the STATION field are pulled out; they are the only real
    map tie in networks whose speech never names a settlement.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "tools"))
from intercept_parse import parse_message  # noqa: E402

REPORTS_DB = REPO / "knowledge" / "upstream" / "reports.db"
GRID = re.compile(r"\b\d{2}[A-Z]\s+[A-Z]{2}\s+\d{4,5}\s+\d{4,5}")
HURT = re.compile(r"\b300\b|\b200\b|ранен|осколоч|конту|подорвал|эвакуац|груз ?200", re.I)
PLACE = re.compile(r"\bт\s?\d|\bор\b|укрыт|укритт|лесопол|лісосм|посадк|позици|позиці|"
                   r"блиндаж|окоп|транше|подвал|вышк|лэп|мост|дорог", re.I)
COMMON = set("""Да Нет Все Так Ну Понял Принял Приём Прием Как Что Где Давай Сейчас Там Здесь Тут
Ты Я Мы Он Она Это Вот Хорошо Ага Слышу Стоп Есть Ок Ждать Ждем Жди Начинай Иди Идти Блядь Нахуй
Сука Ебать Пиздец Короче Просто Ладно Значит Пока Потом Уже Еще Ещё Один Два Три Четыре Пять Север
Юг Запад Восток Правее Левее Вперед Назад Хуй Пизда Быстрее Тихо Медленно Спасибо Братан Брат
Командир Чуть Бля Стой Повтори Куда Прямо Молодец Рядом Никак Одна Будь Какой Туда Меня Ведь
Двигайся Налево Направо Тридцать Хорошо""".split())


def fetch(freq: float, tol_khz: float) -> list[dict]:
    # Prefilter on the INTEGER part only. Two decimals looked tighter and was wrong: `%.2f` ROUNDS,
    # so 446.0687 became the pattern `446.07`, which the string `446.0687` does not contain — the
    # channel came back empty while the pipeline saw 58 intercepts on it. The real selection is the
    # tolerance check below; this LIKE only exists to keep the query cheap.
    like = f"{int(freq)}."
    sql = ("SELECT id || E'\\x01' || replace(coalesce(text,''), E'\\n', E'\\x02') "
           "FROM source_messages WHERE group_name='PATAGONIA_GP' "
           f"AND text LIKE '%{like}%' ORDER BY occurred_ts, id")
    raw = subprocess.run(["docker", "exec", "upstream_db", "psql", "-U", "upstream", "-d", "upstream",
                          "-At", "-c", sql], capture_output=True, text=True, timeout=300).stdout
    out = []
    for line in raw.split("\n"):
        if "\x01" not in line:
            continue
        mid, body = line.split("\x01", 1)
        for r in parse_message(body.replace("\x02", "\n")):
            try:
                if not r.get("freq") or abs(float(r["freq"]) - freq) * 1000 > tol_khz:
                    continue
            except ValueError:
                continue
            try:
                fmt = "%d.%m.%Y %H:%M:%S" if r["time"].count(":") == 2 else "%d.%m.%Y %H:%M"
                r["_dt"] = datetime.strptime(f"{r['date']} {r['time']}", fmt)
            except (KeyError, ValueError):
                continue
            r["msg_id"] = mid
            out.append(r)
    out.sort(key=lambda x: x["_dt"])
    return out


def archive(freq: float, tol_khz: float, w) -> None:
    """What the analyst's own finished reports already say about this channel."""
    if not REPORTS_DB.exists():
        return
    db = sqlite3.connect(f"file:{REPORTS_DB}?mode=ro", uri=True)
    ids = []
    for nid, fr, header, unit, area in db.execute(
            "SELECT id, freqs, header, unit, area FROM networks"):
        try:
            fs = json.loads(fr) if fr and fr.strip().startswith("[") else []
        except json.JSONDecodeError:
            fs = []
        for f in fs:
            try:
                if abs(float(f) - freq) * 1000 <= tol_khz:
                    ids.append(nid)
                    w(f"  {header}   | підрозділ: {unit} | район: {area}")
                    break
            except ValueError:
                pass
    if not ids:
        w("  (в архіві аналітика цієї частоти немає)")
        return
    m = ",".join("?" * len(ids))
    w("\n  РЕЄСТР:")
    for c, r in db.execute(f"SELECT DISTINCT callsign, role FROM roster WHERE network_id IN ({m})",
                           ids):
        w(f"    {c} - {r}")
    w("\n  ЛЕГЕНДА:")
    for c, mm in db.execute(f"SELECT DISTINCT code, meaning FROM legend WHERE network_id IN ({m})",
                            ids):
        w(f"    «{c}» - {mm}")
    w("\n  ГОТОВІ РЯДКИ ЗВІТІВ:")
    for d, t, x in db.execute(
            f"SELECT date, time, text FROM events WHERE network_id IN ({m}) ORDER BY ts", ids):
        w(f"    {d} {t or ''} {x}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", required=True, type=float)
    ap.add_argument("--tol-khz", type=float, default=5.0)
    ap.add_argument("--out")
    a = ap.parse_args()

    lines: list[str] = []

    def w(s: str = "") -> None:
        lines.append(s)

    recs = fetch(a.freq, a.tol_khz)
    if not recs:
        print(f"по частоті {a.freq} перехоплень не знайдено")
        return
    speech = [(r["_dt"], s) for r in recs for s in r.get("speech", [])]

    w(f"=== ЧАСТОТА {a.freq:.4f} — {len(recs)} перехоплень")
    w(f"ПЕРШИЙ:    {recs[0]['_dt']}  msg {recs[0]['msg_id']}")
    w(f"ОСТАННІЙ:  {recs[-1]['_dt']}  msg {recs[-1]['msg_id']}")
    w(f"ЗНАЧЕННЯ ЧАСТОТИ: {Counter(r['freq'] for r in recs).most_common()}")
    w(f"ОБСЯГ МОВЛЕННЯ: {sum(len(s) for _, s in speech)} симв.")

    w("\n=== ПО ДНЯХ")
    for d, n in sorted(Counter(r["_dt"].date().isoformat() for r in recs).items()):
        w(f"  {d}: {n}")

    w("\n=== ШАПКИ")
    for h, n in Counter(r.get("network") or "(немає)" for r in recs).most_common():
        w(f"  {n:4}  {h}")

    st, pairs = Counter(), Counter()
    for r in recs:
        names = [s.strip().upper() for s in r.get("stations", []) if s and s.strip()]
        for x in names:
            st[x] += 1
        if len(names) > 1:
            pairs[" / ".join(sorted(names))] += 1
    w("\n=== СТАНЦІЇ В ШАПКАХ")
    for k, v in st.most_common():
        w(f"  {v:4}  {k}")
    w("\n=== ПАРИ В ЕФІРІ")
    for k, v in pairs.most_common(20):
        w(f"  {v:4}  {k}")

    w("\n=== ХТО ВЕДЕ (представляється «я X»)")
    lead = Counter()
    for _, s in speech:
        for m in re.finditer(r"\bя\s+([А-ЯЁ][а-яё\-]{2,})", s):
            lead[m.group(1)] += 1
    for k, v in lead.most_common(20):
        w(f"  {v:4}  {k}")

    w("\n=== ІМЕНА В МОВЛЕННІ (Title-case, >=3)")
    tok = Counter()
    for _, s in speech:
        for m in re.findall(r"\b([А-ЯЁЇІЄҐ][а-яёїієґ\-]{2,})\b", s):
            if m not in COMMON:
                tok[m] += 1
    for k, v in tok.most_common(60):
        if v >= 3:
            w(f"  {v:4}  {k}")

    w("\n=== КООРДИНАТИ MGRS")
    for r in recs:
        blob = " | ".join(r.get("stations", [])) + " || " + " ".join(r.get("speech", []))
        for m in GRID.finditer(blob):
            w(f"  [{r['_dt']:%d.%m %H:%M}] msg {r['msg_id']}  {m.group(0)}   "
              f"станції: {r.get('stations')}")

    w("\n=== ВТРАТИ / ПОРАНЕННЯ")
    for dt, s in speech:
        if HURT.search(s):
            w(f"  [{dt:%d.%m %H:%M}] {' '.join(s.split())[:230]}")

    w("\n=== ТОЧКИ, УКРИТТЯ, ОРІЄНТИРИ")
    seen = set()
    for dt, s in speech:
        if PLACE.search(s) and s not in seen:
            seen.add(s)
            w(f"  [{dt:%d.%m %H:%M}] {' '.join(s.split())[:230]}")

    w("\n=== ПОЗНАЧКИ ОПЕРАТОРА")
    cm = Counter()
    for r in recs:
        for t in ("comment_above", "comment_below"):
            if r.get(t):
                cm[" ".join(str(r[t]).split())] += 1
    for k, v in cm.most_common(60):
        w(f"  {v:3}  {k}")

    w("\n=== ПЕРШІ 3 ПЕРЕХОПЛЕННЯ")
    w("\n=== ОСТАННІ 6 ПЕРЕХОПЛЕНЬ")
    for tag, chunk in (("ПЕРШІ", recs[:3]), ("ОСТАННІ", recs[-6:])):
        w(f"\n--- {tag}")
        for r in chunk:
            w(f"[{r['_dt']:%d.%m %H:%M}] {' / '.join(r['stations']) or 'НВ'}  msg {r['msg_id']}")
            for t in ("comment_above", "comment_below"):
                if r.get(t):
                    w(f"   ({' '.join(str(r[t]).split())})")
            for s in r.get("speech", [])[:4]:
                w(f"   {' '.join(s.split())[:200]}")

    w("\n=== З АРХІВУ АНАЛІТИКА")
    archive(a.freq, a.tol_khz, w)

    text = "\n".join(lines)
    if a.out:
        Path(a.out).write_text(text)
        print(f"{a.out}  ({len(lines)} рядків)")
    else:
        print(text)


if __name__ == "__main__":
    main()
