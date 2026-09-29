#!/usr/bin/env python3
"""Stage A of the unsigned-net pipeline: the SIEVE. No model, no verdicts.

    python3 tools/identity/code_sieve.py --calibrate          # what each code is worth
    python3 tools/identity/code_sieve.py --group AllInARow    # sift that group
    python3 tools/identity/code_sieve.py --group AllInARow --day 2026-09-26

What it is for. An unsigned net is traffic we hold without knowing whose it is. Sometimes the
speech itself gives it away: a code word, a route name or a signal number that, in our own signed
traffic, only one formation ever uses. This narrows a day of unsigned traffic down to the few
intercepts worth a human's attention. **It attributes nothing** - it hands over a candidate and the
evidence behind it, and the reading is somebody else's job.

Three deliberate decisions, all of them the owner's (27.09.2026):

1. **A code is not proof, and most codes are not even a hint.** «Коробка» can be a vehicle or an
   actual box; «55» can be «прийняв» or half of a frequency. So every code is CALIBRATED against
   the signed corpus first: how often it occurs on its own unit's nets against everyone else's. A
   code heard everywhere is noise and is scored as noise. The calibration is measured, not assumed -
   the code tables themselves already disagree («11» is «рух дозволено» for 305 абр, «обстановка
   стабільна» for 1 шр «V» 38 омсбр and «небо чисте» for 1198 мсп).

2. **Declension is handled by stem, not by a model.** Russian and Ukrainian noun endings are
   regular enough that «малина / малину / малине» share `малин`. Matching trims up to three
   trailing letters. This over-matches on purpose: the sieve's job is to lose nothing, the later
   stages throw things out.

3. **Numbers are matched with their transcription damage in mind.** «55» may be written `55`,
   `5 5`, or swallowed inside a longer number. The digit matcher accepts the spaced form and
   refuses a match that sits inside a longer run of digits - the second rule is what keeps `55`
   out of `433.5500`.

Codes are read ONLY from the SPEECH. The comment line is the analyst's own Ukrainian and the net
header is ours; matching in either would be matching our own vocabulary against itself.
"""
from __future__ import annotations

import argparse
import collections
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GLOSS = REPO / "knowledge" / "upstream" / "glossary" / "glossary.db"
CORPUS = Path("~/wa-monitor/messages.db")

CODE_KINDS = ("settlement_code", "route_code", "code_word", "signal_code",
              "numeric_code", "signal_word")
UNIT_RE = re.compile(r"\d{1,4}\s*(?:омсбр|мсп|мсбр|отбр|абр|мсд|одшбр|опдп)", re.I)
FREQ_LINE = re.compile(r"^\s*(\d{2,4}[.,]\d{2,4})\s*$")
WORD = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґA-Za-z]{3,}")
DIGITS = re.compile(r"\d+")


# ── what a formation is, for the purpose of comparing ────────────────────────────────────────────
def formation(text: str) -> str:
    """«2 мср 1 мсб 38 омсбр» -> «38 омсбр». Codes are issued at formation level; comparing a
    company against a brigade would call every match a miss."""
    m = UNIT_RE.findall(text or "")
    return re.sub(r"\s+", " ", m[-1].lower()) if m else ""


def speech(text: str) -> str:
    """Only what was SAID. Everything above the callsign line belongs to us, not to them."""
    lines = [l.rstrip() for l in (text or "").splitlines()]
    out, started = [], False
    for i, l in enumerate(lines):
        if not started:
            if re.match(r"^\s*(\d+\s*)?[–—-]\s", l):
                started = True
            else:
                continue
        out.append(l)
    return "\n".join(out)


def callsigns_of(lines: list[str], i: int) -> list[str]:
    """The callsigns of an intercept whose frequency stands at line `i`.

    There are TWO callsign lines, not one — the layout is

        <частота> / <мережа> / <перший бік> / <другий бік> / (порожньо) / мова

    and `НВ` in either slot means that side was not identified. We read only the second line for
    the first day and a half of this work, which silently dropped a QUARTER of every callsign in
    the corpus: measured on 4813 intercepts, the first slot holds a real name 1265 times. ТАЙФУН,
    who leads БЛУДНОГО across two days on 402.8348, sat in exactly that slot and was invisible.
    """
    out = []
    for k in (2, 3):
        if i + k >= len(lines):
            break
        for x in re.split(r"[,/]", lines[i + k]):
            x = re.sub(r"\s+", " ", x).strip().upper()
            if 2 <= len(x) <= 20 and x != "НВ" and re.match(r"^[А-ЯЁЇІЄҐA-Z0-9 '\-\.]+$", x):
                out.append(x)
    return out


