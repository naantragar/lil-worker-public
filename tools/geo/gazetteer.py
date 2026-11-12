#!/usr/bin/env python3
"""Gazetteer of the settlements our analysts name in net headers — every point checked twice, locally.

    tools/geo/.venv/bin/python tools/geo/gazetteer.py [--days 60]

Owner, 12.11.2026: battalions go on a map, and the settlement → coordinate step must be checkable,
not trusted. Nothing is sent anywhere: both sources live on this box (tools/geo/fetch_sources.sh).

  * GeoNames UA dump — places (feature class P) inside the front bbox;
  * our own OpenStreetMap copy — the `places` layer of upstream's tile archive.

A name is resolved when both sources hold a point within AGREE_KM of each other. A name with several
candidates (Мирне, Новоселівка…) is resolved by its NEIGHBOURS: the settlements it is written next to in
the same headers, plus the MGRS station fixes of those intercepts. The nearest candidate wins only by a
clear margin; otherwise the name is left «неоднозначно» and is NOT put on the map. Every verdict carries
the reason, so the owner can check any line by hand.

Output: knowledge/upstream/geo/gazetteer.json (machine) and GAZETTEER.md (people: the doubtful first).
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import math
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "identity"))
sys.path.insert(0, str(REPO / "tools" / "upstream"))
from posady import parse                                       # noqa: E402
from geo import mgrs_to_ll                                     # noqa: E402  one MGRS converter for every tool

CORPUS = "file:~/wa-monitor/messages.db?mode=ro"
GEO = REPO / "knowledge" / "upstream" / "geo"
SRC = GEO / "src"
BBOX = (34.6, 46.4, 38.4, 48.6)
KYIV = ZoneInfo("Europe/Kyiv")
AGREE_KM = 2.0          # two sources name the same village
ANCHOR_MAX_KM = 25.0    # an ambiguous name must sit this close to its neighbours…
MARGIN = 2.0            # …and the runner-up must be this many times farther
MGRS_FLAG_KM = 15.0     # stations of the intercepts far from the place named: worth a look
MGRS_RX = re.compile(r"MGRS:\s*(\d{2}[A-Z][A-Z]{2}\d{4,10})")
# Words an analyst puts in front of a settlement: direction, «район», «пеленг», «трофей», «н.п.», marks.
PREFIX = re.compile(
    r"^(?:(?:Р-Н|Р Н|РН|РАЙОН|ПЕЛЕНГ|ПЕЛ|ТР|ТРОФЕЙ|ЙДЕ|ЛУНАВ|ДОРОЗВІДКА|Н\.?\s?П|С|СМТ|М|ДО|БІЛЯ|ПОБЛИЗУ|"
    r"ПІВНІЧНІШЕ|ПІВДЕННІШЕ|ЗАХІДНІШЕ|СХІДНІШЕ|ПІВН|ПІВД|ПІВД-ЗАХ|ПІВН-ЗАХ|ПІВД-СХ|ПІВН-СХ|ПН|ПД|СХ|ЗХ|ЗАХ|СХІД|"
    r"ПН-СХ|ПН-ЗХ|ПД-СХ|ПД-ЗХ|ПД\.ЗХ|ПН\.ЗХ|ПД\.СХ|ПН\.СХ)(?:[.:]\s*|\s+))+", re.I)
SPLIT = re.compile(r"[,;→+–—]|\s-\s|\s-(?=\S)|(?<=\S)-\s")


def fold(s: str) -> str:
    t = s.upper().replace("’", "'").replace("ʼ", "'").replace("`", "'").replace("Ё", "Е").replace("-", " ")
    t = re.sub(r"[^\w' ]", " ", t)
    return re.sub(r"\s+", " ", t).strip(" .'")


def header_places(header: str, known) -> list[tuple[str, str]]:
    """(folded name, raw piece) from the parentheses of a net header.

    A piece that is not a known name as a whole is tried as a run of known names
    («ЧАРІВНЕ ГУЛЯЙПІЛЬСЬКЕ НОВОСЕЛІВКА») — longest match first, so «МАЛА ТОКМАЧКА» stays one place.
    """
    out = []
    for inner in re.findall(r"\(([^()]*)\)", header):
        for piece in SPLIT.split(inner):
            raw = piece.strip()
            n = fold(PREFIX.sub("", fold(raw)))
            if not n or not re.search(r"[А-ЯІЇЄ]{4}", n) or re.search(r"\d", n) or PREFIX.fullmatch(n + " "):
                continue
            if n in known:
                out.append((n, raw))
                continue
            w, i, got = n.split(), 0, []
            while i < len(w):
                for j in range(len(w), i, -1):
                    if " ".join(w[i:j]) in known:
                        got.append(" ".join(w[i:j]))
                        i = j
                        break
                else:
                    i += 1
            out += [(g, raw) for g in got] if got else [(n, raw)]
    return out


def km(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def edit1(a: str, b: str) -> bool:
    """One typo apart (substitution, insertion or deletion) — a suggestion only, never auto-applied."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    s, l = sorted((a, b), key=len)
    return any(l[:i] + l[i + 1:] == s for i in range(len(l)))


