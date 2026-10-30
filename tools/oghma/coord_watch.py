#!/usr/bin/env python3
"""coord_watch — every intercept where coordinates are dictated must have a line in the report.

The belt-and-braces for the most expensive class we have. A grid, a quadrant, a map reference read
out loud is the one thing in this traffic that cannot be re-derived later: a garbled callsign can be
matched against the register, a garbled number cannot be matched against anything.

**Why it exists.** On 15.10.2026 the owner picked one intercept at random and asked whether it was
in the report. It was not: КРАКЕН dictated «52 75 918, 72 77 414» for a drop, the operator marked it
❗❗❗, and the day's report had no line. Checking backwards, 13.10 had gone the same way, only worse —
МАЛОЙ dictated «47 39 59 19, 36 06 15 65» to ЧЕБУРАШКА and the report said «місце визначено за
координатами», which tells the reader that coordinates were passed and not WHICH, the exact failure
`rules.md` forbids in its own words.

Neither was a model failure. The rule in `rules.md` is scoped to coordinates of OUR positions (it
was written to warn our people), and both of those were coordinates of THEIR OWN position for a
supply drop — outside the rule as written. The three check passes could not catch it either: they
all work on lines that exist, and these lines did not exist.

**Why a frame and not digits.** Measured over the 7 days to 15.10.2026, 18 831 intercepts: a verbal
frame («записывай», «диктую», «координаты», «привязка») appears in 61, a coordinate-shaped triple of
numbers in 28, and the two TOGETHER in 11 — of which all 11 were real coordinate handovers, about
five or six unique events a week (each arrives twice, through two groups). Zero false positives. The
flood the owner feared comes from reading digits alone: this traffic is full of codes (200, 300,
412, 55), buttons, cubes, points and minutes («5 малых»). So the trigger here is the FRAME, and the
numbers are only what gets copied.

    python3 tools/upstream/coord_watch.py 15.10            # one report day
    python3 tools/upstream/coord_watch.py --days 7         # sweep back, no report needed
    python3 tools/upstream/coord_watch.py 15.10 --all      # covered ones too

It only reports. Exit code is 0 whether or not it finds anything — "found none" is a result, and a
coverage check must never be able to fail a job that actually ran.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "tools" / "analytics2"))
from intercept_parse import parse_message                      # noqa: E402
import run as report                                           # noqa: E402  — one door to the corpus

OUT = REPO / "knowledge" / "upstream" / "reports_out"

# The frame: somebody is about to read numbers off a map, or the operator says so in the comment.
# «Харитон»/«Ульяна» are the voice-procedure letters for the X and Y axes — in this traffic they are
# a coordinate marker all by themselves.
FRAME = re.compile(r"запис(?:ыва|уй|ать|ати|ыв)|диктую|координат|прив['’я]?язк|харитон|ульян", re.I)
# Shapes actually seen in this corpus, all three of them:
#   local grid, two numbers   «52 75 918» + «72 77 414»
#   grid with axis letters    «Харитон 527 31 55, Ульяна 671 30 53»
#   degrees/minutes/seconds   «47 39 59 19, 36 06 15 65»
# Three groups minimum, up to five: «47 39 59 19» is one reading and must not come out as «47 39 59»
# — a truncated number copied into a line is worse than none, it looks precise and is not.
NUM_TRIPLE = re.compile(r"\b\d{2,3}(?:[ \-]\d{1,3}){2,4}\b")
NUM_PAIR = re.compile(r"\b\d{4,6}\b[ ,]+\b\d{4,6}\b")
NUM_MGRS = re.compile(r"\b\d{2}\s?[A-ZА-Я]{1,3}\s?\d{4,6}\s?\d{4,6}\b")
# Numbers that are NOT coordinates and must never raise a candidate on their own. Everything here
# is an everyday value in this traffic; the frame is what separates a map reading from a code.
NOT_COORD = re.compile(r"\b(?:200|300|400|450|202|303|412|415|55|44|54|33|22|11)\b")
# A Russian mobile number dictated on air («продиктую… 8 914 017 47 7») is not a map reading; kept in
# step with tools/alerts/safety_net.py, where it raised a false «координати» on 21.10.2026.
PHONE = re.compile(r"(?<!\d)(?:\+7|8)[ \-]?\(?9\d{2}\)?(?:[ \-]?\d){5,8}(?!\d)")


def digit_groups(text: str) -> list[str]:
    out = []
    text = PHONE.sub(" ", text or "")
    for rx in (NUM_MGRS, NUM_TRIPLE, NUM_PAIR):
        for m in rx.finditer(text):
            s = m.group(0).strip()
            if s not in out:
                out.append(s)
    return out


SIG_LEN = 7


def signatures(spans: list[str]) -> set[str]:
    """Digits only, in windows — so «52 99 789» in the air matches «52 99 789,» in a line.

    Comparing the matched SPANS as strings does not work: the greedy match merges two readings
    («52 99 789 72 97») and a repeated number picks up a stray digit, so a correct line would be
    reported as having no digits. Spacing and punctuation differ between the air and the line by
    nature; the digits do not.
    """
    out = set()
    for s in spans:
        d = re.sub(r"\D", "", s)
        for i in range(0, max(1, len(d) - SIG_LEN + 1)):
            w = d[i: i + SIG_LEN]
            if len(w) == SIG_LEN:
                out.add(w)
    return out


def candidates(lo: datetime, hi: datetime) -> list[dict]:
    """Intercepts in the window where coordinates are being read out."""
    db = report.CORPUS_DB
    if not db.exists():
        sys.exit(f"немає бази перехоплень: {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT group_name, coalesce(text,'') FROM messages "
            "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
            (int(lo.timestamp() * 1000), int(hi.timestamp() * 1000))).fetchall()
    finally:
        con.close()

    found: dict[str, dict] = {}
    for group, body in rows:
        for rec in parse_message(body):
            speech = " ".join(rec.get("speech") or [])
            marks = " ".join([rec.get("comment_above") or ""] + (rec.get("marks") or []))
            nums = digit_groups(speech)
            if not nums:
                continue
            # two independent triggers, and neither of them is "there are digits"
            by_speech = bool(FRAME.search(speech))
            by_mark = "координат" in marks.lower()
            if not (by_speech or by_mark):
                continue
            ref = report.ref_of(rec)
            prev = found.get(ref)
            if prev is None:
                found[ref] = {"ref": ref, "nums": nums, "mark": marks.strip()[:160],
                              "net": rec.get("net") or "", "groups": [group],
                              "why": "мова" if by_speech else "мітка",
                              "speech": [s for s in (rec.get("speech") or []) if FRAME.search(s)
                                         or digit_groups(s)][:4]}
            elif group not in prev["groups"]:
                prev["groups"].append(group)
    return list(found.values())


def main() -> int:
    ap = argparse.ArgumentParser(description="coordinates dictated in the air vs lines in the report")
    ap.add_argument("day", nargs="?", help="DD.MM or YYYY-MM-DD — the report day")
    ap.add_argument("--days", type=int, default=0, help="sweep the last N days instead")
    ap.add_argument("--events", help="events file (default: the day's ZVIT_<day>_events.json)")
    ap.add_argument("--all", action="store_true", help="list covered candidates too")
    a = ap.parse_args()

    events, tag = [], ""
    if a.days:
        hi = datetime.now()
        lo = hi - timedelta(days=a.days)
        tag = f"останні {a.days} діб"
    else:
        if not a.day:
            sys.exit("треба день (DD.MM) або --days N")
        d = None
        for fmt in ("%d.%m", "%Y-%m-%d", "%d.%m.%Y"):
            try:
                d = datetime.strptime(a.day, fmt)
                break
            except ValueError:
                continue
        if d is None:
            sys.exit(f"не розібрав день: {a.day!r}")
        if d.year == 1900:
            d = d.replace(year=datetime.now().year)
        cut_h, cut_m = (int(x) for x in report.WINDOW_CUT.split(":"))
        hi = d.replace(hour=cut_h, minute=cut_m, second=0, microsecond=0)
        lo = hi - timedelta(days=1)
        tag = f"вікно звіту {lo:%d.%m %H:%M} - {hi:%d.%m %H:%M}"
        ev_path = Path(a.events) if a.events else OUT / f"ZVIT_{d:%d.%m}_events.json"
        if ev_path.exists():
            events = json.loads(ev_path.read_text(encoding="utf-8"))
        else:
            print(f"подій немає ({ev_path.name}) — перевіряю лише наявність координат у мові",
                  file=sys.stderr)

    used_refs = {r for e in events for r in (e.get("_src_ref") or [])}
    all_digits = re.sub(r"\D", "", " ".join(e.get("text") or "" for e in events))

    cands = candidates(lo, hi)
    bad, naked, ok = [], [], []
    for c in cands:
        # Three states, not two. The numbers count as present if they stand in ANY line, because the
        # same transmission arrives through two groups and a line may rest on the other copy.
        in_ref = c["ref"] in used_refs
        in_text = any(sig in all_digits for sig in signatures(c["nums"]))
        if in_text:
            ok.append(c)
        elif in_ref:
            # The 13.10.2026 shape, and the reason this third state exists: the line was there and
            # said «місце визначено за координатами» — telling the reader that coordinates were
            # passed and not WHICH. A line without the digits is what `rules.md` forbids outright,
            # so it is a finding, not coverage.
            naked.append(c)
        else:
            bad.append(c)

    print(f"координати в ефірі — {tag}")
    if not events:
        # No events to compare against (a --days sweep, or the day was never built): say so, instead
        # of printing every candidate as "БЕЗ рядка" and looking like a pile of findings.
        print(f"кандидатів: {len(cands)}   (покриття не перевіряється - немає файла подій)")
        for c in cands:
            print(f"  ·  {c['ref']}  числа: {', '.join(c['nums'])}"
                  + (f"   мітка: {c['mark'][:70]}" if c["mark"] else ""))
        if not cands:
            print("(координат у цьому вікні не диктували - нормальний результат)")
        return 0
    print(f"кандидатів: {len(cands)}   з цифрами у звіті: {len(ok)}   "
          f"рядок без цифр: {len(naked)}   БЕЗ рядка: {len(bad)}")
    for c in naked:
        print()
        print(f"  ~~ {c['ref']}  РЯДОК Є, ЦИФР НЕМА  ({c['why']}, {'+'.join(c['groups'])})")
        if c["mark"]:
            print(f"     мітка: {c['mark']}")
        print(f"     числа з мови: {', '.join(c['nums'])}")
        for s in c["speech"]:
            print(f"     {s.strip()[:150]}")
    for c in bad:
        print()
        print(f"  !! {c['ref']}  ({c['why']}, {'+'.join(c['groups'])})")
        if c["mark"]:
            print(f"     мітка: {c['mark']}")
        print(f"     числа: {', '.join(c['nums'])}")
        for s in c["speech"]:
            print(f"     {s.strip()[:150]}")
    if a.all and ok:
        print("\n--- вже у звіті:")
        for c in ok:
            print(f"  ok {c['ref']}  числа: {', '.join(c['nums'])}")
    if not cands:
        print("(координат у цьому вікні не диктували — нормальний результат)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
