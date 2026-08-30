#!/usr/bin/env python3
"""Prototype of the intercept-analytics pipeline: a window of raw messages → report lines.

Runs OUTSIDE upstream on purpose. The point is to see the quality of the analysis before any schema,
job runner or UI is built around it — if the model cannot write usable lines from a real day, that
is much cheaper to learn now.

    python3 tools/analytics_run.py --from '2026-07-11 12:15' --to '2026-07-12 15:00' \
        [--limit-threads N] [--model claude-sonnet-5] [--out report.txt]

Pipeline: pull → parse (tools/intercept_parse.py) → group by network → split into threads on a time
gap → pack whole threads into batches → one model call per batch → collect events → render.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from intercept_parse import parse_message  # noqa: E402
sys.path.insert(0, str(HERE / "analytics2"))
# The corpus door lives in one file for every analytics tool — v1 included, so a source change can
# never leave the old pipeline reading somewhere else.
from run import corpus_rows as _corpus_rows, _ms  # noqa: E402

REPO = HERE.parent
# Results live in the repo, not /tmp: a run costs real model time and must survive a reboot or a
# lost session.
OUT_DIR = REPO / "knowledge" / "upstream" / "reports_out"
GLOSSARY = REPO / "matrix" / "bot" / "prompts" / "refraz" / "10_glossary.md"
TASK = HERE / "analytics_prompt.md"
SELECT_TASK = HERE / "analytics_select_prompt.md"

VERSIONS = HERE / ".analytics_versions"


def snapshot_rules() -> str:
    """Freeze the rule files that drive this run and return the version id.

    Owner's standing rule (2026-08-24): every state of the prompt system must be reachable later, by
    version and by date. It was learned the hard way — the configuration that produced the first
    accepted report could not be restored, because the prompts were edited in place and the whole
    analytics tree was never committed.

    The id is date + a hash of the rule files' CONTENT, so an unchanged pipeline reuses its directory
    instead of littering one per run, and any output can be traced to the exact rules behind it.
    """
    import hashlib
    import shutil

    files = [TASK, SELECT_TASK, HERE / "analytics_sense_prompt.md", Path(__file__), GLOSSARY]
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


GAP_MINUTES = 90          # a silence longer than this starts a new thread
BATCH_CHARS = 40000       # ≈ 10k tokens of thread text per call, plus the rules
CALL_RETRIES = 3          # a transient CLI failure must not abort a nine-minute extraction
RETRY_BACKOFF_S = 20      # multiplied by the attempt number


MARGIN_HOURS = 6          # how far outside the window to look for late/early POSTINGS


def _own_dt(rec: dict) -> datetime | None:
    """The moment the intercept itself was heard, taken from its own header."""
    try:
        fmt = "%d.%m.%Y %H:%M:%S" if rec["time"].count(":") == 2 else "%d.%m.%Y %H:%M"
        return datetime.strptime(f"{rec['date']} {rec['time']}", fmt)
    except (KeyError, ValueError):
        return None


def fetch(dt_from: str, dt_to: str, by: str = "intercept") -> list[dict]:
    """Pull a window of intercepts.

    `by='intercept'` (the default) means the window is about WHEN THE EVENT HAPPENED, not when
    somebody got round to posting it: an exchange heard at 14:57 may land in WhatsApp at 16:00, and
    a report «на 15:00» must contain it. So the SQL reaches MARGIN_HOURS beyond the window on both
    sides and the real cut is made on each intercept's own timestamp after parsing.

    `by='publish'` keeps the old behaviour — the posting time — which is only useful for reproducing
    an earlier run.
    """
    lo = datetime.strptime(dt_from, "%Y-%m-%d %H:%M")
    hi = datetime.strptime(dt_to, "%Y-%m-%d %H:%M")
    q_lo, q_hi = (lo - timedelta(hours=MARGIN_HOURS), hi + timedelta(hours=MARGIN_HOURS)) \
        if by == "intercept" else (lo, hi)

    # Same corpus as v2 and for the same reason: the collector's own file, not a database a third
    # party owns. See the note above `corpus_rows` in analytics2/run.py. (This also drops the old
    # hardcoded `+03`, which was wrong for half the year.)
    out, undated = [], 0
    for mid, body in _corpus_rows("timestamp >= ? AND timestamp < ?", (_ms(q_lo), _ms(q_hi))):
        for rec in parse_message(body):
            rec["msg_id"] = mid
            rec["_dt"] = _own_dt(rec)
            if by == "intercept":
                # An intercept whose own header has no readable time cannot be placed; it is kept
                # only if it was POSTED inside the nominal window, so nothing is silently lost.
                if rec["_dt"] is None:
                    undated += 1
                    continue
                if not (lo <= rec["_dt"] < hi):
                    continue
            out.append(rec)
    if undated:
        print(f"без розбірливого часу в шапці, пропущено: {undated}", file=sys.stderr)
    return out


def norm_net(header: str | None) -> str:
    """Network identity: the header, whitespace- and case-normalised. NOT the frequency."""
    if not header:
        return "(без шапки)"
    h = re.sub(r"\s+", " ", header).strip().rstrip(".").lower()
    h = re.sub(r"\bйм\.?|\bім\.?", "", h)          # "ймовірно" is not part of the identity
    return re.sub(r"\s+", " ", h).strip()


FREQ_TOL_KHZ = 5.0        # channel spacing here is 12.5 kHz; anything closer is one channel


def _freq_buckets(freqs: set[str]) -> dict[str, str]:
    """Map each frequency string onto a canonical one, merging near-identical readings.

    The frequency in a header is typed by hand from what the operator saw, so the SAME channel
    arrives as `411.9630` and `411.9650` — 2 kHz apart, far below the 12.5 kHz spacing. Compared as
    strings they never match, so the network split in two and was reported twice (17 and 8 events on
    22.08 for one network the analyst heads with a single frequency).

    Each bucket is anchored on its FIRST member, not on the previous one, so a long ladder of
    near-misses cannot chain a bucket wider than the tolerance.
    """
    vals = []
    for f in freqs:
        try:
            vals.append((float(f), f))
        except (TypeError, ValueError):
            continue
    vals.sort()
    out: dict[str, str] = {}
    head: tuple[float, str] | None = None
    for v, s in vals:
        if head is None or (v - head[0]) * 1000 > FREQ_TOL_KHZ:
            head = (v, s)
        out[s] = head[1]
    return out


def cluster_by_freq(recs: list[dict]) -> dict[str, str]:
    """Merge headers that share a frequency into one network, and name the result.

    In the analyst's own report a network IS a set of frequencies — one block is headed
    `416.1900/416.1921/416.1950/416.2699/423.4748` above a single «шапка». Our headers are typed by
    hand by whoever posted the intercept, so the same network arrives as «… (р-н ЧАРІВНЕ) DMR» and
    «… (р-н ЧАРІВНЕ)» and gets reported twice. Grouping by shared frequency reproduces the analyst's
    model and collapsed 92 headers to 52 on a real day.

    The risk is the mirror image: if two genuinely different networks ever work one frequency they
    merge here. That is the same assumption the analyst makes when signing a network, so we take it.
    """
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
    owner: dict[str, str] = {}                  # frequency → some header already holding it
    for h, fs in freqs_of.items():
        for f in fs:
            f = bucket.get(f, f)                # near-identical readings share one bucket
            if f in owner:
                a, b = find(h), find(owner[f])
                if a != b:
                    parent[a] = b
            else:
                owner[f] = h

    members: dict[str, list[str]] = defaultdict(list)
    for h in seen:
        members[find(h)].append(h)
    # The canonical name is the header actually used most — the typo variants lose. It is mapped back
    # to its ORIGINAL casing, because norm_net lowercases and the report must read like the corpus.
    out: dict[str, str] = {}
    for ms in members.values():
        top = max(ms, key=lambda x: (seen[x], len(x)))
        name = max(raw_of[top].items(), key=lambda kv: kv[1])[0]
        for h in ms:
            out[h] = name
    return out


def group(records: list[dict]) -> list[dict]:
    canon = cluster_by_freq([r for r in records if r.get("speech")])
    by_net: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if not r.get("speech"):
            continue
        if r.get("_dt") is None:                # fetch(by='publish') leaves these unset
            r["_dt"] = _own_dt(r)
        if r["_dt"] is None:
            continue
        key = norm_net(r.get("network"))
        by_net[canon.get(key, key)].append(r)

    threads = []
    for net, items in by_net.items():
        items.sort(key=lambda x: x["_dt"])
        cur: list[dict] = []
        for r in items:
            if cur and (r["_dt"] - cur[-1]["_dt"]).total_seconds() > GAP_MINUTES * 60:
                threads.append({"net": net, "items": cur}); cur = []
            cur.append(r)
        if cur:
            threads.append({"net": net, "items": cur})
    threads.sort(key=lambda t: t["items"][0]["_dt"])
    return threads


# The cap is applied to an ESTIMATE of the rendered size (speech + a header line per intercept),
# which runs a little under the real thing, so it is set below the 20k target the yield table
# points at — measured, a 16k budget renders to at most ~19k.
MAX_THREAD_CHARS = 16000  # above this a thread is windowed — see split_threads
OVERLAP_ITEMS = 3         # intercepts repeated at a seam so a straddling event stays visible


def split_threads(threads: list[dict]) -> list[dict]:
    """Window any thread too big to be read attentively in one call.

    A thread is never split across calls by design — the model must see a whole conversation. But a
    busy network can talk for hours inside the 90-minute gap rule, and on 22.08 four threads ran to
    34–82 KB and held HALF the day's material. Measured yield falls with size:

        thread size    events per 10k chars
        0–5k                   6.25
        5–20k                  5.91
        20–40k                 4.50
        over 40k               3.85

    The model does not fail on a big thread, it SKIMS it — and the 38% shortfall on the largest
    threads is roughly sixty events lost on one day, which is exactly the recall we are missing.

    Windows carry OVERLAP_ITEMS intercepts across each seam, so an event whose fragments sit either
    side of a cut is still visible whole in one of them. Duplicate readings of the same event are
    what the union and the selection pass already handle.
    """
    out: list[dict] = []
    for th in threads:
        if len(render_thread(0, th)) <= MAX_THREAD_CHARS:
            out.append(th)
            continue

        def weight(r: dict) -> int:
            return sum(len(s) for s in r["speech"]) + 80      # + the header line per intercept

        parts, cur, size = [], [], 0
        for r in th["items"]:
            if cur and size + weight(r) > MAX_THREAD_CHARS:
                parts.append(cur)
                cur = cur[-OVERLAP_ITEMS:]
                size = sum(weight(x) for x in cur)
            cur.append(r)
            size += weight(r)
        if cur:
            parts.append(cur)
        for k, p in enumerate(parts, 1):
            out.append({"net": th["net"], "items": p, "part": (k, len(parts))})
    out.sort(key=lambda t: t["items"][0]["_dt"])
    return out


def render_thread(idx: int, th: dict) -> str:
    head = th["net"] or "(мережа без шапки)"
    freqs = sorted({r["freq"] for r in th["items"] if r["freq"]})
    part = th.get("part")
    if part:
        head += f"  [частина {part[0]}/{part[1]} довгої розмови]"
    lines = [f"### THREAD {idx} — {head}", f"частоти: {', '.join(freqs)}"]
    for n, r in enumerate(th["items"]):
        who = " / ".join(r["stations"]) if r["stations"] else "НВ"
        # HH:MM, not HH:MM:SS — the model copies this string into the report, and the report has
        # never carried seconds.
        lines.append(f"[{n}] {r['date']}, {r['time'][:5]} — {who}")
        if r.get("comment_above"):
            lines.append(f"    (позначка аналітика: {r['comment_above']})")
        if r.get("comment_below"):
            lines.append(f"    (позначка аналітика: {r['comment_below']})")
        lines += [f"    {s}" for s in r["speech"]]
    return "\n".join(lines)


def batches(threads: list[dict]) -> list[list[tuple[int, dict]]]:
    """Whole threads only — a thread is never split across calls."""
    out, cur, size = [], [], 0
    for i, th in enumerate(threads):
        body = render_thread(i, th)
        if cur and size + len(body) > BATCH_CHARS:
            out.append(cur); cur, size = [], 0
        cur.append((i, th)); size += len(body)
    if cur:
        out.append(cur)
    return out


# A scratch directory with no CLAUDE.md and no .claude/ — the CLI loads the persona, the skills index
# and the memory paths from the working tree, and none of that belongs in an extraction call.
BARE_CWD = Path("/tmp/upstream_analytics_cwd")

NO_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebFetch", "WebSearch",
            "Task", "Agent", "Workflow", "Skill", "NotebookEdit"]


def call_model(system: str, material: str, model: str, effort: str | None = None) -> tuple[str, float]:
    """One bare model call: our rules as the SYSTEM prompt, the material on stdin.

    Everything the agent harness would otherwise attach is stripped, because it is pure overhead
    here and it is paid on every batch:
      --system-prompt      replaces the agent's own system prompt instead of appending to it
      --mcp-config {} + --strict-mcp-config   no MCP servers (their schemas cost context)
      --disallowedTools    no tool schemas; this task needs no tools at all
      cwd=BARE_CWD         no CLAUDE.md, no skills, no memory paths
    The prompt itself goes on STDIN — as argv it dies with "Argument list too long" on any real batch.
    """
    BARE_CWD.mkdir(parents=True, exist_ok=True)
    cmd = ["claude", "-p", "--model", model,
           "--system-prompt", system,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--disallowedTools", *NO_TOOLS]
    if effort:
        cmd += ["--effort", effort]
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


UNION_TOL_MIN = 20        # the same event lands minutes apart in two passes (06:04 vs 06:07)

_NAME = re.compile(r"[А-ЯЁЇІЄҐ][А-ЯЁЇІЄҐ\-]{2,}")
_NAME_STOP = {"ВУ", "СОУ", "РОВ", "БПЛА", "МТЗ", "ФПВ", "ДРГ", "БК", "ДМР", "УКХ"}


def _names(text: str | None) -> set[str]:
    return {n for n in _NAME.findall(str(text or "")) if n not in _NAME_STOP}


def _stamp(s: str | None) -> int | None:
    """`21.08.2026, 07:11` → minutes, date included so midnight is not a 24-hour jump."""
    s = str(s or "")
    hm = re.search(r"(\d{1,2}):(\d{2})", s)
    if not hm:
        return None
    out = int(hm.group(1)) * 60 + int(hm.group(2))
    d = re.search(r"(\d{2})\.(\d{2})", s)
    if d:
        out += (int(d.group(1)) + int(d.group(2)) * 31) * 1440
    return out


def union_passes(passes: list[list[dict]]) -> list[dict]:
    """Merge several extraction passes over the SAME material — union, not vote.

    Measured on three identical runs over one network: 18, 25 and 25 events, with only ~75% of them
    common to any two runs. What floats is not noise — a 300, a fire impact, a planned movement and an
    orientir fix were each seen by exactly one run of the three. So a single pass silently loses about
    a quarter of the facts, and the fix is to run it more than once and add the results up. Voting
    belongs to interpretation (roles, code words), never here: dropping a fact because only one pass
    noticed it would throw away exactly the material this exists to recover.

    Matching is by network + a shared callsign + a ±20 min window, because the same arrival is timed
    06:04 by one pass and 06:07 by another; comparing exact timestamps gives a false 13% overlap.
    An accumulated event can absorb at most ONE event per pass, so two genuinely distinct events that
    a single pass wrote minutes apart are never collapsed into each other.
    """
    acc: list[dict] = []
    for e in passes[0] if passes else []:
        e["_passes"] = 1
        acc.append(e)
    for evs in passes[1:]:
        taken: set[int] = set()
        for e in evs:
            t, ns = _stamp(e.get("time")), _names(e.get("text"))
            best, best_d = None, None
            for i, o in enumerate(acc):
                if i in taken or (o.get("_net") or "") != (e.get("_net") or ""):
                    continue
                ot = _stamp(o.get("time"))
                if t is None or ot is None:
                    continue
                d = abs(t - ot)
                if d > UNION_TOL_MIN or not (ns & _names(o.get("text"))):
                    continue
                if best_d is None or d < best_d:
                    best, best_d = i, d
            if best is None:
                e["_passes"] = 1
                acc.append(e)
            else:
                taken.add(best)
                acc[best]["_passes"] = acc[best].get("_passes", 1) + 1
                # The fuller wording survives into selection; the editor pass tightens it anyway.
                if len(str(e.get("text", ""))) > len(str(acc[best].get("text", ""))):
                    acc[best]["text"] = e["text"]
    return acc


def select(events: list[dict], model: str, effort: str, jobs: int) -> list[dict]:
    """Pass A2 — precision after recall.

    Extraction sees one batch of threads and cannot know how much the day as a whole is producing;
    measured against a real analyst's report it over-produced tenfold, mostly by writing a callsign's
    ROLE as if it were an event. This pass gets a whole network's candidates at once, so it can
    compare them, drop the roster material, merge duplicates and hold the day to the real rate.
    """
    rules = SELECT_TASK.read_text()
    by_net: dict[str, list[dict]] = defaultdict(list)
    for n, e in enumerate(events):
        e["id"] = n
        by_net[e.get("_net") or "(без шапки)"].append(e)

    # Batch by network — a network is never split, or the pass loses exactly the comparison it exists
    # for. Several small networks share one call.
    packs, cur, size = [], [], 0
    for net, evs in by_net.items():
        body = f"## Мережа: {net}\n" + "\n".join(
            f"[{e['id']}] {e.get('time','')} {e.get('text','')}"
            + (f"  (проходів: {e['_passes']})" if e.get("_passes") else "")
            for e in evs)
        if cur and size + len(body) > 20000:
            packs.append(cur); cur, size = [], 0
        cur.append(body); size += len(body)
    if cur:
        packs.append(cur)

    kept: list[dict] = []
    by_id = {e["id"]: e for e in events}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for n, got in pool.map(lambda p: (p[0], extract_json(
                call_model(rules, "# Кандидати\n\n" + "\n\n".join(p[1]), model, effort)[0])),
                list(enumerate(packs, 1))):
            print(f"відбір {n}/{len(packs)}: залишено {len(got)}", file=sys.stderr)
            for k in got:
                src = by_id.get(k.get("id"))
                if src is None:                 # a hallucinated id is dropped, not guessed at
                    continue
                out = dict(src)
                out.update({kk: vv for kk, vv in k.items() if kk in
                            ("text", "time", "confidence", "keep_reason", "merged")})
                kept.append(out)
    return kept


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dt_from", required=True)
    ap.add_argument("--to", dest="dt_to", required=True)
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--limit-threads", type=int)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--passes", type=int, default=3,
                    help="extraction runs over the same material, unioned (recall; see union_passes)")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--select-model", default="claude-opus-5")
    ap.add_argument("--select-effort", default="high")
    ap.add_argument("--no-select", action="store_true", help="skip the precision pass (raw recall)")
    ap.add_argument("--no-split", action="store_true",
                    help="do not window oversized threads — reproduces the pre-2026-08-24 behaviour")
    ap.add_argument("--from-candidates", metavar="FILE",
                    help="skip extraction and select from a previous run's <stem>_candidates.json — "
                         "tuning the selection prompt costs seconds instead of a full extraction")
    ap.add_argument("--out")
    a = ap.parse_args()

    version = snapshot_rules()
    print(f"версія правил: {version}   (tools/.analytics_versions/{version})", file=sys.stderr)

    # Selection-only mode. Extraction is the expensive half (three passes over a day is ~12 minutes
    # of model time) and it does not change while the SELECTION rules are being tuned, so iterating
    # on the editor pass must not pay for it again. This also makes the two stages separately
    # measurable: candidates → coverage of recall, kept → coverage after precision.
    if a.from_candidates:
        cands = json.loads(Path(a.from_candidates).read_text())
        print(f"кандидатів з файлу: {len(cands)}", file=sys.stderr)
        kept = select(cands, a.select_model, a.select_effort, a.jobs)
        print(f"відбір: {len(cands)} → {len(kept)} подій", file=sys.stderr)
        write_out(kept, a, spent=0.0, candidates=cands)
        return

    recs = fetch(a.dt_from, a.dt_to)
    threads = group(recs)
    raw_n = len(threads)
    if not a.no_split:
        threads = split_threads(threads)
    print(f"перехватів: {len(recs)}   мереж: {len({t['net'] for t in threads})}   "
          f"ниток: {raw_n}" + (f" → {len(threads)} після нарізки довгих" if len(threads) != raw_n
                               else ""), file=sys.stderr)
    if a.limit_threads:
        threads = threads[: a.limit_threads]
        print(f"обмежено до {len(threads)} ниток", file=sys.stderr)

    rules = TASK.read_text() + "\n\n---\n\n# Глосарій\n\n" + GLOSSARY.read_text()
    events, spent = [], 0.0
    packs = batches(threads)

    def run_pack(task):
        """One batch of one pass, with retries — a single failed call must not lose the run.

        A day is ~13 batches × 3 passes = ~39 concurrent-ish calls, and the CLI occasionally exits 1
        with empty stderr (transient overload). ThreadPoolExecutor.map re-raises, so one such blip
        used to abort the whole extraction after nine minutes of work. Now the call is retried, and a
        batch that still fails is reported LOUDLY and skipped rather than taking everything with it.
        """
        p, n, pack = task
        body = "\n\n".join(render_thread(i, th) for i, th in pack)
        last = None
        for attempt in range(1, CALL_RETRIES + 1):
            try:
                raw, dt = call_model(rules, "# Матеріал\n\n" + body, a.model, a.effort)
                return p, n, pack, body, extract_json(raw), dt
            except Exception as exc:             # noqa: BLE001 — any failure is worth one more try
                last = exc
                print(f"прохід {p} · батч {n}: спроба {attempt}/{CALL_RETRIES} впала: {exc}",
                      file=sys.stderr)
                if attempt < CALL_RETRIES:
                    time.sleep(RETRY_BACKOFF_S * attempt)
        print(f"!!! ВТРАЧЕНО: прохід {p} · батч {n} ({len(pack)} ниток, {len(body)} символів) — "
              f"{last}", file=sys.stderr)
        return p, n, pack, body, [], 0.0

    # Every batch of every pass is one independent call, so they all go into one pool: three passes
    # cost roughly one pass of wall clock plus queueing, not three times as long.
    tasks = [(p, n, pack) for p in range(1, a.passes + 1) for n, pack in enumerate(packs, 1)]
    per_pass: list[list[dict]] = [[] for _ in range(a.passes)]
    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        for p, n, pack, body, got, dt in pool.map(run_pack, tasks):
            spent += dt
            print(f"прохід {p} · батч {n}/{len(packs)}: ниток={len(pack)} символів={len(body)} "
                  f"→ подій={len(got)} за {dt:.0f}s", file=sys.stderr)
            for e in got:
                ti = e.get("thread")
                e["_net"] = threads[ti]["net"] if isinstance(ti, int) and ti < len(threads) else None
                per_pass[p - 1].append(e)

    if a.passes > 1:
        events = union_passes(per_pass)
        got = " + ".join(str(len(x)) for x in per_pass)
        both = sum(1 for e in events if e.get("_passes", 1) > 1)
        print(f"проходи: {got} → об'єднано {len(events)} унікальних "
              f"(бачені >1 проходом: {both}, лише одним: {len(events)-both})", file=sys.stderr)
    else:
        events = per_pass[0]

    candidates = [dict(e) for e in events]      # frozen BEFORE selection, so the two stages can be
                                                # measured apart and selection re-run without paying
                                                # for extraction again
    if not a.no_select:
        raw_n = len(events)
        events = select(events, a.select_model, a.select_effort, a.jobs)
        print(f"відбір: {raw_n} → {len(events)} подій", file=sys.stderr)

    write_out(events, a, spent, candidates)


def write_out(events: list[dict], a, spent: float, candidates: list[dict] | None = None) -> None:
    events.sort(key=lambda e: str(e.get("time", "")))
    by_net = defaultdict(list)
    for e in events:
        by_net[e.get("_net") or "(без шапки)"].append(e)

    # Two renderings on purpose. The clean one is the working product — no confidence marks, no
    # notes, nothing to strip by hand before it goes into a report. The annotated one is for us,
    # while we are still tuning: it shows what the model was unsure about.
    clean, marked = [], []
    for net, evs in by_net.items():
        clean.append(net); marked.append(net)
        for e in evs:
            line = f"{e.get('time','')} {e.get('text','')}"
            clean.append(line)
            conf = float(e.get("confidence", 1))
            marked.append(line + (f"   [{conf:.1f}]" if conf < 0.7 else ""))
            if e.get("note"):
                marked.append(f"    ({e['note']})")
        clean.append(""); marked.append("")
    report = "\n".join(clean)

    print(f"\nвсього подій: {len(events)}   мереж у звіті: {len(by_net)}   "
          f"час моделі: {spent:.0f}s", file=sys.stderr)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = a.out or f"report_{a.dt_to[:10].replace('-', '')}"
    out = Path(stem) if "/" in stem else OUT_DIR / (stem + ".txt")
    out.write_text(report)
    notes = out.with_name(out.stem + "_marked.txt")
    notes.write_text("\n".join(marked))
    raw_dump = out.with_name(out.stem + "_events.json")
    raw_dump.write_text(json.dumps(events, ensure_ascii=False, indent=2))
    cand_dump = None
    if candidates is not None:
        cand_dump = out.with_name(out.stem + "_candidates.json")
        cand_dump.write_text(json.dumps(candidates, ensure_ascii=False, indent=2))

    docx = out.with_suffix(".docx")
    try:
        from text_to_docx import write as write_docx
        write_docx(report, docx, title=f"Аналітика радіоперехоплень {a.dt_from} — {a.dt_to}")
    except Exception as e:                      # a failed export must not lose the run
        print(f"docx не вийшов: {e!r}", file=sys.stderr)
        docx = None
    print(f"звіт:          {out}\nз позначками:  {notes}\nсирі події:    {raw_dump}"
          + (f"\nкандидати:     {cand_dump}" if cand_dump else "")
          + (f"\ndocx:          {docx}" if docx else ""), file=sys.stderr)


if __name__ == "__main__":
    main()
