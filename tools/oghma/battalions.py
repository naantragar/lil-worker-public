#!/usr/bin/env python3
"""Which battalions of a formation we hear, and where they work — from net headers, checked by speech.

    python3 tools/upstream/battalions.py                       # 38 омсбр, 1198 мсп, 189 мсп; 30 days + last 14
    python3 tools/upstream/battalions.py --days 30 --recent 14 --units "38 омсбр" "1198 мсп"

Owner, 11.11.2026: «які батальйони ми чуємо (38, 1198, 189) і де вони виконують задачі». Month window,
the last two weeks shown apart; all three groups, Patagonia counted SEPARATELY so it can be dropped.

Sources of «where», in order of trust:
  * the settlements an analyst wrote in the header — a direction, what he believed;
  * MGRS of stations (Patagonia writes them next to a callsign) — reported as 1 km squares, NOT
    turned into place names: no gazetteer here, and a made-up coordinate is worse than a grid ref
    the analyst can put on his map;
  * battalion / regiment numbers said in SPEECH — the cross-check of the header (owner: «на наших
    частотах виходять другі підрозділи»).
People per battalion come from our role register (roles.db), by the header of the evidence.
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
ROLES = REPO / "knowledge" / "upstream" / "identity" / "roles.db"
OUT = REPO / "knowledge" / "upstream" / "battalions"
KYIV = ZoneInfo("Europe/Kyiv")
OURS = ("Invisible Hand", "AllInARow")
PATA = "PATAGONIA_GP"

UNIT_RX = {
    "38 омсбр": re.compile(r"\b38\s*о?м?с?бр\b|\b38\s*омбр\b|\b38\s*омбср\b", re.I),
    "1198 мсп": re.compile(r"\b1198\s*мсп\b|\b1198\b", re.I),
    "189 мсп": re.compile(r"\b189\s*мсп\b", re.I),
}
SUB_RX = [
    (re.compile(r"\b(\d)\s*-?\s*(?:о?мсб|мсб)\b", re.I), lambda m: f"{m.group(1)} мсб"),
    (re.compile(r"\bдшб\b", re.I), lambda m: "дшб"),
    (re.compile(r"\bшгр?\b", re.I), lambda m: "шг"),
    (re.compile(r"\b(\d)?\s*шр\b|\bшз\b|шторм", re.I), lambda m: "шр/шз «Шторм» / «V»"),
    (re.compile(r"управлінн", re.I), lambda m: "управління"),
]
COMPANY = re.compile(r"\b(\d)\s*мср\b", re.I)
NOISE = re.compile(r"^(Р-Н|РН|ЙМ\.?|ЙМ|ПЕЛЕНГ|ДОРОЗВІДКА|ДО|ПІВД\.?|ЗАХ\.?|ПН\.?|СХ\.?|ПД\.?|ЗХ\.?|"
                   r"ТРОФЕЙ.*|DMR|МГЦ|\d+[\d.]*|ЦФП.*|\(?КТ\)?)$", re.I)
MGRS = re.compile(r"MGRS:\s*(\d{2}[A-Z])([A-Z]{2})(\d{5})(\d{5})")
# Numbers in this air are codes, channels and minutes, so a unit is counted only with its word:
# «второй бат», «2-й батальон», «второе хозяйство» (their slang for a battalion), «189-й полк»,
# «38-я бригада». Bare «38» or «189» is not evidence.
SPEECH_BAT = re.compile(
    r"\b(перв\w*|втор\w*|трет\w*|четв\w*|[1-4])[\s-]*(?:й|я|е|ое|ый|ий)?\s+(бат\w*|мсб|хозяйств\w*)"
    r"|\b(сто\s+восемьдесят\s+девят\w*\s+(?:полк|мсп)\w*|189[\s-]*(?:й|ый|го)?\s*(?:полк|мсп)\w*"
    r"|тридцать\s+восьм\w*\s+бригад\w*|38[\s-]*(?:я|ая|й)?\s*(?:бригад|омсбр)\w*"
    r"|1198[\s-]*(?:й|ый)?\s*(?:полк|мсп)?\w*)", re.I)
ORD = {"перв": "1", "втор": "2", "трет": "3", "четв": "4"}


def bat_of(part: str) -> str:
    for rx, name in SUB_RX:
        m = rx.search(part)
        if m:
            return name(m)
    return "підрозділ не вказано"


def places(header: str) -> list[str]:
    out = []
    for inner in re.findall(r"\(([^()]*)\)", header):
        for p in re.split(r"[,–—]|\s*-\s+|\s+-\s*|-(?=[А-ЯІЇЄ])|→", inner):
            p = re.sub(r"\s+", " ", p.replace("’", "'")).strip(" .").upper()
            p = re.sub(r"^(Р-Н|РН|ЙМ\.?|ПЕЛЕНГ|ДОРОЗВІДКА|ДО)\s+", "", p)
            if p and not NOISE.match(p) and len(p) > 3 and re.search(r"[А-ЯІЇЄ]{4}", p):
                out.append(p)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--recent", type=int, default=14)
    ap.add_argument("--units", nargs="*", default=list(UNIT_RX))
    a = ap.parse_args()
    now = datetime.now(KYIV)
    t0 = now - timedelta(days=a.days)
    t_recent = (now - timedelta(days=a.recent)).strftime("%Y-%m-%d")
    db = sqlite3.connect(CORPUS, uri=True)
    # bucket: (unit, bat) -> stats split by source and period
    st = collections.defaultdict(lambda: {
        "n": collections.Counter(), "days": collections.defaultdict(set), "last": "", "freqs": collections.Counter(),
        "places": collections.Counter(), "mgrs": collections.Counter(), "ym": 0, "dual": 0, "companies": collections.Counter(),
        "speech": collections.Counter()})
    seen = set()
    q = "SELECT group_name, text FROM messages WHERE timestamp >= ? AND group_name IN (?,?,?) ORDER BY timestamp"
    for g, txt in db.execute(q, (int(t0.timestamp() * 1000), *OURS, PATA)):
        r = parse(txt or "")
        if not r:
            continue
        src = "Патагонія" if g == PATA else "наші"
        body = re.sub(r"\W", "", " ".join(r["speech"]))[:200]
        if (src, r["day"], body) in seen:
            continue                          # one transmission through two groups of the same side
        seen.add((src, r["day"], body))
        head = r["header"]
        parts = re.split(r"\s\+\s", head)
        for part in parts:
            for unit in a.units:
                if not UNIT_RX[unit].search(part):
                    continue
                bat = bat_of(part if len(parts) > 1 else head)
                s = st[(unit, bat)]
                per = "14" if r["day"] >= t_recent else "old"
                s["n"][(src, per)] += 1
                s["days"][src].add(r["day"])
                s["last"] = max(s["last"], r["day"])
                s["freqs"][r["ref"].split()[-1]] += 1
                s["places"].update(places(head))
                for m in COMPANY.finditer(part):
                    s["companies"][f"{m.group(1)} мср"] += 1
                if re.search(r"\bйм\.?|ймовірно", part, re.I):
                    s["ym"] += 1
                if len(parts) > 1:
                    s["dual"] += 1
                for x in r["stations"]:
                    for m in MGRS.finditer(x):
                        s["mgrs"][f"{m.group(2)} {m.group(3)[:2]}{m.group(4)[:2]}"] += 1
                for m in SPEECH_BAT.finditer(" ".join(r["speech"])):
                    if m.group(2):
                        k = m.group(1).lower()
                        k = ORD.get(k[:4], k)
                        s["speech"][f"{k} бат"] += 1
                    else:
                        w = m.group(3).lower()
                        s["speech"]["189" if ("189" in w or "восемьдесят" in w) else "1198" if "1198" in w else "38"] += 1
    # people from our register, by the header of their evidence
    people = collections.defaultdict(collections.Counter)
    try:
        rc = sqlite3.connect(f"file:{ROLES}?mode=ro", uri=True)
        for role, cs, net, title in rc.execute(
                "SELECT e.role, e.callsign, e.net, COALESCE(v.title,'') FROM evidence e JOIN verdict v "
                "USING(role, callsign, unit) WHERE v.machine='active' AND COALESCE(v.owner,'') != 'no' AND e.day >= ?",
                (t0.strftime("%Y-%m-%d"),)):
            for part in re.split(r"\s\+\s", net or ""):
                for unit in a.units:
                    if UNIT_RX[unit].search(part):
                        lab = {"komsklad": "ком склад", "nakopych": "накопичувач", "bpla_log": "БпЛА логістики"}.get(role, title)
                        people[(unit, bat_of(part))][f"{cs} ({lab})"] += 1
    except sqlite3.Error:
        pass

    OUT.mkdir(parents=True, exist_ok=True)
    lines = [f"# Батальйони: {', '.join(a.units)} — {t0:%d.%m} .. {now:%d.%m.%Y} (останні {a.recent} діб: з {t_recent})",
             "", "Числа: перехоплень `наші / Патагонія`, у дужках — за останні "
             f"{a.recent} діб. Райони — з шапок аналітиків; MGRS — квадрати 1 км зі станцій (Патагонія).", ""]
    for unit in a.units:
        lines.append(f"## {unit}")
        bats = sorted((k for k in st if k[0] == unit), key=lambda k: -sum(st[k]["n"].values()))
        for k in bats:
            s = st[k]
            ours = s["n"][("наші", "14")] + s["n"][("наші", "old")]
            pata = s["n"][("Патагонія", "14")] + s["n"][("Патагонія", "old")]
            if ours + pata < 3:
                continue
            lines.append(f"### {k[1]} — наші {ours} ({s['n'][('наші','14')]}), Патагонія {pata} ({s['n'][('Патагонія','14')]}); "
                         f"днів {len(s['days']['наші'] | s['days']['Патагонія'])}, останній раз {s['last']}")
            lines.append(f"- райони: {', '.join(f'{p} ({c})' for p, c in s['places'].most_common(8))}")
            if s["mgrs"]:
                lines.append(f"- MGRS (квадрат 1 км, зона 37T): {', '.join(f'{p} ({c})' for p, c in s['mgrs'].most_common(6))}")
            if s["companies"]:
                lines.append(f"- роти в шапках: {', '.join(f'{p} ({c})' for p, c in s['companies'].most_common(5))}")
            lines.append(f"- частоти: {', '.join(f'{p} ({c})' for p, c in s['freqs'].most_common(6))}")
            lines.append(f"- «йм.» у підписі: {s['ym']}; подвійна шапка (+): {s['dual']}")
            if s["speech"]:
                lines.append(f"- у МОВІ названо: {', '.join(f'{p} ({c})' for p, c in s['speech'].most_common(6))}")
            if people.get(k):
                lines.append(f"- люди (наш реєстр): {', '.join(p for p, _ in people[k].most_common(10))}")
            lines.append("")
    path = OUT / f"BAT_{now:%d.%m}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n→ {path}")


if __name__ == "__main__":
    main()
