#!/usr/bin/env python3
"""Intercept analytics v2 — a window of intercepts → the daily report, in one pass.

    python3 tools/analytics2/run.py --from '2026-08-23 15:00' --to '2026-08-24 15:00' \
        --band 'ЗАЛІЗНИЧНЕ-ЗАГІРНЕ' --out ZVIT_24.08

Deliberately a clean rewrite; v1 lives on in tools/analytics_*.py and its rules are archived under
knowledge/upstream/archive/. The concept this implements is knowledge/upstream/intercept-analytics-v2-concept.md
— read that before changing anything here, especially §0: when the output is wrong, SUBTRACT from
rules.md. Do not add a stage.

What v2 drops from v1, on the owner's decision:
  * the A2 selection pass — one stage that describes what we take cannot lose material unseen;
  * the roster and code legend (pass C) — not the product, and 45% of the old wall clock.

What it keeps, because each was measured rather than assumed:
  * windowing oversized units of work (yield falls 6.25 → 3.85 events per 10k chars as they grow);
  * several extraction passes unioned (one pass silently loses ~25% of the facts);
  * retries around the model call (one transient failure used to abort a nine-minute run).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Every time in this pipeline — the window arguments, the intercept headers, the report stamp — is
# the analyst's local clock, which is Kyiv. Nothing here is ever server-local or UTC.
KYIV = ZoneInfo("Europe/Kyiv")

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "tools"))
from intercept_parse import parse_message  # noqa: E402

RULES = HERE / "rules.md"
GLOSSARY = REPO / "matrix" / "bot" / "prompts" / "refraz" / "10_glossary.md"
OUT_DIR = REPO / "knowledge" / "upstream" / "reports_out"
VERSIONS = HERE / ".versions"

MARGIN_HOURS = 6          # how far past the window to look for late POSTINGS
# The window starts this much earlier than it is declared — see main(). Raised 30 → 60 on
# 2026-08-27 after measuring the collector's real lag over 8 days: mean 30 min, p95 ~66, max 131.
# What that costs at each setting, counted as intercepts that missed their own report AND fell
# outside the next window's lead-in, i.e. lost from every report: 30 min → 6 of 12 798; 60 min → 0;
# 120 min → 0. An hour closes the hole completely, so there is no reason to pay for two.
WINDOW_LEAD_IN_MIN = 60
# "A report FOR the 20th" is the 24 hours ending at 15:00 on the 20th. The hour is the analyst's
# reporting cut, not a preference, and the band has been the same on every run we have made.
REPORT_HOUR = "15:00"
DEFAULT_BAND = "ЗАЛІЗНИЧНЕ-ЗАГІРНЕ"
MAX_CHUNK_CHARS = 16000   # estimated body; renders to ~19k, the size band with the best yield
OVERLAP = 3               # intercepts repeated across a seam so a straddling event stays whole
FREQ_TOL_KHZ = 5.0        # channel spacing is 12.5 kHz; closer than this is one channel
UNION_TOL_MIN = 20        # the same event is timed 06:04 by one pass and 06:07 by another
CALL_RETRIES = 3
RETRY_BACKOFF_S = 20

BARE_CWD = Path("/tmp/upstream_analytics2_cwd")
NO_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebFetch", "WebSearch",
            "Task", "Agent", "Workflow", "Skill", "NotebookEdit"]


# ─── window ────────────────────────────────────────────────────────────────────────────────────

def _own_dt(rec: dict) -> datetime | None:
    """When the exchange was HEARD, from the intercept's own header."""
    try:
        fmt = "%d.%m.%Y %H:%M:%S" if rec["time"].count(":") == 2 else "%d.%m.%Y %H:%M"
        return datetime.strptime(f"{rec['date']} {rec['time']}", fmt)
    except (KeyError, ValueError):
        return None


def fetch(dt_from: str, dt_to: str) -> list[dict]:
    """Pull the window by INTERCEPT time, not posting time.

    An exchange heard at 14:57 can be posted at 16:00 and still belongs to the earlier day, so the
    query reaches MARGIN_HOURS beyond the window on both sides and the real cut is made on each
    intercept's own header after parsing.
    """
    lo = datetime.strptime(dt_from, "%Y-%m-%d %H:%M")
    hi = datetime.strptime(dt_to, "%Y-%m-%d %H:%M")
    q_lo, q_hi = lo - timedelta(hours=MARGIN_HOURS), hi + timedelta(hours=MARGIN_HOURS)
    # The window is given in the analyst's own clock — Kyiv. The database stores UTC. The offset used
    # to be hardcoded `+03`, which is right only from April to October: after the autumn change Kyiv
    # is EET/+02 and every boundary would have been an hour out. Derived from the zone per timestamp
    # instead, so the switch passes unnoticed.
    def _pg(dt: datetime) -> str:
        return dt.replace(tzinfo=KYIV).strftime("%Y-%m-%d %H:%M%z")

    sql = (f"SELECT id || E'\\x01' || replace(coalesce(text,''), E'\\n', E'\\x02') "
           f"FROM source_messages WHERE group_name='PATAGONIA_GP' "
           f"AND occurred_ts >= '{_pg(q_lo)}' AND occurred_ts < '{_pg(q_hi)}' "
           f"ORDER BY occurred_ts, id")
    raw = subprocess.run(["docker", "exec", "upstream_db", "psql", "-U", "upstream", "-d", "upstream",
                          "-At", "-c", sql], capture_output=True, text=True, timeout=300).stdout
    out, undated = [], 0
    for line in raw.split("\n"):
        if "\x01" not in line:
            continue
        mid, body = line.split("\x01", 1)
        for rec in parse_message(body.replace("\x02", "\n")):
            rec["msg_id"] = mid
            rec["_dt"] = _own_dt(rec)
            if rec["_dt"] is None:
                undated += 1
                continue
            if lo <= rec["_dt"] < hi:
                out.append(rec)
    if undated:
        print(f"без розбірливого часу в шапці, пропущено: {undated}", file=sys.stderr)
    return out


# ─── networks ──────────────────────────────────────────────────────────────────────────────────

def norm_net(header: str | None) -> str:
    if not header:
        return "(без шапки)"
    h = re.sub(r"\s+", " ", header).strip().rstrip(".").lower()
    h = re.sub(r"\bйм\.?|\bім\.?", "", h)              # "ймовірно" is not part of the identity
    return re.sub(r"\s+", " ", h).strip()


def _freq_buckets(freqs: set[str]) -> dict[str, str]:
    """Near-identical frequency readings are one channel — 411.9630 and 411.9650 are 2 kHz apart."""
    vals = sorted((float(f), f) for f in freqs if _is_float(f))
    out, head = {}, None
    for v, s in vals:
        if head is None or (v - head[0]) * 1000 > FREQ_TOL_KHZ:
            head = (v, s)
        out[s] = head[1]
    return out


