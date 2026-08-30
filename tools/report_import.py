#!/usr/bin/env python3
"""Parse the analyst's .docx reports (already extracted to text) into a queryable SQLite base.

Why SQLite and not JSON: the questions we will actually ask are queries — "every event mentioning
ФИЛИН", "which frequencies has this callsign been heard on", "all 300s in August", "what role was
assigned to ОСОКА". JSON would force a full re-read and a hand-rolled scan for each of those; a
40 KB index answers them instantly. The DB is derived data — it can always be rebuilt from the
.txt extracts, which are the thing worth keeping in git.

Report structure (stable across both files):

    145.6500/151.4700/...          ← frequency line, one or several
    УКХ р/м <опис> (<р-н>) DMR {1000}   ← network header
    СКИЛЕТ – ком склад             ← roster: callsign – role  (before the first event)
    ЛЕТО – кр 1 мср
    12.08.2026, 06:57 <подія>      ← event with full stamp
    07:07 <подія>                  ← event inheriting the last date
    <продовження попередньої події> ← a wrapped line

    python3 tools/report_import.py knowledge/upstream/reports_raw/*.txt --db knowledge/upstream/reports.db
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path

RE_FREQ = re.compile(r"^\s*(\d{2,3}\.\d{3,4}(?:\s*/\s*\d{2,3}\.\d{3,4})*)\s*$")
RE_NET = re.compile(r"^\s*(УКХ|КХ)\b", re.I)
RE_DMR = re.compile(r"\{(\d+)\}")
RE_UNIT = re.compile(r"(\d+\s*(?:омсбр|омбр|мсп|мсд|мсб|мср|шр|тп|адн|А)\b)", re.I)
RE_AREA = re.compile(r"\(([^)]*)\)")
RE_EV_FULL = re.compile(r"^\s*(\d{2}\.\d{2}\.\d{4})\s*,?\s*(\d{1,2}:\d{2})?\s*(.*)$")
RE_EV_TIME = re.compile(r"^\s*(\d{1,2}:\d{2})\s+(.*)$")

# The head of a roster/legend line is the KEY, and the key never contains a lowercase letter —
# callsigns are written uppercase, code words are quoted. The role that follows always starts
# lowercase (`ком склад`, `накопичувач`, `кр 1 мср`). That case boundary is what separates these
# lines from a wrapped continuation of the previous one, and it is far more reliable than the dash:
# the analyst uses an en dash and a plain hyphen interchangeably, often with no space at all
# (`СКИЛЕТ – ком склад`, `КУР-ком склад`), so keying on `–` alone silently swallowed most of the
# register — 70 rows imported where the two archives actually hold ~430, the rest glued into the
# tail of whatever entry happened to use an en dash.
_KEY = r"[А-ЯЁЇІЄҐA-Z0-9][А-ЯЁЇІЄҐA-Z0-9«»\"'’\s,/\.\-]{0,38}?"
RE_ROSTER = re.compile(rf"^\s*({_KEY})\s*[–—-]\s*(.+)$")
RE_BARE = re.compile(r"^\s*([А-ЯЁЇІЄҐA-Z][А-ЯЁЇІЄҐA-Z'’\-/ ]{1,38})\s*$")
# `АВГУСТ мол ком склад, проміжний накопичувач.` — a register entry whose dash the analyst simply
# forgot. Without this it was swallowed as a continuation of the line above, and BAZA came out
# carrying three other people's roles. The uppercase head followed directly by a lowercase word is
# the same case boundary the dashed form relies on, so it is recognised the same way.
RE_NODASH = re.compile(r"^\s*([А-ЯЁЇІЄҐA-Z][А-ЯЁЇІЄҐA-Z0-9'’\-/]{1,24})\s+([а-яёїієґ].{2,})$")
RE_LEGEND = re.compile(r"^\s*[«\"']([^»\"']{1,40})[»\"']\s*[–—-]?\s*(.*)$")
# Some sections quote the raw exchange under the header. A dash opening the line is a speaker turn,
# never a register entry — and without this the whole dialogue was glued into the role of the last
# callsign above it.
RE_SPEECH = re.compile(r"^\s*[—–-]\s")

# In the .docx two register lines sometimes share one paragraph and come out of the extractor with no
# separator at all: `ГРАНИТ – ком складМОНТАНА – ком склад (…)`, `…прийнято«88» – стан справ ?`. The
# join is recognisable because a lowercase letter runs straight into a NEW key that is itself
# followed by a dash. Requiring that following dash is what keeps ordinary roles intact — `ком склад
# підрозділу ДОН. Ком для ЧАКИ` and `звітує для МАРТ про стан` have capitals in them too, but no
# dash behind them.
RE_GLUED = re.compile(r"(?<=[а-яёїієґ])(?=(?:[А-ЯЁЇІЄҐA-Z]{3,}|«[^»]{1,20}»)\s*[–—-]\s)")

# `«54», «55» – стан справ` — one reading, several codes. Parsed as a single entry it made `54` mean
# the literal string `«55» – стан справ`.
RE_LEGEND_MULTI = re.compile(r"^\s*((?:[«\"'][^»\"']{1,40}[»\"']\s*,\s*)+[«\"'][^»\"']{1,40}[»\"'])"
                             r"\s*[–—-]\s*(.+)$")


def split_glued(line: str) -> list[str]:
    """One physical line that holds two register entries -> the two entries."""
    return [p.strip() for p in RE_GLUED.split(line) if p.strip()]


# `НВ` is not a callsign, it is the analyst writing "not established". It was being registered as a
# man and printed under three networks.
STOP_CALLSIGN = {"НВ", "НВ.", "Н\\В", "НВ ", "ОК"}


def register_line(net: dict, line: str) -> str | None:
    """Read ONE register line into the network. Returns what kind it was, or None."""
    m = RE_LEGEND_MULTI.match(line)
    if m:                       # `«54», «55» – стан справ` — one reading shared by several codes
        for code in re.findall(r"[«\"']([^»\"']{1,40})[»\"']", m.group(1)):
            net["legend"].append({"code": code.strip(), "meaning": m.group(2).strip(),
                                  "source_line": line})
        return "legend"
    m = RE_LEGEND.match(line)
    if m:
        net["legend"].append({"code": m.group(1).strip(), "meaning": m.group(2).strip(),
                              "source_line": line})
        return "legend"
    m = RE_ROSTER.match(line) or RE_NODASH.match(line)
    if m and m.group(1).strip():
        if m.group(1).strip().upper() in STOP_CALLSIGN:
            return None
        net["roster"].append({"callsign": m.group(1).strip(), "role": m.group(2).strip()})
        return "roster"
    m = RE_BARE.match(line)
    if m:                       # `ЛЕВША`, `ЗАЗА` — heard on the net, no role established
        if m.group(1).strip().upper() in STOP_CALLSIGN:
            return None
        net["roster"].append({"callsign": m.group(1).strip(), "role": ""})
        return "roster"
    return None

# A line in the register zone that heads neither a roster nor a legend entry is USUALLY the wrapped
# tail of the entry above it — except that in this whole archive it never once was. Both lines that
# ever reached here were something else: a standalone observation closing the block (`Відмічено за
# ідентичні татуювання для о\с 60 мсбр`, REP_12.07 l.217) and a stray clock artefact (`15:23:56`,
# REP_14.08 l.18). Appending them made «платье» read `пончо, халат Відмічено за ідентичні
# татуювання…` and «55» read `зрозуміло, прийнято 15:23:56` — the analyst's own words, moved onto a
# word he never attached them to. So the branch sorts them instead of assuming.
_RE_NOISE_LINE = re.compile(r"^[\d\s:.,\-–—/]+$")


def continuation_kind(line: str) -> str:
    """"noise" (drop) · "wrap" (tail of the entry above) · "note" (a remark of its own).

    A wrapped tail continues a sentence, so it opens on a lowercase letter or on punctuation; a new
    sentence opens on a capital. Getting this wrong is cheap in one direction only, which is why the
    doubt is resolved toward "note": a misjudged wrap still prints, on its own line in the same
    block, whereas a misjudged note silently rewrites the meaning of a code word.
    """
    if _RE_NOISE_LINE.match(line):
        return "noise"
    head = line.lstrip("«»\"'(-–— ")[:1]
    return "wrap" if head and head.islower() else "note"


SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY, source TEXT UNIQUE, sha256 TEXT, lines INTEGER, chars INTEGER);
CREATE TABLE IF NOT EXISTS networks (
    id INTEGER PRIMARY KEY, report_id INTEGER, line_no INTEGER,
    freqs TEXT, freq_count INTEGER, header TEXT, unit TEXT, area TEXT, dmr TEXT);
CREATE TABLE IF NOT EXISTS roster (
    id INTEGER PRIMARY KEY, network_id INTEGER, callsign TEXT, role TEXT);
CREATE TABLE IF NOT EXISTS legend (
    id INTEGER PRIMARY KEY, network_id INTEGER, code TEXT, meaning TEXT, source_line TEXT);
CREATE INDEX IF NOT EXISTS ix_legend_code ON legend(code);
-- A remark the analyst writes on its own line inside the register block, belonging to the NETWORK
-- rather than to any one callsign or code (`Відмічено за ідентичні татуювання для о\с 60 мсбр`).
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY, network_id INTEGER, line_no INTEGER, text TEXT);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, network_id INTEGER, line_no INTEGER,
    date TEXT, time TEXT, ts TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS ix_ev_net ON events(network_id);
CREATE INDEX IF NOT EXISTS ix_ev_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_roster_cs ON roster(callsign);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(text, content='events', content_rowid='id');
"""


