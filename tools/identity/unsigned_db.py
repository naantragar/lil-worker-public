#!/usr/bin/env python3
"""Findings from the UNSIGNED nets. A base of its own, deliberately separate from everything else.

    python3 tools/identity/unsigned_db.py stats
    python3 tools/identity/unsigned_db.py net 402.8348      # the card of one frequency
    python3 tools/identity/unsigned_db.py voice ТАЙФУН      # where this voice is heard, what he does
    python3 tools/identity/unsigned_db.py leads             # who leads whom - the top-value class
    python3 tools/identity/unsigned_db.py match             # voices that also exist on SIGNED nets

## Why a separate base, and why that is not fussiness

An unsigned net has a life ahead of it: one day the analysts will sign that frequency, and from
then on its traffic flows into the signed corpus where the register, the dossier and the report
already know what to do. Until that day our findings about it are **guesses about an unknown**, and
they must not be mixed into a base that holds established fact. The owner's rule, 28.09.2026:
«чтобы мы не намешали всё в кучу и не запутались вкрай… чтобы код не имел даже технической
возможности случайно перебежать в другую базу».

So the separation is enforced by construction, not by care:

* This module writes to **exactly one file** - `knowledge/upstream/unsigned/unsigned.db`. It holds no
  writable handle to anything else, ever.
* The corpus (`messages.db`) and the glossary (`glossary.db`) are opened `mode=ro` and only for
  comparison. A comparison result is stored as OUR row, with `base` naming where it came from, so
  a line in this base can never be mistaken for a line of theirs.
* Nothing here is an attribution. `match` prints candidates and the counts behind them; joining an
  unsigned net to a formation is a human decision and is taken elsewhere.

## What is kept

    nets    one row per frequency: when heard, how much, what it currently points at
    voices  one row per (frequency, callsign): how often, from the header or from the speech
    leads   who LED whom, with the verbatim phrase - the class the targeting priority puts first
    hints   every piece of attribution evidence, so it accumulates across days instead of dying
            in a report file
    seen    intercept ids already folded in, so re-running a day changes nothing
"""
from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DB = REPO / "knowledge" / "upstream" / "unsigned" / "unsigned.db"   # the ONLY file this module writes

SCHEMA = """
CREATE TABLE IF NOT EXISTS nets (
    freq TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT, intercepts INTEGER DEFAULT 0,
    header TEXT, points_to TEXT, weight INTEGER DEFAULT 0, note TEXT);
CREATE TABLE IF NOT EXISTS voices (
    freq TEXT, name TEXT, heard INTEGER DEFAULT 0,
    from_header INTEGER DEFAULT 0, from_speech INTEGER DEFAULT 0,
    first_seen TEXT, last_seen TEXT, PRIMARY KEY (freq, name));
CREATE TABLE IF NOT EXISTS leads (
    msg_id TEXT, freq TEXT, leader TEXT, quote TEXT, when_at TEXT,
    PRIMARY KEY (msg_id, leader));
CREATE TABLE IF NOT EXISTS hints (
    msg_id TEXT, freq TEXT, when_at TEXT, kind TEXT, points_to TEXT,
    weight INTEGER, detail TEXT, counts TEXT,
    base TEXT DEFAULT 'signed-corpus',   -- where the comparison came from, never confused with ours
    PRIMARY KEY (msg_id, kind, detail));
CREATE TABLE IF NOT EXISTS seen (msg_id TEXT PRIMARY KEY, when_at TEXT, run TEXT);
"""


def con() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB))
    c.executescript(SCHEMA)
    c.row_factory = sqlite3.Row
    return c


def _touch_net(c, freq, when, header):
    r = c.execute("SELECT first_seen FROM nets WHERE freq=?", (freq,)).fetchone()
    if r:
        c.execute("UPDATE nets SET last_seen=max(last_seen,?), intercepts=intercepts+1, "
                  "header=coalesce(nullif(?,''),header) WHERE freq=?", (when, header, freq))
    else:
        c.execute("INSERT INTO nets (freq, first_seen, last_seen, intercepts, header) "
                  "VALUES (?,?,?,1,?)", (freq, when, when, header))


def _touch_voice(c, freq, name, when, in_header):
    c.execute("INSERT INTO voices (freq,name,heard,from_header,from_speech,first_seen,last_seen) "
              "VALUES (?,?,1,?,?,?,?) ON CONFLICT(freq,name) DO UPDATE SET "
              "heard=heard+1, from_header=from_header+?, from_speech=from_speech+?, "
              "last_seen=max(last_seen,?)",
              (freq, name, int(in_header), int(not in_header), when, when,
               int(in_header), int(not in_header), when))


