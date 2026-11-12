#!/usr/bin/env python3
"""Battalions split by frequency — the frequencies follow the PEOPLE, never the other way round.

    python3 tools/upstream/battalion_freqs.py [--days 30]

Owner, 11.11.2026. A battalion is defined by its core callsigns (knowledge/upstream/battalions/cores.json,
namesakes left out). For every frequency we count intercepts where a core man of a battalion is a
LISTED STATION, not on a net signed for an untracked formation (namesakes) — the same rule as bat_atlas.py. Calls in speech («Х, я Y») are opt-in (--with-speech):
common words (БАЗА, ТОК, ЗАКАТ) would count, and the 11.11 check showed they move no frequency anyway.
A frequency is a battalion's MAIN one when ≥60% of those hits are that battalion's and its men are in
≥10% of the frequency's intercepts (else «епізодично»); it is SHARED when two battalions each hold
≥25%. Patagonia is counted apart throughout, so it can always be dropped.

Writes FREQ_<DD.MM>.md (the attribution) and BAT_FREQ_<DD.MM>.md (frequency → who is heard there).
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "identity"))
from posady import parse                                       # noqa: E402

CORPUS = "file:~/wa-monitor/messages.db?mode=ro"
CORES = REPO / "knowledge" / "upstream" / "battalions" / "cores.json"
OUT = REPO / "knowledge" / "upstream" / "battalions"
KYIV = ZoneInfo("Europe/Kyiv")
OURS, PATA = ("Invisible Hand", "AllInARow"), "PATAGONIA_GP"
NAME = r"[А-ЯЁІЇЄ][а-яёіїє]{2,}"
STATION = re.compile(r"^[А-ЯЁІЇЄа-яёіїє][А-ЯЁІЇЄа-яёіїє' -]{1,24}$")
UNKNOWN = {"НВ", "Н/В", "НЕВІДОМО", "НЕВИДОМО", "НЕВИДОМИИ", "НЕВІДОМИИ", "?"}
MIN_HEARD = 3        # a callsign is listed on a frequency from this many intercepts
MAX_NAMES = 14
CALL = re.compile(rf"^\W*(?:\d+\s*[–—-]\s*)?({NAME})(?:[\s,.]+\1)*[\s,.]+(?:я\s+)?({NAME})", re.I)


def fold(s: str) -> str:
    t = s.upper()
    for a, b in (("Ё", "Е"), ("Ы", "И"), ("Й", "И"), ("І", "И"), ("Ї", "И")):
        t = t.replace(a, b)
    return t.replace("Ь", "")


# A core callsign on a net signed for a formation we do NOT track (60 омсбр, 114 мсп…) is a namesake:
# ЗУБ, ГЕН, МАРИЯ there dragged Білогір'я into 1 мсб 38 (atlas, 11.11). Labels AMONG the tracked
# formations are not trusted either way — 411.96x is signed «2 мсб 38» and carries 189 мсп's people, who
# call 38 «союзники»; the people decide. Shared with bat_atlas.py — one definition.
FORMATION = re.compile(r"\b(\d{2,4})\s*(омсбр|омбср|омбр|мсп|мсд|обрмп|оброн|дшп|одшбр|сп|тп|бр)\b", re.I)


def formation_of(header: str) -> str:
    m = FORMATION.search(header)
    if not m:
        return "?"
    kind = m.group(2).lower()
    return f"{m.group(1)} {'омсбр' if kind in ('омбр', 'омбср') else kind}"   # «38 омбр» is a typo of 38 омсбр


def counts_here(bat: str, fm: str, tracked: set[str]) -> bool:
    """Does a core man of `bat` count on a net whose header names formation `fm`?"""
    return fm == "?" or fm in tracked


def nf(f: str) -> str:
    try:
        return f"{float(f):.4f}"
    except ValueError:
        return f


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--with-speech", action="store_true",
                    help="also count calls in speech («Х, я Y») — a cross-check, not the rule")
    a = ap.parse_args()
    cores = {k: {fold(n) for n in v} for k, v in json.loads(CORES.read_text()).items() if not k.startswith("_")}
    tracked = {b.split(" / ")[0] for b in cores}
    who = {}
    for bat, names in cores.items():
        for n in names:
            who[n] = bat
    now = datetime.now(KYIV)
    t0 = now - timedelta(days=a.days)
    db = sqlite3.connect(CORPUS, uri=True)
    hits = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))   # freq -> bat -> src
    total = collections.defaultdict(collections.Counter)                                 # freq -> src
    days = collections.defaultdict(list)
    men = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    crowd = collections.defaultdict(collections.Counter)      # freq -> every listed station callsign
    disp = collections.defaultdict(collections.Counter)       # folded -> spelling as the analysts write it
    seen = set()
    for g, txt in db.execute("SELECT group_name, text FROM messages WHERE timestamp >= ? AND group_name IN (?,?,?) "
                             "ORDER BY timestamp", (int(t0.timestamp() * 1000), *OURS, PATA)):
        r = parse(txt or "")
        if not r:
            continue
        src = "П" if g == PATA else "Н"
        body = re.sub(r"\W", "", " ".join(r["speech"]))[:200]
        if (src, r["day"], body) in seen:
            continue
        seen.add((src, r["day"], body))
        f = nf(r["ref"].split()[-1])
        total[f][src] += 1
        listed = [n.strip() for x in r["stations"] for n in re.split(r"[,/]", re.sub(r"\(.*?\)", "", x))]
        names = {fold(n) for n in listed}
        for n in listed:
            if STATION.match(n) and fold(n) not in UNKNOWN:
                crowd[f][fold(n)] += 1
                disp[fold(n)][n.upper()] += 1
        for l in (r["speech"] if a.with_speech else []):
            m = CALL.match(l)
            if m:
                names |= {fold(m.group(1)), fold(m.group(2))}
        fm = formation_of(r["header"])
        bats = {who[n] for n in names if n in who and counts_here(who[n], fm, tracked)}
        for b in bats:
            hits[f][b][src] += 1
            days[(f, b)].append(r["day"])
            for n in names:
                if who.get(n) == b:
                    men[f][b][n] += 1
    rows, visits = collections.defaultdict(list), collections.defaultdict(list)
    shared, lines = [], []
    for f, per in hits.items():
        tot_b = {b: sum(c.values()) for b, c in per.items()}
        allhit = sum(tot_b.values())
        if allhit < 10:
            continue
        ranked = sorted(tot_b.items(), key=lambda kv: -kv[1])
        b0, n0 = ranked[0]
        if n0 >= 0.6 * allhit:
            # ≥10% of the frequency's intercepts must carry his men, or it is a net he visits (11.11 recheck:
            # 147.0001 was «1 мсб» on 65 of 4217 intercepts)
            (rows if n0 >= 0.10 * sum(total[f].values()) else visits)[b0].append(f)
        big = [b for b, n in ranked if n >= 0.25 * allhit]
        if len(big) > 1:
            shared.append((f, [(b, tot_b[b]) for b in big]))
    lines.append(f"# Батальйони по частотах — {t0:%d.%m} .. {now:%d.%m.%Y}")
    lines.append("")
    lines.append("Частота належить батальйону, якщо ≥60% перехоплень з його людьми на ній — його. "
                 "Числа: всього перехоплень на частоті `наші / Патагонія`; «з людьми» — де хтось із ядра "
                 + ("є станцією або викликає в мові (перевірочний прогін)." if a.with_speech else "у списку станцій."))
    for bat in cores:
        fs = sorted(rows.get(bat, []), key=lambda f: -sum(hits[f][bat].values()))
        lines.append(f"\n## {bat}")
        if not fs:
            lines.append("- основних частот не знайдено")
        for f in fs:
            d = sorted(days[(f, bat)])
            lines.append(f"- **{f}** — всього {total[f]['Н']} / {total[f]['П']}, з людьми {sum(hits[f][bat].values())}; "
                         f"{d[0][5:]} .. {d[-1][5:]}; {', '.join(n for n, _ in men[f][bat].most_common(5))}")
        if visits.get(bat):
            lines.append("- епізодично (його люди <10% перехоплень частоти): " + "; ".join(
                f"{f} ({sum(hits[f][bat].values())} з {sum(total[f].values())})" for f in visits[bat]))
    lines.append("\n## Спільні частоти (люди двох батальйонів)")
    for f, bs in sorted(shared, key=lambda x: -sum(n for _, n in x[1])):
        lines.append(f"- **{f}** — " + "; ".join(f"{b}: {n}" for b, n in bs))
    path = OUT / f"FREQ_{now:%d.%m}{'_speech' if a.with_speech else ''}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"→ {path}")

    if a.with_speech:
        return                                    # the cross-check run must not overwrite the sheet
    # the readable sheet (owner 11.11.2026: «частоти, батальйони, ще й позивні — супер читабельно»)
    def show(n):
        return disp[n].most_common(1)[0][0] if disp[n] else n

    def fmt_day(d):
        return f"{d[8:10]}.{d[5:7]}"

    out = [f"# Батальйони → частоти → позивні ({t0:%d.%m} - {now:%d.%m.%Y})", "",
           "На кожній частоті - хто звучить як станція, від найчастішого. "
           f"Ядро батальйону **жирним**, решта - всі, хто на частоті ≥{MIN_HEARD} разів.", ""]
    for bat in cores:
        fs = sorted(rows.get(bat, []), key=lambda f: -sum(hits[f][bat].values()))
        if not fs:
            continue
        out.append(f"## {bat}")
        for f in fs:
            d = sorted(days[(f, bat)])
            n_all = sum(total[f].values())
            src = "тільки Патагонія" if not total[f]["Н"] else "тільки наші" if not total[f]["П"] else ""
            ppl = [n for n, c in crowd[f].most_common() if c >= MIN_HEARD]
            names_s = ", ".join(f"**{show(n)}**" if who.get(n) == bat else show(n) for n in ppl[:MAX_NAMES])
            if len(ppl) > MAX_NAMES:
                names_s += f" (+ ще {len(ppl) - MAX_NAMES})"
            out.append(f"**{f}** · {fmt_day(d[0])}-{fmt_day(d[-1])} · {n_all} перехоплень{' · ' + src if src else ''}")
            out.append(f"{names_s or '—'}")
            out.append("")
    if shared:
        out.append("## Спільні частоти")
        for f, bs in sorted(shared, key=lambda x: -sum(n for _, n in x[1])):
            out.append(f"**{f}** · " + " + ".join(b for b, _ in bs))
            ppl = [n for n, c in crowd[f].most_common() if c >= MIN_HEARD][:MAX_NAMES]
            out.append(", ".join(f"{show(n)} ({who[n].split(' / ')[0]} {who[n].split(' / ')[1].split(',')[0]})"
                                 if n in who else show(n) for n in ppl) or "—")
            out.append("")
    sheet = OUT / f"BAT_FREQ_{now:%d.%m}.md"
    sheet.write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))
    print(f"\n→ {sheet}")


if __name__ == "__main__":
    main()