def inside(lat, lon) -> bool:
    w, s, e, n = BBOX
    return s <= lat <= n and w <= lon <= e


def load_sources():
    gn = collections.defaultdict(list)
    for r in csv.reader(open(SRC / "UA.txt", encoding="utf-8"), delimiter="\t", quoting=csv.QUOTE_NONE):
        if r[6] != "P":
            continue
        lat, lon = float(r[4]), float(r[5])
        if not inside(lat, lon):
            continue
        for n in {r[1], *r[3].split(",")}:
            if re.search(r"[А-ЯЁІЇЄа-яёіїє]", n):
                gn[fold(n)].append((lat, lon, r[1], r[0]))
    osm = collections.defaultdict(list)
    for p in json.loads((SRC / "osm_places.json").read_text()):
        for n in {p["name"], p["uk"], p["ru"]} - {None}:
            osm[fold(n)].append((p["lat"], p["lon"], p["uk"] or p["name"], p["kind"]))
    return gn, osm


def dedupe(pts):
    out = []
    for p in pts:
        if all(km(p[:2], q[:2]) > 1.0 for q in out):
            out.append(p)
    return out


def candidates(name, gn, osm):
    """Points both sources agree on; then points only one source has (kept, but marked)."""
    g, o = dedupe(gn.get(name, [])), dedupe(osm.get(name, []))
    both, used_o = [], set()
    for a in g:
        best = min(((km(a[:2], b[:2]), i) for i, b in enumerate(o)), default=None)
        if best and best[0] <= AGREE_KM:
            b = o[best[1]]
            used_o.add(best[1])
            both.append({"lat": b[0], "lon": b[1], "src": "обидва", "gap_km": round(best[0], 2),
                         "uk": b[2], "kind": b[3], "geonames": a[3]})
    single = [{"lat": a[0], "lon": a[1], "src": "лише GeoNames", "uk": a[2], "geonames": a[3]}
              for a in g if not any(km(a[:2], (c["lat"], c["lon"])) <= AGREE_KM for c in both)]
    single += [{"lat": b[0], "lon": b[1], "src": "лише OSM", "uk": b[2], "kind": b[3]}
               for i, b in enumerate(o) if i not in used_o]
    return both, single


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    a = ap.parse_args()
    gn, osm = load_sources()
    known = set(gn) | set(osm)
    now = datetime.now(KYIV)
    t0 = now - timedelta(days=a.days)
    db = sqlite3.connect(CORPUS, uri=True)
    count = collections.Counter()
    raw = collections.defaultdict(collections.Counter)      # folded -> raw spellings
    co = collections.defaultdict(collections.Counter)       # name -> names in the same header
    fixes = collections.defaultdict(list)                   # name -> MGRS points of its intercepts
    seen = set()
    for (txt,) in db.execute("SELECT text FROM messages WHERE timestamp >= ? AND group_name IN "
                             "('Invisible Hand','AllInARow','PATAGONIA_GP')", (int(t0.timestamp() * 1000),)):
        r = parse(txt or "")
        if not r or r["header"] in seen and not any("MGRS" in x for x in r["stations"]):
            continue
        seen.add(r["header"])
        names = []
        for n, p in header_places(r["header"], known):
            names.append(n)
            raw[n][p] += 1
        pts = []
        for x in r["stations"]:
            for g in MGRS_RX.findall(x):
                ll = mgrs_to_ll(g)
                if ll:
                    pts.append(ll)
        for n in set(names):
            count[n] += 1
            co[n].update(set(names) - {n})
            fixes[n].extend(pts)

    res = {}
    # pass 1: names with exactly one agreed point and nothing else in the region
    for n in count:
        both, single = candidates(n, gn, osm)
        res[n] = {"name": n, "seen": count[n], "spellings": dict(raw[n].most_common(3)), "both": both, "single": single}
        if len(both) == 1 and not single:
            res[n].update(status="певно", reason="одна точка в районі, обидва джерела збігаються", pick=both[0])
        elif len(both) == 1:
            res[n].update(status="певно", reason=f"обидва джерела збігаються; ще {len(single)} точка(и) лише в одному", pick=both[0])
    # pass 2: ambiguous names by their neighbours + MGRS
    for n, d in res.items():
        if "status" in d:
            continue
        cands = d["both"] or d["single"]
        if not cands:
            near = sorted(k for k in known if len(k) > 4 and abs(len(k) - len(n)) <= 1 and edit1(k, n))
            d.update(status="не знайдено", reason="немає в жодному джерелі в межах району"
                     + (f"; схоже на {', '.join(near[:3])} (не підставляю сам)" if near else ""))
            continue
        anchors = [(res[o]["pick"]["lat"], res[o]["pick"]["lon"]) for o, c in co[n].items()
                   for _ in range(min(c, 5)) if res.get(o, {}).get("status") == "певно"] + fixes[n][:200]
        if not anchors:
            if len(cands) == 1:
                d.update(status="одне джерело", reason=f"{cands[0]['src']}; сусідів для перевірки немає", pick=cands[0])
            else:
                d.update(status="неоднозначно", reason=f"{len(cands)} кандидати, сусідів немає")
            continue
        ctr = (statistics.median(p[0] for p in anchors), statistics.median(p[1] for p in anchors))
        ranked = sorted(cands, key=lambda c: km(ctr, (c["lat"], c["lon"])))
        d0 = km(ctr, (ranked[0]["lat"], ranked[0]["lon"]))
        d1 = km(ctr, (ranked[1]["lat"], ranked[1]["lon"])) if len(ranked) > 1 else None
        why = f"сусіди/MGRS: {len(anchors)} опор, найближчий кандидат {d0:.1f} км" + (f", наступний {d1:.1f} км" if d1 else "")
        if d0 <= ANCHOR_MAX_KM and (d1 is None or d1 >= MARGIN * max(d0, 1.0)):
            d.update(status="певно" if ranked[0]["src"] == "обидва" else "одне джерело", reason=why, pick=ranked[0])
        else:
            d.update(status="неоднозначно", reason=why)
    # MGRS sanity: the stations of the intercepts that name the place
    for n, d in res.items():
        if d.get("pick") and fixes[n]:
            dist = statistics.median(km((d["pick"]["lat"], d["pick"]["lon"]), p) for p in fixes[n])
            d["mgrs_median_km"] = round(dist, 1)
            d["mgrs_n"] = len(fixes[n])
            if dist > MGRS_FLAG_KM:
                d["flag"] = f"станції цих перехоплень у медіані за {dist:.0f} км"

    GEO.mkdir(parents=True, exist_ok=True)
    (GEO / "gazetteer.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    by = collections.defaultdict(list)
    for d in sorted(res.values(), key=lambda d: -d["seen"]):
        by[d["status"]].append(d)
    L = [f"# Довідник населених пунктів із шапок — {t0:%d.%m} .. {now:%d.%m.%Y}", "",
         "Джерела лише локальні: GeoNames (дамп України) і наша копія OpenStreetMap (плитки Огми). "
         f"«Певно» = обидва джерела в межах {AGREE_KM:.0f} км; неоднозначну назву вирішують сусіди в тих самих шапках і MGRS станцій. "
         "Будь-який рядок можна перевірити: координати відкрити на будь-якій карті.", ""]
    L.append("Підсумок: " + ", ".join(f"{k} {len(v)}" for k, v in by.items()) +
             f"; із прапорцем MGRS {sum(1 for d in res.values() if d.get('flag'))}")
    def line(d):
        p = d.get("pick")
        s = f"- **{d['name']}** ({d['seen']}×) — "
        s += f"{p['lat']:.4f}, {p['lon']:.4f} [{p['src']}{', розбіжність ' + str(p['gap_km']) + ' км' if p.get('gap_km') is not None else ''}]; " if p else ""
        s += d["reason"]
        if d.get("mgrs_median_km") is not None:
            s += f"; MGRS: {d['mgrs_n']} точок, медіана {d['mgrs_median_km']} км"
        if d.get("flag"):
            s += f" ⚑ {d['flag']}"
        if not p and (d["both"] or d["single"]):
            s += "; кандидати: " + "; ".join(f"{c['lat']:.3f},{c['lon']:.3f} ({c['src']})" for c in (d["both"] + d["single"])[:6])
        return s
    for k in ("неоднозначно", "не знайдено", "одне джерело"):
        if by.get(k):
            L += ["", f"## {k} ({len(by[k])})"] + [line(d) for d in by[k]]
    flagged = [d for d in by.get("певно", []) if d.get("flag")]
    if flagged:
        L += ["", f"## певно, але з прапорцем MGRS ({len(flagged)})"] + [line(d) for d in flagged]
    L += ["", f"## певно ({len(by.get('певно', []))})"] + [line(d) for d in by.get("певно", []) if not d.get("flag")]
    (GEO / "GAZETTEER.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L[:6]))
    for k in ("неоднозначно", "не знайдено", "одне джерело"):
        print(f"\n## {k}: " + ", ".join(f"{d['name']}({d['seen']})" for d in by.get(k, [])[:40]))
    print("\nprapor:", [(d["name"], d.get("mgrs_median_km")) for d in flagged][:20])


if __name__ == "__main__":
    main()