def stem(w: str) -> str:
    return w[:-3] if len(w) > 7 else w[:-2] if len(w) > 5 else w[:-1] if len(w) > 3 else w


class Codes:
    """The code tables, turned into something that can be run over a million tokens."""

    def __init__(self, db: Path = GLOSS):
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        self.rows = [dict(r) for r in con.execute(
            f"SELECT term, kind, meaning, unit, source FROM entries "
            f"WHERE kind IN {CODE_KINDS} AND term IS NOT NULL AND term != ''")]
        con.close()
        self.by_stem: dict[str, list[int]] = collections.defaultdict(list)
        self.by_num: dict[str, list[int]] = collections.defaultdict(list)
        self.phrases: list[tuple[re.Pattern, int]] = []
        for i, r in enumerate(self.rows):
            r["form"] = formation(r["unit"] or "")
            t = str(r["term"]).strip()
            if re.fullmatch(r"\d+", t):
                self.by_num[t].append(i)
                continue
            words = WORD.findall(t)
            if len(words) > 1:
                pat = r"[\s,]+".join(re.escape(w[:max(3, len(w) - 2)]) + r"[а-яіїєґ]{0,3}"
                                     for w in words)
                self.phrases.append((re.compile(pat, re.I), i))
            elif words:
                self.by_stem[stem(words[0].lower())].append(i)

    def find(self, text: str) -> set[int]:
        """Indices of every code that this speech appears to contain."""
        hits: set[int] = set()
        toks = [w.lower() for w in WORD.findall(text)]
        for w in toks:
            for k in range(0, 4):
                s = w[:len(w) - k] if k else w
                if len(s) < 3:
                    break
                if s in self.by_stem:
                    hits.update(self.by_stem[s])
        # numbers: the digit run itself, plus digits split by spaces («5 5» for 55)
        runs = DIGITS.findall(re.sub(r"[.,](?=\d)", "#", text))   # 433#0700 stays one run
        joined = "".join(runs)
        for n in self.by_num:
            if n in runs:
                hits.update(self.by_num[n])
            elif len(n) > 1 and re.search(r"(?<![\d#])" + r"\s*".join(n) + r"(?![\d#])", text):
                hits.update(self.by_num[n])
        for pat, i in self.phrases:
            if pat.search(text):
                hits.add(i)
        return hits


# The owner's personal delivery channel to me. He forwards files and messages there because it is
# faster than reaching the server; it is NOT air traffic our collector heard. Excluded here, in the
# one door every analysis tool reads the corpus through, so no counting, no report and no index can
# ever pick it up - including rows the collector writes a second from now, before the drain runs.
# His rule, 29.09.2026: «никак и никогда не должно быть инструментом прямого воздействия на корпус,
# отчёты, базу данных. Мы решаем.» Physical half of the same fence: tools/corpus/transfer_drain.py.
TRANSFER_JID = "120363408965106490@g.us"


def corpus(group: str | None = None) -> list[tuple]:
    """(timestamp, group, net header, full text, message id) for every intercept we hold.

    The id travels with the row because anything that ACCUMULATES findings across runs needs a
    stable address per intercept - without it a re-run of the same day doubles every count."""
    con = sqlite3.connect(f"file:{CORPUS}?mode=ro", uri=True)
    q = ("SELECT timestamp, group_name, coalesce(text,''), id FROM messages "
         "WHERE coalesce(group_jid,'') != ?")
    rows = con.execute(q, (TRANSFER_JID,)).fetchall()
    con.close()
    out = []
    for ts, g, t, mid in rows:
        if group and g != group:
            continue
        lines = [l.strip() for l in t.splitlines()]
        net = ""
        for i, l in enumerate(lines):
            if FREQ_LINE.match(l) and i + 1 < len(lines):
                net = lines[i + 1]
                break
        out.append((ts, g, net, t, mid))
    return out


def calibrate(codes: Codes, exclude_group: str) -> dict[int, dict]:
    """How much each code is worth: occurrences on ITS formation's nets against everyone else's."""
    own = collections.Counter()
    other = collections.Counter()
    for ts, g, net, t, _mid in corpus():
        if g == exclude_group:
            continue
        f = formation(net)
        if not f:
            continue
        sp = speech(t)
        if not sp:
            continue
        for i in codes.find(sp):
            (own if codes.rows[i]["form"] == f else other)[i] += 1
    stats = {}
    for i, r in enumerate(codes.rows):
        o, x = own[i], other[i]
        tot = o + x
        stats[i] = {"own": o, "other": x,
                    "power": (o / tot) if tot else None,   # None = never heard, unproven
                    "seen": tot}
    return stats


