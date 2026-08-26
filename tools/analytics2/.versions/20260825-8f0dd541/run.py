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

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "tools"))
from intercept_parse import parse_message  # noqa: E402

RULES = HERE / "rules.md"
GLOSSARY = REPO / "matrix" / "bot" / "prompts" / "refraz" / "10_glossary.md"
OUT_DIR = REPO / "knowledge" / "upstream" / "reports_out"
VERSIONS = HERE / ".versions"

MARGIN_HOURS = 6          # how far past the window to look for late POSTINGS
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
    sql = (f"SELECT id || E'\\x01' || replace(coalesce(text,''), E'\\n', E'\\x02') "
           f"FROM source_messages WHERE group_name='PATAGONIA_GP' "
           f"AND occurred_ts >= '{q_lo:%Y-%m-%d %H:%M}+03' AND occurred_ts < '{q_hi:%Y-%m-%d %H:%M}+03' "
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


def build_tail() -> list[tuple[str, bool, bool]]:
    """Five blank lines and a marker where the owner pastes his own block.

    The statistics footer we used to print is gone (owner, 2026-08-25): its four counters came from
    another collection and were typed in by hand anyway, so the machine had nothing to contribute
    there. What replaces it is a landing strip — five blanks so the seam is obvious, then `---`,
    which is one easy selection to overwrite with the text he pastes in from elsewhere.
    """
    return [("", False, False)] * 5 + [("---", False, False)]


def build_report(events: list[dict], freqs_of: dict[str, list[str]], band: str,
                 dt_to: str) -> str:
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

    for i, net in enumerate(sorted(by_net, key=lambda n: -len(by_net[n]))):
        evs = sorted(by_net[net], key=lambda e: stamp(e.get("time")) or 0)
        if i:
            styled.append(("", False, False))       # two blank lines between network blocks
        fr = freqs_of.get(net) or []
        if fr:
            styled.append(("/".join(fr), True, False))
        styled.append((net, True, False))

        # The register goes between the header and the events, exactly where the analyst keeps it.
        roster, legend = register_for(fr)
        for cs, role in roster:
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
    ap.add_argument("--from", dest="dt_from", required=True)
    ap.add_argument("--to", dest="dt_to", required=True)
    ap.add_argument("--band", default="")
    ap.add_argument("--out", required=True)
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
    a = ap.parse_args()

    print(f"версія правил: {snapshot()}", file=sys.stderr)
    recs = fetch(a.dt_from, a.dt_to)
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

    styled = build_report(events, freqs_of, a.band, a.dt_to)
    while styled and not styled[-1][0].strip():     # exactly five blanks, not five plus the ones
        styled.pop()                                # build_report leaves after the last block
    styled += build_tail()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{a.out}.txt"
    out.write_text("\n".join(t for t, _, _ in styled))
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