def _is_float(s) -> bool:
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


_COMPANY = re.compile(r"\b(\d+)\s*(мср|шр|мсв)\b", re.I)


def compose_name(members: list[str], raw_of: dict[str, dict[str, int]], seen: dict[str, int]) -> str:
    """One name for a merged network, in the analyst's own form.

    His register writes `8, 9 мср 3 мсб 60 омсбр` — and he explained why: the frequencies had been
    signed as different companies, and when the frequencies were merged the header named both. Our
    clustering already merges exactly those frequencies; this composes the NAME that was missing.
    """
    top = max(members, key=lambda x: (seen[x], len(x)))
    name = max(raw_of[top].items(), key=lambda kv: kv[1])[0]
    nums, kind = [], None
    for h in members:
        for n, k in _COMPANY.findall(h):
            if n not in nums:
                nums.append(n)
                kind = kind or k
    if len(nums) > 1 and kind:
        nums.sort(key=int)
        name = _COMPANY.sub(lambda m: f"{', '.join(nums)} {kind}", name, count=1)
    return name


def cluster(recs: list[dict]) -> dict[str, str]:
    """Headers sharing a frequency are one network. Frequency is a vestige for identification, but
    it IS the mechanism by which the analyst merged units, so we follow it and then name the result."""
    freqs_of: dict[str, set[str]] = defaultdict(set)
    seen: dict[str, int] = defaultdict(int)
    raw_of: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in recs:
        h = norm_net(r.get("network"))
        seen[h] += 1
        raw_of[h][r.get("network") or "(без шапки)"] += 1
        if r.get("freq"):
            freqs_of[h].add(r["freq"])

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    bucket = _freq_buckets({f for fs in freqs_of.values() for f in fs})
    owner: dict[str, str] = {}
    for h, fs in freqs_of.items():
        for f in fs:
            f = bucket.get(f, f)
            if f in owner:
                a, b = find(h), find(owner[f])
                if a != b:
                    parent[a] = b
            else:
                owner[f] = h

    members: dict[str, list[str]] = defaultdict(list)
    for h in seen:
        members[find(h)].append(h)
    return {h: compose_name(ms, raw_of, seen) for ms in members.values() for h in ms}


def units_of_work(records: list[dict]) -> list[dict]:
    """One network's whole day is one unit of work; only oversized ones are windowed by time.

    Slicing by network FIRST matters: a plain six-hour slice of the day would put thirty unrelated
    conversations into one call and make the model disentangle them before it can read anything.
    """
    canon = cluster([r for r in records if r.get("speech")])
    by_net: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if not r.get("speech") or r.get("_dt") is None:
            continue
        by_net[canon.get(norm_net(r.get("network")), norm_net(r.get("network")))].append(r)

    def weight(r):
        return sum(len(s) for s in r["speech"]) + 80

    out = []
    for net, items in by_net.items():
        items.sort(key=lambda x: x["_dt"])
        if sum(weight(r) for r in items) <= MAX_CHUNK_CHARS:
            out.append({"net": net, "items": items})
            continue
        cur, size, parts = [], 0, []
        for r in items:
            if cur and size + weight(r) > MAX_CHUNK_CHARS:
                parts.append(cur)
                cur = cur[-OVERLAP:]
                size = sum(weight(x) for x in cur)
            cur.append(r)
            size += weight(r)
        if cur:
            parts.append(cur)
        for k, p in enumerate(parts, 1):
            out.append({"net": net, "items": p, "part": (k, len(parts))})
    out.sort(key=lambda t: t["items"][0]["_dt"])
    return out


def render_unit(u: dict) -> str:
    head = u["net"] or "(мережа без шапки)"
    if u.get("part"):
        head += f"  [частина {u['part'][0]}/{u['part'][1]} довгої розмови]"
    freqs = sorted({r["freq"] for r in u["items"] if r.get("freq")})
    lines = [f"### МЕРЕЖА — {head}", f"частоти: {', '.join(freqs)}"]
    for n, r in enumerate(u["items"]):
        who = " / ".join(r["stations"]) if r["stations"] else "НВ"
        lines.append(f"[{n}] {r['date']}, {r['time'][:5]} — {who}")
        for tag in ("comment_above", "comment_below"):
            if r.get(tag):
                lines.append(f"    (позначка аналітика: {r[tag]})")
        lines += [f"    {s}" for s in r["speech"]]
    return "\n".join(lines)


# ─── model ─────────────────────────────────────────────────────────────────────────────────────

