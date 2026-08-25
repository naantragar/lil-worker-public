#!/usr/bin/env python3
"""Cross-day memory: what was already known about these networks before today.

Eight of the 23 event lines in a real report begin with «продовження переміщення» — the analyst is
writing against yesterday's report, not against a blank page. A pipeline that starts each day from
zero cannot produce that word, and more importantly cannot notice that a movement it sees the
beginning of today is the continuation of one already reported.

The substrate already exists: `tools/report_import.py` parses finished reports into
`knowledge/upstream/reports.db` (networks · roster · legend · events). This module is the read side —
given the networks of the window being processed, it finds the same networks in earlier reports and
renders what was known about them.

Networks are matched BY FREQUENCY, not by header text: the header is retyped by hand every day, the
frequency set is the network's actual identity (see `cluster_by_freq` in analytics_run).

    python3 tools/analytics_prior.py --from '2026-08-20 15:00' --to '2026-08-21 15:00'

Status: built, not yet wired into the run. Wiring it needs the day's own output imported back into
reports.db first, so that "yesterday" exists for a day we produced ourselves.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from analytics_run import fetch, group  # noqa: E402

DB = HERE.parent / "knowledge" / "upstream" / "reports.db"
RECENT_EVENTS = 6           # enough to see what was in progress, not enough to rewrite the day


def freqs_by_net(dt_from: str, dt_to: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for th in group(fetch(dt_from, dt_to)):
        for r in th["items"]:
            if r.get("freq"):
                out[th["net"]].add(r["freq"])
    return out


def parse_freqs(raw: str | None) -> set[str]:
    """report_import stores a JSON array; a hand-written block may use the report's own `a/b/c`."""
    if not raw:
        return set()
    raw = raw.strip()
    if raw.startswith("["):
        try:
            return {str(f).strip() for f in json.loads(raw) if str(f).strip()}
        except json.JSONDecodeError:
            pass
    return {f.strip() for f in raw.split("/") if f.strip()}


def prior_for(freqs: set[str], db: Path = DB) -> dict:
    """Everything earlier reports hold about a network working any of these frequencies."""
    if not freqs or not db.exists():
        return {}
    con = sqlite3.connect(db)
    hit_ids, headers = set(), []
    for nid, fr, header in con.execute("SELECT id, freqs, header FROM networks"):
        if freqs & parse_freqs(fr):
            hit_ids.add(nid)
            headers.append(header)
    if not hit_ids:
        return {}
    marks = ",".join("?" * len(hit_ids))
    ids = list(hit_ids)
    roster = con.execute(
        f"SELECT DISTINCT callsign, role FROM roster WHERE network_id IN ({marks}) "
        "ORDER BY callsign", ids).fetchall()
    legend = con.execute(
        f"SELECT DISTINCT code, meaning FROM legend WHERE network_id IN ({marks}) "
        "ORDER BY code", ids).fetchall()
    events = con.execute(
        f"SELECT date, time, text FROM events WHERE network_id IN ({marks}) "
        "ORDER BY ts DESC LIMIT ?", ids + [RECENT_EVENTS]).fetchall()
    return {"headers": headers, "roster": roster, "legend": legend,
            "events": list(reversed(events))}


def render(net: str, prior: dict) -> str:
    """The block appended to a batch's material — deliberately terse, it is context, not the task."""
    if not prior:
        return ""
    out = [f"### Раніше відомо про мережу: {net}"]
    if prior["roster"]:
        out.append("Позивні з попередніх звітів:")
        out += [f"  {c}- {r}" if r else f"  {c}" for c, r in prior["roster"]]
    if prior["legend"]:
        out.append("Легенда з попередніх звітів:")
        out += [f"  «{c}» - {m}" for c, m in prior["legend"]]
    if prior["events"]:
        out.append("Останні відомі події (для «продовження …», не переписувати):")
        out += [f"  {d}, {t} {x}" for d, t, x in prior["events"]]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dt_from", required=True)
    ap.add_argument("--to", dest="dt_to", required=True)
    ap.add_argument("--db", default=str(DB))
    a = ap.parse_args()

    nets = freqs_by_net(a.dt_from, a.dt_to)
    found = 0
    for net, fs in nets.items():
        block = render(net, prior_for(fs, Path(a.db)))
        if block:
            found += 1
            print(block + "\n")
    print(f"мереж у вікні: {len(nets)}   з них знайдено в попередніх звітах: {found}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
