#!/usr/bin/env python3
"""Our own register of who-is-who, accumulated from the report events — never re-guessed.

    python3 tools/identity/roles_db.py ingest --day 03.11      # one report's events (idempotent)
    python3 tools/identity/roles_db.py ingest --all            # every ZVIT_*_events.json on disk
    python3 tools/identity/roles_db.py summary [--role komsklad]

Why it exists (owner, 03.11.2026). The callsigns and roles printed under a network header come from
the previous analyst's archive (`reports.db`), which stopped on 24.08.2026. People rotated, the number
of networks doubled, and by November 60% of the networks went out with a bare header. Nobody was
adding anyone. This file is the "somebody".

How it decides, and why it is built the way it is:

  * The evidence is the REPORT EVENTS. The model already read the speech when it wrote «переміщення
    в\\с X - веде ДОК» and those lines were checked against the speech three times (A/B, audit,
    cleanup). Reading them again with regexes costs nothing and inherits all of that checking.
  * The DATABASE IS STABLE. Every piece of evidence is stored once, keyed by the intercept it came
    from, so re-running a report or ingesting the same day twice adds nothing. A role becomes ACTIVE
    when the evidence crosses the threshold and stays active — it is not recomputed from scratch.
  * The OWNER'S VERDICT WINS and is never overwritten. If he removed a man and new evidence arrives,
    the man stays removed; the new evidence is only counted so he can look again.
  * The key is callsign + FORMATION, never the bare callsign: one name lives under several units
    (`docs-vs-air`). The formation is read from the network header of the event.

Roles and what counts as one case (an EPISODE):

  komsklad   — he manages people: «веде X», «керує рухом X», «наказ X …», «за наказом X».
  nakopych   — people are sent TO him: «до позиції/укриття в\\с X», «до в\\с X», «зустрічає X».

Threshold (owner, 03.11.2026): 3 episodes, and they must fall on at least 2 different days, so one
busy night cannot make a commander. An episode is one event line; two report files carrying the same
line (the lead-in hour) are one episode, because the key is the intercept.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "knowledge" / "upstream" / "reports_out"
DB = REPO / "knowledge" / "upstream" / "identity" / "roles.db"

MIN_EPISODES = 3
# Owner, 03.11.2026: «ком склад» from 5 cases. Measured against the analyst's archive the same day:
# at 3-4 cases 9 of 12 checkable men were command staff (КАСПЕР, ШИП were staging points), at 5+
# 21 of 22 (the one «miss», БАЙКАЛ, is a battalion commander by a post heard on air).
MIN_EPISODES_BY_ROLE = {"komsklad": 5}
MIN_DAYS = 2
# Distinct OTHER people in the episodes. Added on the self-check of 03.11: «до позиції в\\с ГЕН» counted
# ИГРИК three times, and a man everybody goes to RESCUE (ФЕРОМОН, buried) looked like a staging point.
MIN_PARTNERS = {"komsklad": 2, "nakopych": 3}
# A trip to dig someone out, carry a wounded man or evacuate is not «people are sent to him».
RESCUE = re.compile(r"відкоп|допомог|евакуац|поранен|\b300\b|\b200\b|тіл[оа]|носилк", re.I)

ROLES = {
    "komsklad": "ком склад",
    "nakopych": "накопичувач",
    "posada": "посади",   # heard on air, written by posady.py, no threshold
    "bpla_log": "БпЛА логістики",   # written by bpla_log.py, 3 cases on 2 days
}
BEHAVIOUR = tuple(MIN_PARTNERS)   # the roles THIS file infers; posady.py owns `posada`

NAME = r"[А-ЯЁІЇЄҐ][А-ЯЁІЇЄҐ\-]{2,}"
NAMES = rf"{NAME}(?:\s*(?:,|/|\bта\b|\bі\b|\bи\b)\s*(?:в\\с\s+)?{NAME})*"
STOP = {"НВ", "СОУ", "РОВ", "БПЛА", "ФПВ", "МТЗ", "БК", "ДРГ", "ТЗ", "АТ", "МТ", "ВУ", "НП", "КНП", "ПВН",
        "ЛС", "ОР", "ГР", "ШГ", "ІМ", "НЕ", "ОС", "СП", "ВЗ", "УКХ", "DMR", "FPV", "ЗАХ", "СХ", "ПН", "ПД",
        "ПІВН", "ПІВД", "СХІД", "ЗАХІД", "МАВІК", "MAVIC", "ВАМПІР", "ЖДУН", "HYTERA", "МАКС", "STARLINK"}

_LEAD = [
    ("веде", re.compile(rf"вед(?:е|уть)\s+(?:в\\с\s+)?({NAMES})")),
    ("керує", re.compile(rf"керу\w+\s+(?:рухом\s+)?(?:в\\с\s+)?({NAMES})")),
    ("наказ", re.compile(rf"наказ\s+(?!в\\с|гр\b|о\\с|н\\в)({NAME})")),
    ("наказ", re.compile(rf"за (?:наказом|вказівк\w+)\s+(?:в\\с\s+)?({NAME})")),
]
_MOVERS = re.compile(rf"(?:переміщенн\w*|рух|вихід|висуванн\w*|прибутт\w*|відхід|евакуаці\w*)\s+"
                     rf"(?:гр\s+)?(?:в\\с\s+|о\\с\s+)?({NAMES})")
_ADDRESSEE = re.compile(rf"(?:наказ\s+{NAME}\s+(?:\([^)]*\)\s+)?(?:в\\с|гр)\s+({NAMES}))|адресовано\s+({NAMES})")
_ACCUM = [
    ("до позиції", re.compile(rf"до (?:позиції|укриття|опорного пункту|бліндажа|точки)\s+(?:гр\s+)?в\\с\s+({NAME})")),
    ("до в\\с", re.compile(rf"\bдо в\\с\s+({NAME})")),
    ("зустрічає", re.compile(rf"зустріча\w+\s+(?:в\\с\s+)?({NAME})")),
]

# A waypoint is not a staging point (owner, 03.11.2026): «прибуття до позиції в\с ВАЛЕРА, далі продовжив
# рух», «до в\с КИПИШ, забір обладнання, далі до позиції СТАРЛИНГ». A route that GOES ON from the name
# within the same clause says the man passed through. Checked on the text right after the match only,
# so two separate moves to two different men in one line both still count.
_TRANSIT = re.compile(r"\b(?:далі|звідти|потім|після чого|продовжи\w*)\b", re.I)
TRANSIT_WINDOW = 70

_UNIT = re.compile(r"(\d{1,4})\s*(омсбр|омбр|мсбр|мсп|пмп|дмп|мсд|бр)\b", re.I)


def split_names(s: str) -> list[str]:
    out = []
    for p in re.split(r"\s*(?:,|/|\bта\b|\bі\b|\bи\b)\s*", s):
        p = p.replace("в\\с", "").strip()
        if re.fullmatch(NAME, p) and p not in STOP:
            out.append(p)
    return out


def formation(net: str | None) -> str:
    """The biggest unit named in the header: «1198 мсп», «38 омсбр». Empty when the header has none."""
    hits = _UNIT.findall(net or "")
    if not hits:
        return ""
    rank = {"мсд": 0, "омсбр": 1, "омбр": 1, "мсбр": 1, "бр": 1, "мсп": 2, "пмп": 2, "дмп": 2}
    num, kind = sorted(hits, key=lambda h: rank.get(h[1].lower(), 9))[0]
    return f"{num} {kind.lower()}"


def unit_key(e: dict) -> str:
    f = formation(e.get("_net"))
    if f:
        return f
    refs = e.get("_src_ref") or []
    return ("част " + refs[0].split()[-1]) if refs else "н\\в"


def event_day(e: dict) -> str:
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", e.get("time", ""))
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def src_key(e: dict) -> str:
    refs = sorted(e.get("_src_ref") or [])
    return refs[0] if refs else f"{e.get('time')}|{e.get('text', '')[:60]}"


def extract(e: dict) -> list[tuple[str, str, str, list[str]]]:
    """(role, callsign, kind, partners) for one event line."""
    t = e.get("text", "")
    movers = []
    for m in _MOVERS.finditer(t):
        movers += split_names(m.group(1))
    addressees = []
    for m in _ADDRESSEE.finditer(t):
        addressees += split_names(m.group(1) or m.group(2) or "")
    out, leaders = [], set()
    for kind, rx in _LEAD:
        for m in rx.finditer(t):
            for n in split_names(m.group(1)):
                partners = [p for p in (movers if kind != "наказ" else addressees or movers) if p != n]
                out.append(("komsklad", n, kind, partners))
                leaders.add(n)
    for kind, rx in _ACCUM:
        if RESCUE.search(t):
            break
        for m in rx.finditer(t):
            if _TRANSIT.search(t[m.end():m.end() + TRANSIT_WINDOW]):
                continue
            for n in split_names(m.group(1)):
                if n in leaders:
                    continue
                partners = [p for p in movers if p != n]
                if partners or kind == "зустрічає":
                    out.append(("nakopych", n, kind, partners))
    return out


SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY, role TEXT, callsign TEXT, unit TEXT, src_key TEXT,
    day TEXT, kind TEXT, partners TEXT, event_time TEXT, event_text TEXT, net TEXT,
    src_refs TEXT, report TEXT, added TEXT,
    UNIQUE(role, callsign, unit, src_key));
CREATE TABLE IF NOT EXISTS ev_refs (
    role TEXT, callsign TEXT, unit TEXT, ref TEXT, PRIMARY KEY(role, callsign, unit, ref));
CREATE TABLE IF NOT EXISTS verdict (
    role TEXT, callsign TEXT, unit TEXT, net_label TEXT,
    machine TEXT DEFAULT 'candidate', activated TEXT, owner TEXT, owner_ts TEXT,
    notified INTEGER DEFAULT 0, updated TEXT,
    PRIMARY KEY(role, callsign, unit));
"""


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    return con


