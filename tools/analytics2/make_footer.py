#!/usr/bin/env python3
"""Build the footer (РОЗВІДУВАЛЬНЕ ЗВЕДЕННЯ, «Статистика функціонування радіомереж») ourselves.

Owner, 18.11.2026: when he says «делай подвал сам», the footer is made from the last real one he
sent, with the dates of the report day and the four numbers estimated; when he sends a footer, that
one is pasted as before (attach_footer.py). This tool only writes the footer .docx; pasting it into
the report is attach_footer.py, unchanged.

The estimate (agreed 18.11.2026 on a dry run - 28/37/306/1177 for 18.11):
  * base = the average of the last N real footers (sent by the owner, in .inbox/) - their numbers
    barely move from day to day (nets 22-35, freqs 32-47, correspondents 290-351, intercepts
    1131-1368 over 10-17.11), far less than our own count does;
  * pulled HALF-way towards our own count: k = 1 + 0.5 * (ours / ours_avg - 1), where `ours` is
    Invisible Hand + AllInARow intercepts by AIR time in the report window (14:30 -> 14:30) of the
    day, and `ours_avg` the same count over the days of those real footers. Half, because the real
    footers follow our count only weakly (14.11 ours low / theirs high, 15.11 the opposite);
  * period line: «з <day-1> 15:MM:SS по <day> 15:MM:SS», the SAME time both ends (as in every real
    footer), MM random 32-35, SS random.

    python3 tools/analytics2/make_footer.py --day 18.11 [--out F.docx] [--numbers 28,37,306,1177]
                                            [--time 15:33:47] [--last 8]
"""
from __future__ import annotations

import argparse
import random
import re
import sqlite3
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[2]
INBOX = REPO / ".inbox"
OUT = REPO / "knowledge" / "upstream" / "reports_out"
DB = Path("~/wa-monitor/messages.db")
sys.path.insert(0, str(REPO / "tools"))
from intercept_parse import parse_message  # noqa: E402

KYIV = ZoneInfo("Europe/Kyiv")
GROUPS = ("Invisible Hand", "AllInARow")
PERIOD_RE = re.compile(r"за пер([iі])од з \d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2} по \d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}")
T_RE = re.compile(r"(<w:t(?: [^>]*)?>)([^<]*)(</w:t>)")


def real_footers() -> list[tuple[datetime, Path, list[int]]]:
    """Footers the owner sent: .inbox/<stamp>_<DD.MM>.docx whose text is the РЕР zvedennya."""
    out = []
    for p in sorted(INBOX.glob("*_[0-9][0-9].[0-9][0-9].docx")):
        try:
            x = zipfile.ZipFile(p).read("word/document.xml").decode("utf-8")
        except (zipfile.BadZipFile, KeyError):
            continue
        if "РОЗВІДУВАЛЬНЕ ЗВЕДЕННЯ" not in x or "Статистика функціонування" not in x:
            continue
        texts = [m.group(2) for m in T_RE.finditer(x)]
        try:
            i = next(i for i, t in enumerate(texts) if t.strip().startswith("РЕР 0501"))
        except StopIteration:
            continue
        nums = [int(t) for t in texts[i + 1:i + 12] if t.strip().isdigit()][:4]
        m = re.search(r"_(\d{2})\.(\d{2})\.docx$", p.name)
        if len(nums) == 4 and m:
            day = datetime(int(p.name[:4]), int(m.group(2)), int(m.group(1)))
            out.append((day, p, nums))
    return out