def call_model(system: str, material: str, model: str, effort: str) -> tuple[str, float]:
    BARE_CWD.mkdir(parents=True, exist_ok=True)
    cmd = ["claude", "-p", "--model", model, "--system-prompt", system,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--disallowedTools", *NO_TOOLS, "--effort", effort]
    t0 = time.monotonic()
    p = subprocess.run(cmd, input=material, capture_output=True, text=True,
                       timeout=3600, cwd=str(BARE_CWD))
    if p.returncode != 0:
        raise RuntimeError(f"claude exited {p.returncode}: {p.stderr[-300:]}")
    return p.stdout.strip(), time.monotonic() - t0


def extract_json(s: str) -> list[dict]:
    m = re.search(r"\[.*\]", s, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return []


# ─── union across passes ───────────────────────────────────────────────────────────────────────

_NAME = re.compile(r"[А-ЯЁЇІЄҐ][А-ЯЁЇІЄҐ\-]{2,}")
_STOP = {"ВУ", "СОУ", "РОВ", "БПЛА", "МТЗ", "ФПВ", "ДРГ", "БК", "ДМР", "УКХ"}


def names(t) -> set[str]:
    return {n for n in _NAME.findall(str(t or "")) if n not in _STOP}


def stamp(s) -> int | None:
    hm = re.search(r"(\d{1,2}):(\d{2})", str(s or ""))
    if not hm:
        return None
    out = int(hm.group(1)) * 60 + int(hm.group(2))
    d = re.search(r"(\d{2})\.(\d{2})", str(s or ""))
    if d:
        out += (int(d.group(1)) + int(d.group(2)) * 31) * 1440
    return out


def _dup_key(e: dict) -> tuple[str, str, str]:
    def n(v) -> str:
        return re.sub(r"\s+", " ", str(v or "")).strip().casefold().rstrip(".")
    return n(e.get("_net")), n(e.get("time")), n(e.get("text"))


def drop_exact_duplicates(events: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Drop events that repeat another one EXACTLY, and events with no text at all.

    The model sometimes writes the same fact twice inside ONE answer - 26.08 shipped with
    "26.08.2026, 09:07 в\\с ЛЕЛИК - 300" printed on two consecutive lines. Nothing upstream catches
    that: with --passes 1 union() never runs, and even with several passes it cannot help, because
    it deliberately lets an accumulated event absorb at most one event per pass (so that two
    genuinely distinct events written minutes apart are never collapsed) - which is exactly what
    lets an identical pair inside one pass survive as two.

    Only an EXACT repeat is dropped: same network, same timestamp, same text after collapsing
    whitespace and case. Two events that differ by a single word are two events, and this function
    must never be the place where material quietly disappears - that is why what it removes is
    printed rather than swallowed.
    """
    seen: dict[tuple[str, str, str], dict] = {}
    kept, dups, empty = [], [], []
    for e in events:
        if not str(e.get("text") or "").strip():
            empty.append(e)
            continue
        k = _dup_key(e)
        if k in seen:
            dups.append(e)
            # a repeat is not a confirmation: it is one fact stated twice, so _passes is left alone
            continue
        seen[k] = e
        kept.append(e)
    return kept, dups, empty


def union(passes: list[list[dict]]) -> list[dict]:
    """Add the passes up; never vote. A fact seen by one pass of five is exactly the material this
    exists to recover — three identical runs once gave 18/25/25 events with only ~75% in common, and
    what floated included a 300, a fire impact and a planned movement.

    An accumulated event absorbs at most ONE event per pass, so two genuinely distinct events written
    minutes apart inside a single pass are never collapsed into each other.
    """
    acc: list[dict] = []
    for e in (passes[0] if passes else []):
        e["_passes"] = 1
        acc.append(e)
    for evs in passes[1:]:
        taken: set[int] = set()
        for e in evs:
            t, ns = stamp(e.get("time")), names(e.get("text"))
            best, best_d = None, None
            for i, o in enumerate(acc):
                if i in taken or (o.get("_net") or "") != (e.get("_net") or ""):
                    continue
                ot = stamp(o.get("time"))
                if t is None or ot is None or abs(t - ot) > UNION_TOL_MIN:
                    continue
                if not (ns & names(o.get("text"))):
                    continue
                d = abs(t - ot)
                if best_d is None or d < best_d:
                    best, best_d = i, d
            if best is None:
                e["_passes"] = 1
                acc.append(e)
            else:
                taken.add(best)
                acc[best]["_passes"] = acc[best].get("_passes", 1) + 1
                if len(str(e.get("text", ""))) > len(str(acc[best].get("text", ""))):
                    acc[best]["text"] = e["text"]
    return acc


# ─── report ────────────────────────────────────────────────────────────────────────────────────

_SUBJ = re.compile(r"(?:в\\с|шг|груп\w*|розрахунк\w*)\s+([А-ЯЁЇІЄҐ][А-ЯЁЇІЄҐ\-]{2,})")


def subject(e: dict) -> str:
    """Who the line is about. The model states it in `who`; fall back to the first callsign."""
    w = str(e.get("who") or "").strip()
    if w:
        return w.upper()
    m = _SUBJ.search(str(e.get("text", "")))
    return m.group(1) if m else ""


REPORTS_DB = REPO / "knowledge" / "upstream" / "reports.db"
KNOWN_CODES = REPO / "knowledge" / "upstream" / "known_codes.md"


ROSTER_ACTIVE_DAYS = 3        # a callsign unheard for longer than this is not worth reminding of


def activity_index(dt_to: str) -> dict[str, dict]:
    """Who has actually been on the air lately, per frequency.

    The register printed under a network header is the analyst's accumulated list, and it accumulates
    forever: one network was printing 40 callsigns above 10 event lines. Most of them had not been
    heard in weeks — a man who has gone silent for that long has changed callsign, moved, or is dead,
    and reminding the reader of him costs more than it gives.

    The window ends at the report's own `--to`, so a callsign that surfaced again TODAY is back in
    immediately and stays for the next `ROSTER_ACTIVE_DAYS`.

    This is pure rendering — it runs after the model is finished and cannot affect what was
    extracted. The worst it can do is print too few names, so it fails OPEN: if the lookup comes back
    empty (a bad query, a DB hiccup), the register is printed in full rather than blanked.
    """
    hi = datetime.strptime(dt_to, "%Y-%m-%d %H:%M")
    recs = fetch(f"{hi - timedelta(days=ROSTER_ACTIVE_DAYS):%Y-%m-%d %H:%M}", dt_to)
    out: dict[str, dict] = defaultdict(lambda: {"names": set(), "speech": []})
    for r in recs:
        f = r.get("freq")
        if not _is_float(f):
            continue
        e = out[f]
        for raw in r.get("stations", []):
            for part in re.split(r"[,/]| та ", raw or ""):
                p = part.strip().strip(".").upper()
                if len(p) > 2:
                    e["names"].add(p)
        e["speech"].extend(r.get("speech", []))
    return out


def heard_on(index: dict[str, dict], freqs: list[str]) -> tuple[set[str], str]:
    """Everything heard on this network's channels inside the activity window."""
    names: set[str] = set()
    speech: list[str] = []
    mine = [float(x) for x in freqs if _is_float(x)]
    for f, e in index.items():
        if any(abs(float(f) - x) * 1000 <= FREQ_TOL_KHZ for x in mine):
            names |= e["names"]
            speech.extend(e["speech"])
    return names, "\n".join(speech).lower()


def still_active(name: str, names: set[str], blob: str) -> bool:
    """A callsign counts as active if it keyed the mic OR was talked about.

    Mentions count deliberately: a commander is discussed far more often than he transmits, and
    dropping him because he does not press the button would be the wrong error. Matching is a prefix
    match on the stem, so `Катану` and `Катаны` both hit — and a callsign that is also an ordinary
    word (ЗЕМЛЯ, БЕЛЫЙ, КОРОЛЬ) will match loosely and be KEPT. That bias is on purpose: printing a
    name too long is a small cost, dropping a live one is not.
    """
    key = name.strip().upper()
    if key in names:
        return True
    for v in re.split(r"[,/]", key):
        v = v.strip()
        if len(v) < 3:
            return True             # too short to match safely — never drop on this evidence
        if re.search(rf"\b{re.escape(v.lower())}", blob):
            return True
    return False


def _variants(word: str) -> set[str]:
    """A code and its spoken forms. `«глаза, глазки»` is ONE entry holding two."""
    return {v.strip().strip("«»\"'").lower()
            for v in re.split(r"[,/]", word or "") if v.strip().strip("«»\"'")}


def known_codes() -> set[str]:
    """Codes the desk already reads without thinking — kept OUT of the printed register block.

    The legend exists to tell the reader something new. A word every analyst knows is noise there and
    gets deleted by hand, so the owner curates this file and the block filters against it. Adding a
    word is cheap and reversible: nothing leaves the database, only the print.

    ONLY the `REGISTER BLOCK` section is read. The rest of the file filters readings our own RUN
    produced, which is a different source with a different verdict — reusing the whole list here
    would silently empty some networks' legends, and that is the owner's call, not a side effect.

    Parsed by the markdown's own `- \\`word\\` — reading` shape, so the list stays editable as prose.

    (v1 has the same parser in `tools/analytics_render.py`. Deliberately not imported: that module
    pulls in the whole v1 pipeline at import time, and v2 does not depend on v1. The FILE is the
    shared thing that matters, not the ten lines that read it.)
    """
    if not KNOWN_CODES.exists():
        return set()
    out, take = set(), False
    for line in KNOWN_CODES.read_text().splitlines():
        if line.startswith("## "):
            take = "REGISTER BLOCK" in line
            continue
        m = re.match(r"\s*-\s*`([^`]+)`", line)
        if m and take:
            out |= _variants(m.group(1))
    return out


def _freq_set(raw: str | None) -> list[float]:
    if not raw:
        return []
    try:
        vals = json.loads(raw) if raw.strip().startswith("[") else raw.split("/")
    except json.JSONDecodeError:
        vals = raw.split("/")
    out = []
    for v in vals:
        try:
            out.append(float(str(v).strip()))
        except ValueError:
            pass
    return out


_UNIT = re.compile(r"(\d{1,4})\s*(омсбр|омбр|мсбр|мсп|мсд|пмп|дшб|мсб|мср|бр|полк|бат)", re.I)


def unit_tags(text: str) -> set[str]:
    """The formations named in a network header — `186 мсп`, `57 омсбр`, `3 мсб`.

    Only the regiment/brigade level is used for matching (мсб/мср are sub-units and the analyst
    writes them inconsistently), so a header that names only a battalion produces no tag and the
    check falls open.
    """
    big = {"омсбр", "омбр", "мсбр", "мсп", "мсд", "пмп", "полк"}
    return {f"{n} {k.lower()}" for n, k in _UNIT.findall(text or "") if k.lower() in big}


def same_unit(mine_header: str, archive_header: str) -> bool:
    """May this archive network's register be printed under this report network?

    Frequency overlap alone is not enough. A CAPTURED radio puts two different formations on one
    channel: `57 омсбр 5А (трофей 11.08.26 Гірке)` shares a frequency with `186 мсп`, and on that
    single hit the whole 57 омсбр roster was being printed under the 186 мсп header — other people's
    men listed as if they were on this net. So when both headers name a formation and the two sets
    do not intersect, the register does not travel.

    Fails OPEN on purpose: if either header names no formation (plenty do not — `УКХ р/м нв
    підрозділу (дорозвідка р-н НОВОСЕЛІВКА)`), the frequency evidence is all there is and it decides.
    """
    a, b = unit_tags(mine_header), unit_tags(archive_header)
    return not (a and b) or bool(a & b)


def assign_registers(freqs_of: dict[str, list[str]]) -> dict[str, tuple[list, list]]:
    """Give every archive network to exactly ONE network of this report.

    The first version asked each report network independently "which archive networks share a
    frequency with me", and an archive network that matched three of them was printed under all
    three. The result looked exactly like what it was: `СЕРБ, БАЗА, ГРОМ` and one identical legend
    standing under `2 мсб 38 омсбр`, under `189 мсп (БАГАТЕ)` and under `189 мсп (НОВОСЕЛІВКА)` in
    the same report. A register that names the same men on three different nets is worse than none.

    So the assignment is made globally and is exclusive: each archive network goes to the report
    network it overlaps most, measured first by how many of its frequencies match and then by what
    share of that report network's own frequencies they cover — the more specific claim wins.
    """
    if not REPORTS_DB.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{REPORTS_DB}?mode=ro", uri=True)
        archive = [(nid, _freq_set(fr), hdr)
                   for nid, fr, hdr in con.execute("SELECT id, freqs, header FROM networks")]
    except sqlite3.Error:
        return {}

    mine_of = {net: [float(x) for x in fr if _is_float(x)] for net, fr in freqs_of.items()}
    owned: dict[str, list[int]] = defaultdict(list)
    for nid, afs, ahdr in archive:
        best, best_score = None, (0, 0.0)
        for net, mine in mine_of.items():
            if not mine:
                continue
            hits = sum(1 for a in afs for b in mine if abs(a - b) * 1000 <= FREQ_TOL_KHZ)
            if not hits:
                continue
            if not same_unit(net, ahdr):
                continue
            score = (hits, hits / len(mine))
            if score > best_score:
                best, best_score = net, score
        if best:
            owned[best].append(nid)

    out: dict[str, tuple[list, list]] = {}
    for net, ids in owned.items():
        out[net] = _pull_register(con, ids)
    return out


def _pull_register(con, ids: list[int]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Roster and legend for one network of this report, from the archived analyst reports.

    The roster comes back in two parts, CORE FIRST: the list as the analyst last wrote it for this
    network, then everyone he listed on it earlier. Aggregating all thirteen archived reports into
    one flat list inflated a net to 15 names where his own last report carried 8 — different epochs
    of the same net stacked on top of each other. The last report is the list a human was actually
    keeping, so it leads; the older names are candidates, kept only if they have been heard lately
    (see build_report).
    """
    core_ids = ids
    if len(ids) > 1:
        marks_all = ",".join("?" * len(ids))
        rid = con.execute(f"SELECT max(report_id) FROM networks WHERE id IN ({marks_all})",
                          ids).fetchone()[0]
        core_ids = [i for (i,) in con.execute(
            f"SELECT id FROM networks WHERE id IN ({marks_all}) AND report_id = ?", [*ids, rid])]

    marks = ",".join("?" * len(ids))

    def pull(sql: str) -> list[tuple[str, str]]:
        # Deduped case-insensitively but printed as written: callsigns are uppercase, code words are
        # not (`«платье»`), so upper-casing the key would mangle half the legend.
        seen: dict[str, tuple[str, str]] = {}
        for key, val in con.execute(sql.format(marks=marks), ids):
            k = str(key or "").strip()
            # A wrapped source line sometimes leaves the description opening on punctuation
            # (`- , «прилетит в ворота» – …`); that is the seam, not content.
            v = " ".join(str(val or "").split()).lstrip(",-–— ").strip()
            if not k:
                continue
            if k.lower() not in seen or len(v) > len(seen[k.lower()][1]):
                seen[k.lower()] = (k, v)
        return list(seen.values())

    def pull_roster(where_ids: list[int]) -> list[tuple[str, str]]:
        # A callsign with no role IS printed. The analyst writes plenty of them — `ЛЕВША`, `ЗАЗА`,
        # `КАЩЕЙ`, `МИХЕЙ` stand alone in his own 21.08 report — and dropping them cost us three of
        # the five names on one net. Knowing a man is on this network is the point; his job is a
        # bonus. (A LEGEND entry with no reading is different and still goes: an unexplained code
        # word tells the reader nothing at all.)
        m = ",".join("?" * len(where_ids))
        seen: dict[str, tuple[str, str]] = {}
        for key, val in con.execute(
                f"SELECT callsign, role FROM roster WHERE network_id IN ({m}) ORDER BY id",
                where_ids):
            k = str(key or "").strip()
            v = " ".join(str(val or "").split()).lstrip(",-–— ").strip()
            # A frequency that slipped into the callsign column during the archive import is not a
            # man: `473.1753` was standing in the register of 1198 мсп as if it were a person.
            # Nothing made of digits, dots and slashes alone is a callsign.
            if not k or len(k) < 2 or re.fullmatch(r"[\d.,/ +-]+", k):
                continue
            if k.lower() not in seen or len(v) > len(seen[k.lower()][1]):
                seen[k.lower()] = (k, v)
        return list(seen.values())

    core = pull_roster(core_ids)
    core_keys = {c.lower() for c, _ in core}
    older = [(c, r) for c, r in pull_roster(ids) if c.lower() not in core_keys]
    legend = pull("SELECT code, meaning FROM legend WHERE network_id IN ({marks}) ORDER BY id")
    legend = [(c, m) for c, m in legend if m]
    known = known_codes()
    legend = [(c, m) for c, m in legend if not (_variants(c) & known)]
    # (callsign, role, is_older) — is_older marks a name the analyst had on this net BEFORE his last
    # report; those are the only ones the activity check may drop.
    return [(c, r, False) for c, r in core] + [(c, r, True) for c, r in older], legend


def archive_records(callsign: str) -> list[tuple[str, str, str, str]]:
    """Every archived line the analyst ever wrote for this callsign: (source, freqs, header, role)."""
    if not REPORTS_DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{REPORTS_DB}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT r.source, n.freqs, n.header, ro.role FROM roster ro "
            "JOIN networks n ON n.id = ro.network_id JOIN reports r ON r.id = n.report_id "
            "WHERE upper(ro.callsign) = ? ORDER BY r.source", (callsign.upper(),)).fetchall()
    except sqlite3.Error:
        return []
    seen, out = set(), []
    for src, fr, hdr, role in rows:
        key = (" ".join(str(hdr or "").split())[:60], " ".join(str(role or "").split())[:60])
        if key in seen:
            continue
        seen.add(key)
        out.append((str(src or ""), "/".join(_freq_str(fr)), key[0], key[1]))
    return out


def _freq_str(raw) -> list[str]:
    try:
        vals = json.loads(raw) if str(raw).strip().startswith("[") else str(raw).split("/")
    except Exception:                                   # noqa: BLE001
        vals = [str(raw)]
    return [str(v).strip() for v in vals if str(v).strip()]


def build_dupes_file(registers: dict, nets: list[str], dt_from: str, dt_to: str) -> str:
    """Callsigns standing in the register of MORE THAN ONE network of this report.

    Deliberately a separate file and NOT part of the report: the same callsign in two formations is
    usually two different men (`ФОКС` is documented as one commander in one net of 38 омсбр and an
    accumulator in another), so merging them would be the worse error — but seeing the repeat with
    no explanation is confusing, and the explanation is always in the analyst's own archive. Some of
    it is HIS uncertainty rather than ours: 411.9630 is signed `2 мсб 38 омсбр` in one report and
    `189 мсп` in another, which alone puts МАРК on two nets.
    """
    where: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for net in nets:
        roster, _ = registers.get(net, ([], []))
        for cs, role, _older in roster:
            where[cs.upper()].append((net, role))
    dupes = {c: v for c, v in where.items() if len(v) > 1}

    out = ["Позивні, що стоять у реєстрі більш ніж однієї мережі цього звіту",
           "довідка для нас, у звіт НЕ йде",
           f"вікно {dt_from} - {dt_to}", ""]
    if not dupes:
        out.append("Таких позивних немає.")
        return "\n".join(out) + "\n"

    for cs in sorted(dupes):
        out.append(f"{cs} - у {len(dupes[cs])} мережах цього звіту")
        for net, role in dupes[cs]:
            out.append(f"    {net}" + (f" - {role}" if role else ""))
        arch = archive_records(cs)
        if arch:
            out.append("  як це записано в архіві аналітика:")
            for src, freqs, hdr, role in arch:
                out.append(f"    {src[:24]:24} {freqs[:30]:30} {hdr}" + (f" | {role}" if role else ""))
        out.append("")
    return "\n".join(out) + "\n"


def register_for(freqs: list[str]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The callsign register and the code legend this network is already known to work with.

    Both come from the analyst's own files, imported by `tools/report_import.py`. From 25.08.2026 he
    stops producing reports and ours is the only one, so this block is no longer a convenience — it
    is the only place the accumulated register survives.

    Matched by FREQUENCY, never by header text: the header is retyped by hand every day and drifts,
    the frequency set is the network's actual identity. Tolerance is the same 5 kHz used to cluster
    the live traffic, because two readings of one channel differ by a couple of kHz.

    Roster is per network by necessity — `ФОКС` is `ком склад` in one network of 38 омсбр and
    `накопичувач` in another, so a global table would merge two different men. Legend is per network
    too, on the owner's correction (2026-08-25): most codes do repeat unchanged, but the same `55`
    has been documented carrying a different meaning in a different network, and inverting a reading
    is the one error here that costs a whole line.
    """
    mine = _freq_set("/".join(freqs)) if freqs else []
    if not mine or not REPORTS_DB.exists():
        return [], []
    try:
        con = sqlite3.connect(f"file:{REPORTS_DB}?mode=ro", uri=True)
        rows = con.execute("SELECT id, freqs FROM networks").fetchall()
    except sqlite3.Error:
        return [], []
    hits = [nid for nid, fr in rows
            if any(abs(a - b) * 1000 <= FREQ_TOL_KHZ for a in _freq_set(fr) for b in mine)]
    if not hits:
        return [], []
    marks = ",".join("?" * len(hits))

    def pull(sql: str) -> list[tuple[str, str]]:
        # Deduped case-insensitively but printed as written: callsigns are uppercase, code words are
        # not (`«платье»`), so upper-casing the key would mangle half the legend.
        seen: dict[str, tuple[str, str]] = {}
        for key, val in con.execute(sql.format(marks=marks), hits):
            k = str(key or "").strip()
            # A wrapped source line sometimes leaves the description opening on punctuation
            # (`- , «прилетит в ворота» – …`); that is the seam, not content.
            v = " ".join(str(val or "").split()).lstrip(",-–— ").strip()
            if not k:
                continue
            # The same name is signed on several days; keep the fullest description we have.
            if k.lower() not in seen or len(v) > len(seen[k.lower()][1]):
                seen[k.lower()] = (k, v)
        return list(seen.values())

    roster = pull("SELECT callsign, role FROM roster WHERE network_id IN ({marks}) ORDER BY id")
    legend = pull("SELECT code, meaning FROM legend WHERE network_id IN ({marks}) ORDER BY id")
    # Only the legend is filtered. A callsign is never "already known" in the same way — the roster
    # is who is on this net, and dropping a name because it is familiar would remove the point of it.
    known = known_codes()
    legend = [(c, m) for c, m in legend if not (_variants(c) & known)]
    return roster, legend


SILENT_LOOKBACK_DAYS = 6      # enough history that a normally-quiet net is not called dead
SILENT_MIN_PRIOR = 10         # below this the channel was never a network, just a few catches


def silent_networks(recs: list[dict], dt_from: str) -> list[tuple[str, int, datetime, str]]:
    """Channels that carried real traffic in the preceding days and gave NOTHING in this window.

    Not an event and not a model product — a deterministic comparison of two windows, so it costs
    one query and no reasoning. A network falling silent is the reader's cue to ask why: the unit
    moved, was destroyed, or changed channel.

    Compared with the SAME 5 kHz tolerance the pipeline clusters with. Without it the list fills up
    with phantoms — `411.9631` reads as silent while `411.9630` is live in the same report, and four
    of the first run's "silent" channels were exactly that.

    The `SILENT_MIN_PRIOR` floor is the other half. 35 of 41 candidates in the first count had been
    heard one to nine times in six days; a channel like that cannot fall silent, it was never
    speaking. Reporting them would bury the six that matter.
    """
    lo = datetime.strptime(dt_from, "%Y-%m-%d %H:%M")
    prev = fetch(f"{lo - timedelta(days=SILENT_LOOKBACK_DAYS):%Y-%m-%d %H:%M}", dt_from)

    live: list[float] = []
    for r in recs:
        if _is_float(r.get("freq")):
            live.append(float(r["freq"]))

    agg: dict[str, dict] = defaultdict(lambda: {"n": 0, "last": None, "heads": defaultdict(int)})
    for r in prev:
        f = r.get("freq")
        if not _is_float(f):
            continue
        e = agg[f]
        e["n"] += 1
        if e["last"] is None or r["_dt"] > e["last"]:
            e["last"] = r["_dt"]
        if r.get("network"):
            e["heads"][r["network"]] += 1

    out = []
    for f, e in agg.items():
        if e["n"] < SILENT_MIN_PRIOR:
            continue
        if any(abs(float(f) - x) * 1000 <= FREQ_TOL_KHZ for x in live):
            continue
        head = max(e["heads"].items(), key=lambda kv: kv[1])[0] if e["heads"] else "(без шапки)"
        out.append((f, e["n"], e["last"], head))
    out.sort(key=lambda t: -t[1])
    return out


def unattended_networks(units: list[dict], events: list[dict]) -> list[tuple]:
    """Networks that DID talk in the window and produced no report line at all.

    The other half of the same question, and the half that actually needs watching. A silent channel
    is usually just a channel that moved. A channel with a day of traffic and nothing selected is
    either genuinely idle chatter — or our rules looking straight past something, and there is no
    way to notice that from the report itself, because absence leaves no trace in it.
    """
    have = {e.get("_net") for e in events}
    by_net: dict[str, list[dict]] = defaultdict(list)
    for u in units:
        by_net[u["net"]].extend(u["items"])
    rows = []
    for net, items in by_net.items():
        if net in have:
            continue
        items.sort(key=lambda r: r["_dt"])
        freqs = sorted({r["freq"] for r in items if r.get("freq")})
        chars = sum(len(s) for r in items for s in r.get("speech", []))
        rows.append((net, len(items), chars, items[0]["_dt"], items[-1]["_dt"], freqs))
    # Ordered by VOLUME OF SPEECH, not by number of intercepts: how much was said and passed over is
    # the thing worth checking. One intercept carrying 1386 characters outranks six carrying 587.
    rows.sort(key=lambda t: -t[2])
    return rows


def build_quiet_file(silent: list[tuple[str, int, datetime, str]], unattended: list[tuple],
                     dt_from: str, dt_to: str) -> str:
    """A SEPARATE file, deliberately not part of the report (owner, 2026-08-25).

    It answers a different question from the report — not what happened, but where nothing did. Two
    sections, because there are two ways for a network to produce nothing and they mean opposite
    things: one has gone off the air, the other is on the air and we wrote nothing about it.

    Both live in ONE file on purpose: otherwise the second half only ever gets looked at when
    somebody remembers to ask for it, and that is exactly what happened for four reports running.
    """
    out = [f"Мовчазні та без уваги - вікно звіту {dt_from} - {dt_to}", "",
           f"=== 1. ЗАМОВКЛИ (працювали попередні {SILENT_LOOKBACK_DAYS} діб, "
           f"мінімум {SILENT_MIN_PRIOR} перехоплень, у вікні - жодного)", ""]
    if not silent:
        out.append("(немає)")
    for f, n, last, head in silent:
        out.append(f"{f} - {head}")
        out.append(f"    перехоплень за {SILENT_LOOKBACK_DAYS} діб: {n}, "
                   f"останній {last:%d.%m.%Y %H:%M}")

    out += ["", "", "=== 2. ПРАЦЮВАЛИ, АЛЕ ЖОДНОЇ ПОДІЇ У ЗВІТІ", ""]
    if not unattended:
        out.append("(немає)")
    for net, n, chars, first, last, freqs in unattended:
        out.append(f"{'/'.join(freqs) if freqs else '(без частоти)'} - {net}")
        out.append(f"    перехоплень: {n}, мовлення: {chars} симв., "
                   f"{first:%d.%m %H:%M} - {last:%d.%m %H:%M}")
    return "\n".join(out) + "\n"


def build_tail() -> list[tuple[str, bool, bool]]:
    """Five blank lines and a marker where the owner pastes his own block.

    The statistics footer we used to print is gone (owner, 2026-08-25): its four counters came from
    another collection and were typed in by hand anyway, so the machine had nothing to contribute
    there. What replaces it is a landing strip — five blanks so the seam is obvious, then `---`,
    which is one easy selection to overwrite with the text he pastes in from elsewhere.
    """
    return [("", False, False)] * 5 + [("---", False, False)]


def build_report(events: list[dict], freqs_of: dict[str, list[str]], band: str,
                 dt_to: str, active: dict[str, dict] | None = None) -> str:
    """Group by network, then by callsign — the analyst's own layout.

    Several events about one man are kept together even when other people's events fall between them
    in time: the first is a normal line, the rest are indented under it. That is much easier to scan,
    and it costs the model nothing because it happens after it has finished.
    """
    by_net: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_net[e.get("_net") or "(без шапки)"].append(e)

    # (text, bold, centered) — the docx layer takes this verbatim; the .txt is the same lines plain.
    styled: list[tuple[str, bool, bool]] = [("Про результати аналізу радіоперехоплень", False, True)]
    if band:
        styled.append((f"у смузі {band}", False, True))
    styled += [(f"на {dt_to[11:16]} {datetime.strptime(dt_to, '%Y-%m-%d %H:%M'):%d.%m.%Y}",
                False, True), ("", False, False)]

    registers = assign_registers(freqs_of)
    for i, net in enumerate(sorted(by_net, key=lambda n: -len(by_net[n]))):
        evs = sorted(by_net[net], key=lambda e: stamp(e.get("time")) or 0)
        if i:
            styled.append(("", False, False))       # two blank lines between network blocks
        fr = freqs_of.get(net) or []
        if fr:
            styled.append(("/".join(fr), True, False))
        styled.append((net, True, False))

        # The register goes between the header and the events, exactly where the analyst keeps it.
        roster, legend = registers.get(net, ([], []))
        # The activity check ADDS, it does not subtract. It used to cut every name unheard for three
        # days out of the whole register, and on this archive that removed 39% of it and left 13
        # networks with no register at all — a net printed with a header and no people, which is
        # worse than a name too many. Now the analyst's last list is printed as he kept it, and only
        # the OLDER names — ones he had dropped from that list himself — have to prove they are
        # still on the air.
        if active:
            names, blob = heard_on(active, fr)
            if names or blob:        # fail open: an empty lookup must not touch the register
                kept, dropped = [], []
                for cs, role, older in roster:
                    tgt = kept if (not older or still_active(cs, names, blob)) else dropped
                    tgt.append((cs, role, older))
                if dropped:
                    print(f"реєстр {net[:34]}: {len(roster)} -> {len(kept)} "
                          f"(зі старіших списків, за {ROSTER_ACTIVE_DAYS} діб не чути: "
                          f"{', '.join(c for c, _, _ in dropped)[:90]})", file=sys.stderr)
                roster = kept
        for cs, role, _older in roster:
            styled.append((f"{cs} - {role}" if role else cs, False, False))
        for code, meaning in legend:
            styled.append((f"«{code}» - {meaning}" if meaning else f"«{code}»", False, False))
        if roster or legend:
            styled.append(("", False, False))

        groups: dict[str, list[dict]] = defaultdict(list)
        order: list[str] = []
        for e in evs:
            k = subject(e) or f"_{len(order)}"
            if k not in groups:
                order.append(k)
            groups[k].append(e)
        for k in order:
            for n, e in enumerate(groups[k]):
                line = f"{e.get('time','')} {e.get('text','')}".strip()
                styled.append((line if n == 0 else f"\t{line}", False, False))
        styled.append(("", False, False))
    return styled


def snapshot() -> str:
    """Freeze the rules behind every run — owner's standing rule, after the configuration of the
    first accepted report turned out to be unrecoverable."""
    files = [RULES, Path(__file__), GLOSSARY]
    h = hashlib.sha256()
    for f in files:
        h.update(f.read_bytes() if f.exists() else b"")
    vid = f"{datetime.now():%Y%m%d}-{h.hexdigest()[:8]}"
    d = VERSIONS / vid
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.exists():
                shutil.copy2(f, d / f.name)
    return vid


def main() -> None:
    ap = argparse.ArgumentParser()
    # `--day 2026-08-20` IS the normal way to call this. "Звіт на 20 серпня" means one fixed thing —
    # 19.08 15:00 → 20.08 15:00 — and computing that by hand every time is an invitation to get the
    # window, the stamp or the filename out of step with each other. Given the day, all three are
    # derived from one number and cannot disagree.
    ap.add_argument("--day", help="report day, YYYY-MM-DD or DD.MM.YYYY: window is the 24 h ending "
                                  "at 15:00 of that day")
    ap.add_argument("--from", dest="dt_from")
    ap.add_argument("--to", dest="dt_to")
    ap.add_argument("--band", default=DEFAULT_BAND)
    ap.add_argument("--out")
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--effort", default="medium")
    # One pass is the standard. Multi-pass union was inherited from v1, where a single call read a
    # 40–80 KB thread and skimmed it, so passes disagreed and adding them up recovered real material
    # (+12 points of coverage there). Measured on v2's per-network units of ~12 KB: one pass and
    # three passes cover EXACTLY the same 22 of the analyst's 47 lines — the extra two passes bought
    # 95 more lines and not one additional finding. Splitting by network cured the cause; the union
    # was treating the symptom. Raise it with --passes if a future change makes calls unreliable
    # again, but do not pay for it by default.
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=6)
    # Everything downstream of the model — the register, the activity filter, the layout, the docx —
    # is pure rendering over `<out>_events.json`. A change there should not cost another 40 minutes
    # of model time, and re-running would also silently produce DIFFERENT events (the model is not
    # deterministic), which makes a rendering change impossible to judge. This replays the saved
    # events instead: same facts, new presentation.
    ap.add_argument("--render-only", action="store_true",
                    help="rebuild the files from an existing <out>_events.json, no model calls")
    # The collector lags 25-30 minutes behind the group, so the last half hour of a window closing at
    # 15:00 has usually not arrived when the report is made. The answer is not a gate that asks
    # whether to proceed — it is to start the window half an hour EARLIER than it says. What the
    # previous report missed off its end, this one picks up off its start. Nothing is lost, the price
    # is that consecutive reports can repeat an event from that overlap, and that is the cheaper
    # error. One constant, applied always, nothing to remember and nothing to decide.
    ap.add_argument("--lead-in", type=int, default=WINDOW_LEAD_IN_MIN,
                    help="minutes to extend the START of the window by (0 disables)")
    a = ap.parse_args()

    if a.day:
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m"):
            try:
                d = datetime.strptime(a.day, fmt)
                if fmt == "%d.%m":
                    d = d.replace(year=datetime.now().year)
                break
            except ValueError:
                d = None
        if d is None:
            sys.exit(f"не розібрав дату: {a.day!r} (треба YYYY-MM-DD або DD.MM.YYYY)")
        a.dt_to = f"{d:%Y-%m-%d} {REPORT_HOUR}"
        a.dt_from = f"{d - timedelta(days=1):%Y-%m-%d} {REPORT_HOUR}"
        a.out = a.out or f"ZVIT_{d:%d.%m}"
    elif not (a.dt_from and a.dt_to and a.out):
        sys.exit("треба або --day, або всі три: --from --to --out")

    print(f"звіт на {a.dt_to}   вікно {a.dt_from} - {a.dt_to}   файл {a.out}", file=sys.stderr)
    print(f"версія правил: {snapshot()}", file=sys.stderr)
    lo = datetime.strptime(a.dt_from, "%Y-%m-%d %H:%M") - timedelta(minutes=a.lead_in)
    read_from = f"{lo:%Y-%m-%d %H:%M}"
    if a.lead_in:
        print(f"вікно {a.dt_from} - {a.dt_to}, читаємо з {read_from} "
              f"(перекриття {a.lead_in} хв на затримку колектора)", file=sys.stderr)
    recs = fetch(read_from, a.dt_to)
    units = units_of_work(recs)
    nets = {u["net"] for u in units}
    print(f"перехватів: {len(recs)}   мереж: {len(nets)}   одиниць роботи: {len(units)}",
          file=sys.stderr)

    freqs_of: dict[str, list[str]] = defaultdict(set)
    for u in units:
        for r in u["items"]:
            if r.get("freq"):
                freqs_of[u["net"]].add(r["freq"])
    freqs_of = {k: sorted(v) for k, v in freqs_of.items()}

    if a.render_only:
        src = OUT_DIR / f"{a.out}_events.json"
        if not src.exists():
            sys.exit(f"немає {src} — нічого перемальовувати")
        events = json.loads(src.read_text())
        age_h = (time.time() - src.stat().st_mtime) / 3600
        print(f"перемальовування з {src.name}: {len(events)} подій, модель не викликається",
              file=sys.stderr)
        # The events are frozen at the moment of the original run; the register, the silent list and
        # the "no events" list are recomputed against the database as it is NOW. Half an hour of drift
        # is nothing. A day of it would put the header and the silence on one date and the events on
        # another, and nothing in the output would say so.
        if age_h > 6:
            print(f"УВАГА: події зібрані {age_h:.0f} год тому, а решта рахується по свіжій базі — "
                  f"для чогось старішого за пів дня краще повний прогон", file=sys.stderr)
        finish(a, recs, units, events, freqs_of)
        return

    rules = RULES.read_text() + "\n\n---\n\n# Глосарій\n\n" + GLOSSARY.read_text()

    def run_one(task):
        p, i, u = task
        body = render_unit(u)
        last = None
        for attempt in range(1, CALL_RETRIES + 1):
            try:
                raw, dt = call_model(rules, "# Матеріал\n\n" + body, a.model, a.effort)
                return p, i, u, extract_json(raw), dt
            except Exception as exc:            # noqa: BLE001
                last = exc
                print(f"прохід {p} · {i}: спроба {attempt} впала: {exc}", file=sys.stderr)
                if attempt < CALL_RETRIES:
                    time.sleep(RETRY_BACKOFF_S * attempt)
        print(f"!!! ВТРАЧЕНО прохід {p} · одиниця {i} ({u['net'][:40]}) — {last}", file=sys.stderr)
        return p, i, u, [], 0.0

    tasks = [(p, i, u) for p in range(1, a.passes + 1) for i, u in enumerate(units)]
    per_pass: list[list[dict]] = [[] for _ in range(a.passes)]
    spent = 0.0
    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        for p, i, u, got, dt in pool.map(run_one, tasks):
            spent += dt
            if got:
                print(f"прохід {p} · {i+1}/{len(units)}: {len(got)} подій за {dt:.0f}s "
                      f"[{u['net'][:38]}]", file=sys.stderr)
            for e in got:
                e["_net"] = u["net"]
                per_pass[p - 1].append(e)

    events = union(per_pass) if a.passes > 1 else per_pass[0]
    if a.passes > 1:
        multi = sum(1 for e in events if e.get("_passes", 1) > 1)
        print(f"проходи: {' + '.join(str(len(x)) for x in per_pass)} → {len(events)} унікальних "
              f"(>1 проходом: {multi}, лише одним: {len(events)-multi})", file=sys.stderr)

    finish(a, recs, units, events, freqs_of, spent)


def finish(a, recs: list[dict], units: list[dict], events: list[dict],
           freqs_of: dict[str, list[str]], spent: float = 0.0) -> None:
    """Everything after the model: register, activity filter, layout, the three files.

    Split out so `--render-only` can reach exactly this and nothing else — the guarantee that a
    presentation change cannot touch the facts is worth more as one shared code path than as a
    promise.
    """
    # Deliberately HERE and not next to the model call: this is the one path both a full run and
    # --render-only go through, so an already-shipped report can be cleaned without spending
    # another 40 minutes of model time.
    events, dups, empty = drop_exact_duplicates(events)
    for e in dups:
        print(f"дубль прибрано: {e.get('time')} {str(e.get('text'))[:70]}", file=sys.stderr)
    for e in empty:
        print(f"порожню подію прибрано: {e.get('time')} [{str(e.get('_net'))[:40]}]", file=sys.stderr)
    if dups or empty:
        print(f"очищення: -{len(dups)} дублів, -{len(empty)} порожніх → {len(events)} подій",
              file=sys.stderr)

    styled = build_report(events, freqs_of, a.band, a.dt_to, activity_index(a.dt_to))
    while styled and not styled[-1][0].strip():     # exactly five blanks, not five plus the ones
        styled.pop()                                # build_report leaves after the last block
    styled += build_tail()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{a.out}.txt"
    out.write_text("\n".join(t for t, _, _ in styled))

    nets_in_report = list(dict.fromkeys(e.get("_net") for e in events if e.get("_net")))
    dupes_txt = build_dupes_file(assign_registers(freqs_of), nets_in_report, a.dt_from, a.dt_to)
    (OUT_DIR / f"{a.out}_dubli.txt").write_text(dupes_txt)
    n_dupes = sum(1 for l in dupes_txt.splitlines() if " - у " in l and "мережах" in l)
    print(f"позивних у кількох мережах: {n_dupes} (довідка у {a.out}_dubli.txt)", file=sys.stderr)

    quiet = silent_networks(recs, a.dt_from)
    idle = unattended_networks(units, events)
    (OUT_DIR / f"{a.out}_silent.txt").write_text(
        build_quiet_file(quiet, idle, a.dt_from, a.dt_to))
    print(f"замовкли: {len(quiet)}   працювали без жодної події: {len(idle)}", file=sys.stderr)
    (OUT_DIR / f"{a.out}_events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2))
    docx = out.with_suffix(".docx")
    try:
        from text_to_docx import write as write_docx
        write_docx(styled, docx, title=None)
    except Exception as e:                      # a failed export must not lose the run
        print(f"docx не вийшов: {e!r}", file=sys.stderr)
        docx = None
    print(f"\nподій: {len(events)}   мереж у звіті: {len({e.get('_net') for e in events})}   "
          f"час моделі: {spent:.0f}s\nзвіт: {out}" + (f"\ndocx: {docx}" if docx else ""),
          file=sys.stderr)


if __name__ == "__main__":
    main()