def ingest_file(con: sqlite3.Connection, path: Path) -> int:
    raw = json.loads(path.read_text())
    events = raw if isinstance(raw, list) else raw.get("events", [])
    report = path.name[5:10]
    now = datetime.now().isoformat(timespec="seconds")
    added = 0
    for e in events:
        refs = e.get("_src_ref") or []
        for role, name, kind, partners in extract(e):
            unit = unit_key(e)
            # The same moment reaches us twice: the lead-in hour repeats yesterday's lines, and a fixed
            # or merged line keeps only part of its references. ANY shared intercept = same episode.
            if refs and con.execute(
                    f"SELECT 1 FROM ev_refs WHERE role=? AND callsign=? AND ref IN "
                    f"({','.join('?' * len(refs))}) LIMIT 1", (role, name, *refs)).fetchone():
                # unit deliberately NOT in this check: the same line under a header that names the
                # formation one day and only the channel the next would otherwise count twice
                continue
            con.executemany("INSERT OR IGNORE INTO ev_refs VALUES(?,?,?,?)",
                            [(role, name, unit, r) for r in refs])
            cur = con.execute(
                "INSERT OR IGNORE INTO evidence(role,callsign,unit,src_key,day,kind,partners,event_time,"
                "event_text,net,src_refs,report,added) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (role, name, unit_key(e), src_key(e), event_day(e), kind, ",".join(partners),
                 e.get("time"), e.get("text"), e.get("_net"), json.dumps(e.get("_src_ref") or [],
                 ensure_ascii=False), report, now))
            added += cur.rowcount
    return added