def ingest(found: list[dict], run_label: str = "") -> dict:
    """Fold one run's findings in. Idempotent: an intercept already folded in is skipped, so
    re-running a day (which we do often while tuning) never doubles a count."""
    c = con()
    added = collections.Counter()
    for r in found:
        mid = str(r.get("msg_id") or "")
        when = datetime.fromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
        freq = str(r.get("freq") or "")
        if not mid or not freq:
            continue
        if c.execute("SELECT 1 FROM seen WHERE msg_id=?", (mid,)).fetchone():
            continue
        c.execute("INSERT INTO seen (msg_id, when_at, run) VALUES (?,?,?)", (mid, when, run_label))
        _touch_net(c, freq, when, r.get("net") or "")
        added["перехоплень"] += 1

        header = {n.upper() for n in (r.get("header") or [])}
        for n in header:
            _touch_voice(c, freq, n, when, True)
            added["голосів"] += 1
        x = r.get("x") or {}
        for cs in x.get("callsigns") or []:
            if not isinstance(cs, dict):
                continue
            n = " ".join(str(cs.get("name") or "").upper().split())
            if n and n not in header and 2 <= len(n) <= 20:
                _touch_voice(c, freq, n, when, False)
                added["голосів із мови"] += 1

        lead = x.get("leading") or {}
        if isinstance(lead, dict) and lead.get("who"):
            who = str(lead["who"])[:40]
            c.execute("INSERT OR IGNORE INTO leads (msg_id,freq,leader,quote,when_at) "
                      "VALUES (?,?,?,?,?)",
                      (mid, freq, who, str(lead.get("quote") or "")[:300], when))
            added["ведення"] += 1

        for e in r.get("evidence") or []:
            if not e.get("points_to"):
                continue
            c.execute("INSERT OR IGNORE INTO hints "
                      "(msg_id,freq,when_at,kind,points_to,weight,detail,counts) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (mid, freq, when, e["kind"], e["points_to"], e.get("weight", 0),
                       e.get("detail", "")[:300], e.get("counts", "")))
            added["підказок"] += 1

    # refresh what each net currently points at, from ALL hints ever recorded for it
    for (freq,) in c.execute("SELECT DISTINCT freq FROM hints").fetchall():
        w = collections.Counter()
        for h in c.execute("SELECT points_to, weight FROM hints WHERE freq=?", (freq,)):
            w[h["points_to"]] += h["weight"] or 0
        if w:
            top, val = w.most_common(1)[0]
            c.execute("UPDATE nets SET points_to=?, weight=? WHERE freq=?", (top, val, freq))
    c.commit()
    c.close()
    return dict(added)


# ── reading it back ──────────────────────────────────────────────────────────────────────────────

def stats() -> None:
    c = con()
    n = c.execute("SELECT count(*), sum(intercepts) FROM nets").fetchone()
    v = c.execute("SELECT count(DISTINCT name) FROM voices").fetchone()[0]
    l = c.execute("SELECT count(*), count(DISTINCT leader) FROM leads").fetchone()
    print(f"БАЗА НЕПІДПИСАНИХ — {DB}")
    print(f"  мереж (частот): {n[0]}, перехоплень у них: {n[1] or 0}")
    print(f"  різних голосів: {v}")
    print(f"  випадків ведення: {l[0]}, різних ведучих: {l[1]}")
    print("\n  найгустіші мережі:")
    for r in c.execute("SELECT freq, intercepts, points_to, weight, first_seen, last_seen "
                       "FROM nets ORDER BY intercepts DESC LIMIT 12"):
        print(f"    {r['freq']:<12} {r['intercepts']:4} перехоплень  "
              f"{(r['points_to'] or '-'):<12} вага {r['weight'] or 0:3}  "
              f"{r['first_seen'][:10]} .. {r['last_seen'][:10]}")
    c.close()


def net_card(freq: str) -> None:
    c = con()
    r = c.execute("SELECT * FROM nets WHERE freq=?", (freq,)).fetchone()
    if not r:
        sys.exit(f"частоти {freq} у базі непідписаних немає")
    print(f"МЕРЕЖА {freq}  ({r['intercepts']} перехоплень, {r['first_seen'][:10]} .. "
          f"{r['last_seen'][:10]})")
    print(f"  останній заголовок: {r['header'] or '-'}")
    print(f"  вказує на: {r['points_to'] or '-'}  (вага {r['weight'] or 0}) — ПІДКАЗКА, НЕ ВИСНОВОК")
    print("\n  голоси:")
    for v in c.execute("SELECT * FROM voices WHERE freq=? ORDER BY heard DESC", (freq,)):
        src = []
        if v["from_header"]:
            src.append(f"шапка {v['from_header']}")
        if v["from_speech"]:
            src.append(f"мова {v['from_speech']}")
        print(f"    {v['name']:<16} {v['heard']:3} разів  ({', '.join(src)})  "
              f"{v['first_seen'][:16]} .. {v['last_seen'][:16]}")
    ld = list(c.execute("SELECT * FROM leads WHERE freq=? ORDER BY when_at", (freq,)))
    if ld:
        print(f"\n  ВЕДЕННЯ — {len(ld)} випадк(ів):")
        for x in ld:
            print(f"    {x['when_at'][:16]}  {x['leader']}")
            print(f"       «{x['quote'][:140]}»")
    print("\n  підказки про належність:")
    for h in c.execute("SELECT kind, points_to, count(*) n, max(counts) cc FROM hints "
                       "WHERE freq=? GROUP BY kind, points_to ORDER BY n DESC", (freq,)):
        print(f"    [{h['kind']}] -> {h['points_to']}  ×{h['n']}  {h['cc'] or ''}")
    c.close()


