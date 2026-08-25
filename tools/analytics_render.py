#!/usr/bin/env python3
"""Assemble the finished report: title, then per network — frequencies, header, roster, legend, events.

The shape is copied from the analyst's own file (РЕР 21.08.2026), because the product has to be
usable as-is rather than as raw material:

    446.0710/446.0687
    УКХ р/м шг 2 та 4 мср 2 мсб 38 омбр ( БАГАТЕ, ЧАРІВНЕ, НОВОСЕЛІВКА) DMR{1005}
    БОРЕЦ- ком склад
    «55» - прийнято, зрозуміло
    21.08.2026, 06:12 зазначено про використання о\\с підрозділу р\\с «АЗАРТ»

Networks come out in the order of their first event — an empty network is not printed at all.

    python3 tools/analytics_render.py --from '2026-08-20 15:00' --to '2026-08-21 15:00' \\
        --events selected.json [--roster roster.json] [--band 'ЗАЛІЗНИЧНЕ-ЗАГІРНЕ'] --out report
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from analytics_run import fetch, group  # noqa: E402
from text_to_docx import write as write_docx  # noqa: E402

# The `(?)` mark exists for ONE reader: the owner, re-checking a legend reading before the report goes
# out. It appears only on code words, and only on genuinely shaky ones — at 0.6 nearly every line
# carried it and the owner deleted them all by hand. Roster rows never carry it: a role is either
# established well enough to print or it is not printed.
SHAKY = 0.4

KNOWN_CODES = Path(__file__).resolve().parent.parent / "knowledge" / "upstream" / "known_codes.md"


def variants(word: str) -> set[str]:
    """A code word and its spoken variants. `«глаза, глазки»` is ONE entry with two forms."""
    return {v.strip().strip("«»\"'").lower()
            for v in re.split(r"[,/]", word or "") if v.strip().strip("«»\"'")}


def known_codes() -> set[str]:
    """Words the desk already reads without thinking — filtered out of the printed legend.

    Parsed out of the markdown by its `- \\`word\\` — reading` shape, so the list is edited as prose
    and needs no second format. The section that lists deliberate exceptions is skipped.

    Variants are split apart on both sides: the file may hold `глаза, глазки` and a run may return
    `«глаза, глазки»` or just `«глазки»`, and all of those have to match. The first version compared
    whole strings and let every multi-variant reading straight through.
    """
    if not KNOWN_CODES.exists():
        return set()
    out, skip = set(), False
    for line in KNOWN_CODES.read_text().splitlines():
        if line.startswith("## "):
            skip = "NOT here" in line
            continue
        m = re.match(r"\s*-\s*`([^`]+)`", line)
        if m and not skip:
            out |= variants(m.group(1))
    return out


def net_freqs(dt_from: str, dt_to: str) -> dict[str, str]:
    """Network header → its frequencies, joined the way the report joins them."""
    freqs: dict[str, set[str]] = defaultdict(set)
    for th in group(fetch(dt_from, dt_to)):
        head = th["net"]
        if not head:
            continue
        for r in th["items"]:
            if r.get("freq"):
                freqs[head].add(r["freq"])
    return {h: "/".join(sorted(f)) for h, f in freqs.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dt_from", required=True)
    ap.add_argument("--to", dest="dt_to", required=True)
    ap.add_argument("--events", required=True)
    ap.add_argument("--roster")
    ap.add_argument("--band", default="")
    ap.add_argument("--out", required=True, help="path stem; .txt and .docx are written")
    a = ap.parse_args()

    events = json.loads(Path(a.events).read_text())
    roster = json.loads(Path(a.roster).read_text()) if a.roster else {}
    freqs = net_freqs(a.dt_from, a.dt_to)

    by_net: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_net[e.get("_net") or "(без шапки)"].append(e)
    for evs in by_net.values():
        evs.sort(key=lambda e: str(e.get("time", "")))

    # A callsign with no established function is rank-and-file: correct analysis, useless in the
    # report, and the owner deletes it by hand. Dropped here rather than trusted to the prompt.
    # A row with no role at all is rank-and-file and is dropped. A role the model is UNSURE of is
    # kept and marked — the analyst does the same (`МАРК – накопичувач(?)`), because a suspected
    # накопичувач is worth flagging for re-check and worthless if silently withheld.
    roles: dict[str, list[str]] = defaultdict(list)
    dropped_plain = 0
    for r in roster.get("callsign_roles", []):
        if not (r.get("role") or "").strip():
            dropped_plain += 1
            continue
        mark = "" if float(r.get("confidence", 1)) >= SHAKY else "(?)"
        roles[r.get("network", "")].append(f"{r['name']}- {r['role']}{mark}")

    known, dropped_known = known_codes(), 0
    codes: dict[str, list[str]] = defaultdict(list)
    for c in roster.get("codes", []):
        if variants(c.get("word", "")) & known:
            dropped_known += 1
            continue
        mark = "" if float(c.get("confidence", 1)) >= SHAKY else "(?)"
        codes[c.get("network", "")].append(f"«{c['word']}» - {c['reading']}{mark}")
    print(f"реєстр: відкинуто рядового складу {dropped_plain}; "
          f"легенда: відкинуто вже відомих кодів {dropped_known}", file=sys.stderr)

    head_date = a.dt_to[8:10] + "." + a.dt_to[5:7] + "." + a.dt_to[:4]
    out: list[str] = ["Про результати аналізу радіоперехоплень"]
    if a.band:
        out.append(f"у смузі {a.band}")
    out += [f"на {a.dt_to[11:16]} {head_date}", ""]

    for net, evs in sorted(by_net.items(), key=lambda kv: str(kv[1][0].get("time", ""))):
        if freqs.get(net):
            out.append(freqs[net])
        out.append(net)
        # A roster key is whatever pass C echoed back; match loosely so a reworded header still lands.
        for key in (net, next((k for k in roles if k and k in net), "")):
            if key in roles:
                out += roles.pop(key)
                break
        for key in (net, next((k for k in codes if k and k in net), "")):
            if key in codes:
                out += codes.pop(key)
                break
        for e in evs:
            out.append(f"{e.get('time','')} {e.get('text','')}")
        out.append("")

    text = "\n".join(out)
    # Not with_suffix: a stem like «ZVIT_21.08.2026» would lose its «.2026» to it.
    stem = Path(a.out)
    txt = stem.with_name(stem.name + ".txt")
    txt.write_text(text)
    docx = stem.with_name(stem.name + ".docx")
    write_docx(text, docx)
    print(f"мереж: {len(by_net)}   подій: {len(events)}   рядків: {len(out)}", file=sys.stderr)
    print(f"{txt}\n{docx}", file=sys.stderr)


if __name__ == "__main__":
    main()
