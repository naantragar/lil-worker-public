#!/usr/bin/env python3
"""Backfill a WhatsApp chat export into the collector's corpus.

    python3 tools/corpus/import_chat_export.py <export.md> --group AllInARow          # dry run
    python3 tools/corpus/import_chat_export.py <export.md> --group AllInARow --write

Why this exists. The collector only sees a group from the moment it is added; everything the group
said BEFORE that is lost to it. When a group turns out to matter we get its history as a WhatsApp
export instead, and that history has to end up in the SAME table as everything else — otherwise
every search, report and callsign index silently answers from half the evidence. (Exactly how
«Гризли» hid: absent from every signed net in the corpus, present in an unsigned group the
collector had not been reading.)

**The record separator is the bracketed posting time at the start of a line** — `[2:53 AM]` — and
nothing else. What follows it is not stable: usually the air-time line, but a record may open with
the analyst's `Коментар:` instead. A parser that keys on the date line loses every commented
intercept; a parser that keys on `Коментар:` loses every plain one.

Two clocks, and they are not the same:
  `[1:10 PM]`             the posting time in the exporting phone's timezone — no date at all
  `30.08.2026, 13:09:47`  the AIR time, from the intercept's own header

The bracket has no date, so it cannot anchor a record on its own; the air time can, and it is the
one that means anything analytically. `timestamp` is therefore written from the AIR time. Measured
against live collector rows the two differ by about a minute (the collector posts right after the
exchange), so ordering and day-window filters stay honest either way.

Identity: `IMP-<sha1(text)[:20]>`, derived from the text itself, so importing the same export twice
inserts nothing the second time. Real collector ids are WhatsApp's own (`3EB0…`) and cannot collide.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

DB = Path("~/wa-monitor/messages.db")

REC_RE = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?\s*[AaPp]\.?[Mm]\.?)\]\s?", re.M)
AIR_RE = re.compile(r"^\s*(\d{2}\.\d{2}\.\d{4}),\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*$", re.M)
FREQ_RE = re.compile(r"^\s*(\d{2,4}[.,]\d{3,4})\s*$", re.M)
NET_RE = re.compile(r"^\s*((?:УКХ|КХ)\b.*)$", re.M)


def records(text: str) -> list[tuple[str, str]]:
    """(bracket time, record body) in file order. Everything before the first bracket is the
    export's own header and is dropped."""
    marks = list(REC_RE.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip("\n").rstrip()
        if body.strip():
            out.append((m.group(1), body))
    return out


def air_time(body: str) -> datetime | None:
    m = AIR_RE.search(body)
    if not m:
        return None
    d, t = m.group(1), m.group(2)
    fmt = "%d.%m.%Y %H:%M:%S" if t.count(":") == 2 else "%d.%m.%Y %H:%M"
    try:
        return datetime.strptime(f"{d} {t}", fmt)
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("export")
    ap.add_argument("--group", required=True, help="group_name as the collector knows it")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--write", action="store_true", help="actually insert (default: dry run)")
    ap.add_argument("--show", type=int, default=6, help="how many oddities to print in full")
    a = ap.parse_args()

    raw = Path(a.export).read_text(encoding="utf-8", errors="replace")
    recs = records(raw)
    if not recs:
        sys.exit("жодного запису не знайдено — у файлі немає рядків, що починаються з [H:MM AM]")

    db = Path(a.db)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    g = con.execute("SELECT jid, name FROM groups WHERE name = ?", (a.group,)).fetchone()
    if not g:
        sys.exit(f"групи «{a.group}» немає в таблиці groups — колектор про неї не знає")
    jid, gname = g
    have = {r[0] for r in con.execute("SELECT id FROM messages WHERE group_name = ?", (gname,))}
    con.close()

    rows, odd = [], Counter()
    samples: dict[str, list] = {}

    def note(kind: str, br: str, body: str) -> None:
        odd[kind] += 1
        samples.setdefault(kind, []).append((br, body))

    for br, body in recs:
        when = air_time(body)
        airs = len(AIR_RE.findall(body))
        if when is None:
            note("без часу ефіру - пропущено", br, body)
            continue
        if airs > 1:
            note(f"ДВА В ОДНОМУ ({airs} часів ефіру)", br, body)
        if not FREQ_RE.search(body):
            note("без частоти", br, body)
        if not NET_RE.search(body):
            note("без шапки мережі", br, body)
        mid = "IMP-" + hashlib.sha1(body.encode("utf-8")).hexdigest()[:20].upper()
        rows.append((mid, when, body))

    uniq = {}
    dup_in_file = 0
    for mid, when, body in rows:
        if mid in uniq:
            dup_in_file += 1
            continue
        uniq[mid] = (when, body)
    fresh = {k: v for k, v in uniq.items() if k not in have}

    days = Counter(v[0].date() for v in uniq.values())
    print(f"файл: {a.export}")
    print(f"записів за роздільником [H:MM AM]: {len(recs)}")
    print(f"розібрано з часом ефіру:           {len(rows)}")
    print(f"унікальних (дублі в файлі: {dup_in_file}):      {len(uniq)}")
    print(f"вже є в базі:                      {len(uniq) - len(fresh)}")
    print(f"ДО ВСТАВКИ:                        {len(fresh)}")
    if uniq:
        ds = sorted(days)
        print(f"період: {ds[0].strftime('%d.%m.%Y')} - {ds[-1].strftime('%d.%m.%Y')}, "
              f"днів з ефіром: {len(ds)}, у середньому {len(uniq) / len(ds):.1f} за день")
    print(f"група: {gname} ({jid}), зараз у базі: {len(have)}")

    if odd:
        print("\n--- на що подивитись оком ---")
        for k, c in odd.most_common():
            print(f"  {k}: {c}")
        for k in odd:
            for br, body in samples[k][:a.show]:
                print(f"\n  [{k}] [{br}]")
                for line in body.splitlines()[:14]:
                    print("     " + line[:150])

    if not a.write:
        print("\nСУХИЙ ПРОГІН. Нічого не записано. Додай --write.")
        return 0
    if not fresh:
        print("\nНових записів немає - нічого робити.")
        return 0

    backup = db.with_name(db.name + f".bak-import-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    # The collector is writing to this file right now. Copy first, insert in one short
    # transaction, and never touch a row that is not ours.
    shutil.copy2(db, backup)
    con = sqlite3.connect(str(db), timeout=30)
    try:
        con.execute("PRAGMA busy_timeout = 30000")
        con.executemany(
            "INSERT OR IGNORE INTO messages "
            "(id, group_jid, group_name, sender_jid, sender_name, text, msg_type, "
            " is_from_me, is_forwarded, forwarding_score, is_reply, multicast, timestamp, "
            " category, raw_fields) "
            "VALUES (?,?,?,?,?,?,'text',0,0,0,0,0,?,'import','[\"chatExportBackfill\"]')",
            [(mid, jid, gname, "import@backfill", "chat export",
              body, int(when.timestamp() * 1000)) for mid, (when, body) in fresh.items()])
        con.commit()
        now = con.execute("SELECT count(*) FROM messages WHERE group_name = ?", (gname,)).fetchone()[0]
    finally:
        con.close()
    print(f"\nЗаписано. У групі «{gname}» тепер {now} повідомлень.")
    print(f"Резервна копія бази: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