def verdict(st: dict) -> str:
    if not st["seen"]:
        return "НЕ ЧУТИ"            # never occurs in signed traffic - unproven, not useless
    if st["power"] >= 0.9 and st["own"] >= 5:
        return "СИЛЬНИЙ"
    if st["power"] >= 0.7 and st["own"] >= 3:
        return "СЕРЕДНІЙ"
    return "ШУМ"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="AllInARow")
    ap.add_argument("--day", help="YYYY-MM-DD, only that day of the group")
    ap.add_argument("--calibrate", action="store_true", help="print the code table's worth, stop")
    ap.add_argument("--show", type=int, default=25)
    a = ap.parse_args()

    codes = Codes()
    with_unit = sum(1 for r in codes.rows if r["form"])
    print(f"кодів у глосарії: {len(codes.rows)} (з підрозділом: {with_unit}, "
          f"слів: {len(codes.by_stem)}, чисел: {len(codes.by_num)}, фраз: {len(codes.phrases)})")

    print("міряю силу кожного коду на підписаному ефірі...", file=sys.stderr)
    stats = calibrate(codes, a.group)

    if a.calibrate:
        buckets = collections.Counter(verdict(stats[i]) for i in range(len(codes.rows))
                                      if codes.rows[i]["form"])
        print("\nсила кодів (тільки ті, що мають підрозділ):", dict(buckets))
        for want in ("СИЛЬНИЙ", "СЕРЕДНІЙ"):
            rows = [(i, stats[i]) for i in range(len(codes.rows))
                    if codes.rows[i]["form"] and verdict(stats[i]) == want]
            rows.sort(key=lambda x: -x[1]["own"])
            print(f"\n--- {want} ({len(rows)}) ---")
            for i, st in rows[:a.show]:
                r = codes.rows[i]
                print(f"  {str(r['term'])[:22]:<22} {r['form']:<10} свої:{st['own']:5} "
                      f"чужі:{st['other']:5}  {(r['meaning'] or '')[:42]}")
        noisy = [(i, stats[i]) for i in range(len(codes.rows))
                 if codes.rows[i]["form"] and verdict(stats[i]) == "ШУМ"]
        noisy.sort(key=lambda x: -x[1]["seen"])
        print(f"\n--- ШУМ, найгучніший ({len(noisy)}) ---")
        for i, st in noisy[:12]:
            r = codes.rows[i]
            print(f"  {str(r['term'])[:22]:<22} {r['form']:<10} свої:{st['own']:5} "
                  f"чужі:{st['other']:5}")
        return 0

    rows = corpus(a.group)
    if a.day:
        d = datetime.strptime(a.day, "%Y-%m-%d").date()
        rows = [r for r in rows if datetime.fromtimestamp(r[0] / 1000).date() == d]
    found = []
    for ts, g, net, t in rows:
        sp = speech(t)
        if not sp:
            continue
        ev = []
        for i in codes.find(sp):
            r = codes.rows[i]
            if not r["form"]:
                continue
            v = verdict(stats[i])
            if v == "ШУМ":
                continue
            ev.append((v, r, stats[i]))
        if ev:
            rank = {"СИЛЬНИЙ": 0, "СЕРЕДНІЙ": 1, "НЕ ЧУТИ": 2}
            ev.sort(key=lambda e: (rank[e[0]], -e[2]["own"]))
            found.append((ts, net, t, ev))
    strong = [f for f in found if f[3][0][0] == "СИЛЬНИЙ"]
    print(f"\nперехоплень у групі «{a.group}»"
          + (f" за {a.day}" if a.day else "") + f": {len(rows)}")
    print(f"із кодовим словом, що взагалі щось означає: {len(found)}"
          f"   (з них із СИЛЬНИМ кодом: {len(strong)})")
    for ts, net, t, ev in sorted(found, key=lambda f: ({"СИЛЬНИЙ": 0, "СЕРЕДНІЙ": 1,
                                                        "НЕ ЧУТИ": 2}[f[3][0][0]], f[0]))[:a.show]:
        when = datetime.fromtimestamp(ts / 1000).strftime("%d.%m.%Y %H:%M")
        print("\n" + "=" * 92)
        print(f"{when}  {net[:70]}")
        for v, r, st in ev[:6]:
            print(f"   [{v}] «{r['term']}» -> {r['form']} : {(r['meaning'] or '')[:50]}"
                  f"   (свої {st['own']}, чужі {st['other']})")
        for line in speech(t).splitlines()[:6]:
            print("   " + line[:150])
    return 0


if __name__ == "__main__":
    sys.exit(main())