def voice_card(name: str) -> None:
    name = name.upper()
    c = con()
    rows = list(c.execute("SELECT * FROM voices WHERE name=? ORDER BY heard DESC", (name,)))
    if not rows:
        sys.exit(f"голосу {name} у базі непідписаних немає")
    print(f"ГОЛОС {name} — у непідписаних мережах")
    for v in rows:
        print(f"  {v['freq']:<12} {v['heard']:3} разів  {v['first_seen'][:16]} .. "
              f"{v['last_seen'][:16]}")
    ld = list(c.execute("SELECT * FROM leads WHERE leader=? ORDER BY when_at", (name,)))
    if ld:
        print(f"\n  ВОДИТЬ — {len(ld)} випадк(ів):")
        for x in ld:
            print(f"    {x['when_at'][:16]} · {x['freq']}\n       «{x['quote'][:140]}»")
    c.close()


def leads_all() -> None:
    c = con()
    print("ХТО ВОДИТЬ ЛЮДЕЙ — усе, що накопичилось у непідписаних\n")
    for r in c.execute("SELECT leader, count(*) n, count(DISTINCT freq) f, min(when_at) a, "
                       "max(when_at) b FROM leads GROUP BY leader ORDER BY n DESC"):
        print(f"  {r['leader']:<18} {r['n']:3} рази, мереж: {r['f']}  "
              f"{r['a'][:16]} .. {r['b'][:16]}")
    c.close()


def match_signed() -> None:
    """Voices here that are ALSO heard on signed nets. Read-only on the corpus; the result is our
    own row and is printed as a candidate, never written back into anything."""
    sys.path.insert(0, str(HERE))
    from code_sieve import corpus, formation, FREQ_LINE, callsigns_of   # noqa: E402

    by_call = collections.defaultdict(collections.Counter)
    for ts, g, net, t, _mid in corpus():
        if g == "AllInARow":
            continue
        f = formation(net)
        if not f:
            continue
        lines = [l.strip() for l in t.splitlines()]
        for i, l in enumerate(lines):
            if FREQ_LINE.match(l):
                for x in callsigns_of(lines, i):
                    by_call[x][f] += 1
                break
    c = con()
    print("ГОЛОСИ З НЕПІДПИСАНИХ, ЯКІ ЗВУЧАТЬ І НА ПІДПИСАНИХ")
    print("Це КАНДИДАТИ на те, що це та сама людина. Однакове імʼя - ще не та сама людина:")
    print("приблизно кожен третій збіг по голому імені виявляється іншим чоловіком.\n")
    rows = list(c.execute("SELECT name, sum(heard) h, count(DISTINCT freq) f FROM voices "
                          "GROUP BY name ORDER BY h DESC"))
    lead_names = {r[0] for r in c.execute("SELECT DISTINCT leader FROM leads")}
    shown = 0
    for r in rows:
        forms = by_call.get(r["name"])
        if not forms:
            continue
        sole = len(forms) == 1
        mark = " ★ ВОДИТЬ" if r["name"] in lead_names else ""
        top = ", ".join(f"{a} ({b})" for a, b in forms.most_common(3))
        print(f"  {r['name']:<16} у непідписаних {r['h']:3} раз / {r['f']} мереж  ->  "
              f"{'ЛИШЕ ' if sole else ''}{top}{mark}")
        shown += 1
    print(f"\n  збігів: {shown} з {len(rows)} голосів")
    c.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["stats", "net", "voice", "leads", "match"])
    ap.add_argument("arg", nargs="?")
    a = ap.parse_args()
    if a.cmd == "net":
        net_card(a.arg or sys.exit("треба частота"))
    elif a.cmd == "voice":
        voice_card(a.arg or sys.exit("треба позивний"))
    elif a.cmd == "leads":
        leads_all()
    elif a.cmd == "match":
        match_signed()
    else:
        stats()
    return 0


if __name__ == "__main__":
    sys.exit(main())