def _iso(date: str | None, time: str | None) -> str | None:
    if not date:
        return None
    d, m, y = date.split(".")
    return f"{y}-{m}-{d}" + (f"T{time.zfill(5)}" if time else "")


def parse(path: Path) -> dict:
    text = path.read_text()
    lines = text.splitlines()
    report = {"source": path.name, "sha256": hashlib.sha256(text.encode()).hexdigest(),
              "lines": len(lines), "chars": len(text), "networks": []}
    net = None
    last_date = None
    seen_event = False           # roster lines only occur BEFORE the first event of a section
    last_kind = None             # was the previous head line a roster entry or a legend entry
    pending_freqs = None

    for i, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            continue

        m = RE_FREQ.match(line)
        if m:
            pending_freqs = [f.strip() for f in m.group(1).split("/") if f.strip()]
            continue

        if RE_NET.match(line):
            dmr = RE_DMR.search(line)
            unit = RE_UNIT.search(line)
            area = RE_AREA.search(line)
            net = {"line_no": i, "freqs": pending_freqs or [], "header": line,
                   "unit": (unit.group(1).strip() if unit else None),
                   "area": (area.group(1).strip() if area else None),
                   "dmr": (dmr.group(1) if dmr else None),
                   "roster": [], "legend": [], "notes": [], "events": []}
            report["networks"].append(net)
            pending_freqs, last_date, seen_event = None, None, False
            last_kind = None
            continue

        if net is None:                     # preamble before the first network
            continue

        m = RE_EV_FULL.match(line)
        if m and m.group(1):
            last_date = m.group(1)
            net["events"].append({"line_no": i, "date": last_date, "time": m.group(2),
                                  "text": m.group(3).strip()})
            seen_event = True
            continue

        m = RE_EV_TIME.match(line)
        if m:
            net["events"].append({"line_no": i, "date": last_date, "time": m.group(1),
                                  "text": m.group(2).strip()})
            seen_event = True
            continue

        if not seen_event:
            if RE_SPEECH.match(line):
                seen_event, last_kind = True, None    # the register zone is over
                continue
            kinds = [register_line(net, p) for p in split_glued(line)]
            kinds = [k for k in kinds if k]
            if kinds:
                last_kind = kinds[-1]
                continue

        # anything else after the first event is a wrapped continuation of it
        if net["events"]:
            net["events"][-1]["text"] += " " + line
        elif last_kind == "legend" and net["legend"]:
            kind = continuation_kind(line)
            if kind == "wrap":
                net["legend"][-1]["meaning"] += " " + line
            elif kind == "note":
                net["notes"].append({"line_no": i, "text": line})
        elif last_kind == "roster" and net["roster"] and re.search(r"[а-яёїієґa-z]", line):
            # A wrapped role always carries lowercase words. A line of nothing but capitals —
            # `СЕРБ, СЕДОЙ, МАЛОЙ` — is the station list of a quoted exchange, and appending it made
            # MARK's "role" a list of three other men. (That guard is why the capitals case never
            # reaches the sort below: here a "note" can only be a normal sentence.)
            kind = continuation_kind(line)
            if kind == "wrap":
                net["roster"][-1]["role"] += " " + line
            elif kind == "note":
                net["notes"].append({"line_no": i, "text": line})

    return report