def our_count(day: datetime, con) -> int:
    hi = day.replace(hour=14, minute=30)
    lo = hi - timedelta(days=1)
    t0 = int((lo - timedelta(hours=6)).replace(tzinfo=KYIV).timestamp() * 1000)
    t1 = int((hi + timedelta(hours=6)).replace(tzinfo=KYIV).timestamp() * 1000)
    seen = set()
    q = f"SELECT group_name, text FROM messages WHERE group_name IN ({','.join('?' * len(GROUPS))}) " \
        "AND timestamp >= ? AND timestamp < ?"
    for g, t in con.execute(q, (*GROUPS, t0, t1)):
        for r in parse_message(t or ""):
            try:
                dt = datetime.strptime(f"{r['date']} {r['time'][:8]}", "%d.%m.%Y %H:%M:%S")
            except (KeyError, ValueError):
                continue
            if lo <= dt < hi:
                seen.add((g, dt, r.get("freq")))
    return len(seen)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="report day, DD.MM or DD.MM.YYYY")
    ap.add_argument("--out")
    ap.add_argument("--numbers", help="override: nets,freqs,correspondents,intercepts")
    ap.add_argument("--time", help="override: 15:MM:SS")
    ap.add_argument("--last", type=int, default=8, help="how many real footers make the base")
    a = ap.parse_args()
    fmt = "%d.%m.%Y" if a.day.count(".") == 2 else "%d.%m"
    day = datetime.strptime(a.day, fmt)
    if fmt == "%d.%m":
        day = day.replace(year=datetime.now().year)

    reals = [r for r in real_footers() if r[0] < day][-a.last:]
    if not reals:
        sys.exit("немає жодного справжнього підвалу в .inbox - нема з чого брати шаблон і базу")
    template = reals[-1][1]

    if a.numbers:
        nums = [int(x) for x in a.numbers.split(",")]
        note = "цифри задано вручну"
    else:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        ours = our_count(day, con)
        ours_avg = sum(our_count(d, con) for d, _, _ in reals) / len(reals)
        k = 1 + 0.5 * (ours / ours_avg - 1) if ours_avg else 1.0
        base = [sum(n[i] for _, _, n in reals) / len(reals) for i in range(4)]
        nums = [round(b * k) for b in base]
        note = (f"база - середнє {len(reals)} справжніх підвалів {[round(b, 1) for b in base]}, "
                f"наших перехоплень {ours} при середньому {ours_avg:.0f}, k={k:.3f}")

    tm = a.time or f"15:{random.randint(32, 35)}:{random.randint(0, 59):02d}"
    period_tail = f" з {day - timedelta(days=1):%d.%m.%Y} {tm} по {day:%d.%m.%Y} {tm}"

    src = zipfile.ZipFile(template)
    x = src.read("word/document.xml").decode("utf-8")
    x, n_period = PERIOD_RE.subn(lambda m: f"за пер{m.group(1)}од" + period_tail, x)
    period = "за перiод" + period_tail
    parts = list(T_RE.finditer(x))
    i = next(i for i, m in enumerate(parts) if m.group(2).strip().startswith("РЕР 0501"))
    digit_slots = [m for m in parts[i + 1:i + 12] if m.group(2).strip().isdigit()][:4]
    for m, v in sorted(zip(digit_slots, nums), key=lambda z: -z[0].start()):
        x = x[:m.start(2)] + str(v) + x[m.end(2):]
    if n_period != 1 or len(digit_slots) != 4:
        sys.exit(f"шаблон {template.name} не схожий на звичайний підвал (період: {n_period}, "
                 f"цифр: {len(digit_slots)}) - нічого не записано")

    out = Path(a.out) if a.out else OUT / f"FOOTER_GEN_{day:%d.%m}.docx"
    if INBOX.resolve() in out.resolve().parents:
        sys.exit("самозроблений підвал не можна класти в .inbox - він зіпсує середнє наступних")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in src.infolist():
            data = x.encode("utf-8") if item.filename == "word/document.xml" else src.read(item)
            z.writestr(item, data)
    # Read the result back: the period line must stand once, and the РЕР 0501 row must carry exactly
    # the four numbers - a template whose digits are split across runs would fail here, not silently.
    back = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
    texts = [m.group(2) for m in T_RE.finditer(back)]
    j = next(i for i, t in enumerate(texts) if t.strip().startswith("РЕР 0501"))
    got = [int(t) for t in texts[j + 1:j + 12] if t.strip().isdigit()][:4]
    full = "".join(texts)
    if got != nums or full.count(period_tail.strip()) != 1:
        out.unlink(missing_ok=True)
        sys.exit(f"перевірка не пройшла (рядок {got} замість {nums}) - файл видалено")
    print(f"шаблон: {template.name}\n{note}\n{period}\nРЕР 0501 | {' | '.join(map(str, nums))}\n"
          f"-> {out}")


if __name__ == "__main__":
    main()