def refresh_verdicts(con: sqlite3.Connection) -> list[tuple]:
    """Promote candidates that crossed the threshold. Never demotes, never touches `owner`."""
    now = datetime.now().isoformat(timespec="seconds")
    newly = []
    rows = con.execute("SELECT role, callsign, unit, COUNT(*), COUNT(DISTINCT day) FROM evidence "
                       f"WHERE role IN {BEHAVIOUR} GROUP BY role, callsign, unit").fetchall()
    for role, cs, unit, n, days in rows:
        net = con.execute("SELECT net FROM evidence WHERE role=? AND callsign=? AND unit=? "
                          "GROUP BY net ORDER BY COUNT(*) DESC LIMIT 1", (role, cs, unit)).fetchone()[0]
        con.execute("INSERT OR IGNORE INTO verdict(role,callsign,unit,net_label,updated) VALUES(?,?,?,?,?)",
                    (role, cs, unit, net, now))
        partners = {p for (ps,) in con.execute("SELECT partners FROM evidence WHERE role=? AND callsign=? "
                                               "AND unit=?", (role, cs, unit)) for p in (ps or "").split(",") if p}
        need = MIN_EPISODES_BY_ROLE.get(role, MIN_EPISODES)
        if n >= need and days >= MIN_DAYS and len(partners) >= MIN_PARTNERS[role]:
            # `activated` is the DATA day the threshold was crossed, not the wall clock: the bot lists
            # the newest on top, and a backfill of three months would otherwise stamp everyone «now».
            seen, n_so_far, crossed = set(), 0, ""
            for (d,) in con.execute("SELECT day FROM evidence WHERE role=? AND callsign=? AND unit=? "
                                    "ORDER BY day, event_time", (role, cs, unit)):
                n_so_far += 1
                seen.add(d)
                if n_so_far >= need and len(seen) >= MIN_DAYS:
                    crossed = d
                    break
            cur = con.execute("UPDATE verdict SET machine='active', activated=?, updated=? "
                              "WHERE role=? AND callsign=? AND unit=? AND machine!='active'",
                              (crossed, now, role, cs, unit))
            if cur.rowcount:
                newly.append((role, cs, unit, n, days))
    return newly


SURVEY_ENV = Path("/root/.krevetka-secrets/survey_bot.env")
OWNER = 323182394