def load(db: sqlite3.Connection, rep: dict) -> None:
    # Re-importing a source must not leave its old rows behind: `reports` is the only table with a
    # UNIQUE source, so deleting just that row used to orphan every network, roster, legend and
    # event of the previous import and silently double the base.
    old = [r[0] for r in db.execute(
        "SELECT n.id FROM networks n JOIN reports r ON r.id = n.report_id WHERE r.source = ?",
        (rep["source"],))]
    if old:
        marks = ",".join("?" * len(old))
        for t in ("events", "roster", "legend", "notes"):
            db.execute(f"DELETE FROM {t} WHERE network_id IN ({marks})", old)
        db.execute(f"DELETE FROM networks WHERE id IN ({marks})", old)
    db.execute("DELETE FROM reports WHERE source = ?", (rep["source"],))
    cur = db.execute("INSERT INTO reports(source, sha256, lines, chars) VALUES (?,?,?,?)",
                     (rep["source"], rep["sha256"], rep["lines"], rep["chars"]))
    rid = cur.lastrowid
    for net in rep["networks"]:
        cur = db.execute(
            "INSERT INTO networks(report_id, line_no, freqs, freq_count, header, unit, area, dmr)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (rid, net["line_no"], json.dumps(net["freqs"], ensure_ascii=False),
             len(net["freqs"]), net["header"], net["unit"], net["area"], net["dmr"]))
        nid = cur.lastrowid
        db.executemany("INSERT INTO roster(network_id, callsign, role) VALUES (?,?,?)",
                       [(nid, r["callsign"], r["role"]) for r in net["roster"]])
        db.executemany("INSERT INTO legend(network_id, code, meaning, source_line) VALUES (?,?,?,?)",
                       [(nid, l["code"], l["meaning"], l["source_line"]) for l in net["legend"]])
        db.executemany("INSERT INTO notes(network_id, line_no, text) VALUES (?,?,?)",
                       [(nid, n["line_no"], n["text"]) for n in net["notes"]])
        for ev in net["events"]:
            c = db.execute("INSERT INTO events(network_id, line_no, date, time, ts, text)"
                           " VALUES (?,?,?,?,?,?)",
                           (nid, ev["line_no"], ev["date"], ev["time"],
                            _iso(ev["date"], ev["time"]), ev["text"]))
            db.execute("INSERT INTO events_fts(rowid, text) VALUES (?,?)", (c.lastrowid, ev["text"]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--db", default="knowledge/upstream/reports.db")
    a = ap.parse_args()

    db = sqlite3.connect(a.db)
    db.executescript(SCHEMA)
    for f in a.files:
        rep = parse(Path(f))
        load(db, rep)
        ev = sum(len(n["events"]) for n in rep["networks"])
        ro = sum(len(n["roster"]) for n in rep["networks"])
        lg = sum(len(n["legend"]) for n in rep["networks"])
        print(f"{Path(f).name[:40]:40} мереж={len(rep['networks']):3} подій={ev:4} "
              f"реєстр={ro:4} легенда={lg:4}")
    # external-content FTS cannot be deleted from row by row; rebuilding keeps it in step with
    # `events` after a re-import that removed rows.
    db.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
    db.commit()
    print("\nВсього:", dict(zip(("reports", "networks", "roster", "legend", "events"), (
        db.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
        db.execute("SELECT COUNT(*) FROM networks").fetchone()[0],
        db.execute("SELECT COUNT(*) FROM roster").fetchone()[0],
        db.execute("SELECT COUNT(*) FROM legend").fetchone()[0],
        db.execute("SELECT COUNT(*) FROM events").fetchone()[0]))))


if __name__ == "__main__":
    main()