def notify(con: sqlite3.Connection, dry: bool = False, mark_only: bool = False) -> int:
    """Tell the owner, through the survey bot, which roles became active since the last message.

    Each verdict is announced ONCE (`notified`). `--mark-only` silences the backlog without sending,
    which is what the first backfill needs: ninety names at once is not news.
    """
    rows = con.execute("SELECT v.role, v.callsign, COALESCE(v.title || ', ', '') || v.unit, COUNT(e.id), "
                       "COUNT(DISTINCT e.day) FROM verdict v "
                       "JOIN evidence e USING(role, callsign, unit) WHERE v.machine='active' AND v.notified=0 "
                       "GROUP BY v.rowid ORDER BY v.role, COUNT(e.id) DESC").fetchall()
    if not rows:
        return 0
    if mark_only:
        con.execute("UPDATE verdict SET notified=1 WHERE machine='active' AND notified=0")
        return len(rows)
    lines = ["👥 Нові ролі в реєстрі «хто є хто»:"]
    for role in ROLES:
        part = [r for r in rows if r[0] == role]
        if part:
            lines.append(f"\n{ROLES[role]} (+{len(part)}):")
            lines += [f"• {cs} · {unit} - {n} вип. за {d} діб" for _r, cs, unit, n, d in part]
    lines.append("\nПеревірити й зняти зайве - кнопкою нижче.")
    text = "\n".join(lines)[:3900]
    if dry:
        print(text)
        return len(rows)
    import urllib.parse
    import urllib.request
    tok = next(l.split("=", 1)[1].strip() for l in SURVEY_ENV.read_text().splitlines()
               if l.startswith("SURVEY_TG_TOKEN="))
    data = urllib.parse.urlencode({"chat_id": OWNER, "text": text, "reply_markup": json.dumps(
        {"inline_keyboard": [[{"text": "👥 Відкрити ролі", "callback_data": "r:home"}]]})}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=20) as r:
        ok = json.loads(r.read()).get("ok")
    if ok:
        con.execute("UPDATE verdict SET notified=1 WHERE machine='active' AND notified=0")
    return len(rows) if ok else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    nt = sub.add_parser("notify")
    nt.add_argument("--dry", action="store_true")
    nt.add_argument("--mark-only", action="store_true")
    i = sub.add_parser("ingest")
    g = i.add_mutually_exclusive_group(required=True)
    g.add_argument("--day", help="DD.MM of the report")
    g.add_argument("--all", action="store_true")
    i.add_argument("--rebuild", action="store_true",
                   help="drop evidence and machine state and re-read; the OWNER's ✅/❌ are kept. "
                        "Never delete roles.db by hand: his verdicts live in it.")
    s = sub.add_parser("summary")
    s.add_argument("--role", choices=list(ROLES))
    a = ap.parse_args()
    con = connect()
    if a.cmd == "notify":
        n = notify(con, dry=a.dry, mark_only=a.mark_only)
        con.commit()
        print(f"повідомлено/позначено: {n}")
        return
    if a.cmd == "ingest":
        files = ([OUT / f"ZVIT_{a.day}_events.json"] if a.day else
                 [Path(p) for p in sorted(glob.glob(str(OUT / "ZVIT_[0-9][0-9].[0-9][0-9]_events.json")))])
        if a.rebuild:
            # only the behavioural roles: posts come from posady.py and its `posada_seen` would not re-read them
            con.execute(f"DELETE FROM evidence WHERE role IN {BEHAVIOUR}")
            con.execute(f"DELETE FROM ev_refs WHERE role IN {BEHAVIOUR}")
            con.execute(f"DELETE FROM verdict WHERE owner IS NULL AND role IN {BEHAVIOUR}")
            con.execute(f"UPDATE verdict SET machine='candidate', activated=NULL WHERE role IN {BEHAVIOUR}")
        total = 0
        for f in files:
            if not f.exists():
                sys.exit(f"немає {f}")
            total += ingest_file(con, f)
        newly = refresh_verdicts(con)
        con.commit()
        print(f"файлів {len(files)}, нових свідчень {total}, нових активних ролей {len(newly)}")
        for role, cs, unit, n, days in newly:
            print(f"  + {ROLES[role]}: {cs} ({unit}) - {n} випадків за {days} діб")
    else:
        for role, label in ROLES.items():
            if a.role and role != a.role:
                continue
            act = con.execute("SELECT v.callsign, v.unit, COUNT(e.id), COUNT(DISTINCT e.day) FROM verdict v "
                              "JOIN evidence e USING(role, callsign, unit) WHERE v.role=? AND v.machine='active' "
                              "AND COALESCE(v.owner,'yes')='yes' GROUP BY v.callsign, v.unit "
                              "ORDER BY COUNT(e.id) DESC", (role,)).fetchall()
            cand = con.execute("SELECT COUNT(*) FROM verdict WHERE role=? AND machine='candidate'",
                               (role,)).fetchone()[0]
            print(f"== {label}: активних {len(act)}, кандидатів нижче порогу {cand}")
            for cs, unit, n, days in act:
                print(f"   {cs:14} {unit:12} {n:3} випадків, {days} діб")


if __name__ == "__main__":
    main()
