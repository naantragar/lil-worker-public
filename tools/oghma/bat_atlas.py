#!/usr/bin/env python3
"""Battalion atlas: the reference (who, which frequencies, where) and a map you can look at.

    python3 tools/upstream/bat_atlas.py [--days 30] [--points-days 120] [--out-dir /root/upstream-maps]

Owner, 11.11.2026: «щоб це було картою, яку можна побачити очима», layers switched on and off.
Built as a separate tool, not inside upstream's «Мапа»: one HTML file, fully offline — the base map is
cut from upstream's own OSM archive (/var/lib/upstream-tiles) and embedded, so opening it on a phone sends
no request anywhere (the owner's 23.10 rule for upstream: no outside tile servers).

Writes
  knowledge/upstream/battalions/atlas.json       the reference, machine-readable (small, in the repo)
  knowledge/upstream/battalions/ATLAS_<DD.MM>.md the reference for reading
  <out-dir>/MAP_<DD.MM>.html                  the map (~18 MB, NOT in the repo)

Rules, same as battalion_freqs.py: a battalion is its PEOPLE (cores.json); a frequency is a
battalion's when ≥60% of the intercepts with core men on it are that battalion's. Here only LISTED
STATIONS count, not calls in speech — common words (БАЗА, ТОК, ЗАКАТ) must not place a battalion on
the map; the speech-call variant was checked 11.11 and moved no frequency.

Where things are, and how much each is worth:
  * settlements from the analysts' headers — the direction the analyst believed, placed ONLY through
    the checked gazetteer (tools/geo/gazetteer.py: GeoNames + our OSM agree within 2 km, namesakes
    resolved by neighbours); anything not «певно» there stays off the map and is listed with its status;
  * Patagonia's MGRS next to a station — a position given for that station. Patagonia stopped
    filling the field after 29.10.2026 10:56 (only «MGRS: —» since), so this layer is history; and in
    12-29.10 its fixes were for 60 омсбр and 114 мсп nets only — our three formations have positions for
    Aug-Sep alone (see «усе»).
"""
from __future__ import annotations

import argparse
import base64
import collections
import json
import math
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "identity"))
sys.path.insert(0, str(REPO / "tools" / "upstream"))
from posady import parse                                       # noqa: E402
sys.path.insert(0, str(REPO / "tools" / "geo"))
from gazetteer import header_places as gaz_split                # noqa: E402
from battalion_freqs import counts_here, fold, formation_of, nf  # noqa: E402
from geo import mgrs_to_ll                                     # noqa: E402

CORPUS = "file:~/wa-monitor/messages.db?mode=ro"
CORES = REPO / "knowledge" / "upstream" / "battalions" / "cores.json"
ROLES = REPO / "knowledge" / "upstream" / "identity" / "roles.db"
REF_DIR = REPO / "knowledge" / "upstream" / "battalions"
HERE = Path(__file__).resolve().parent / "atlas"
GAZ = REPO / "knowledge" / "upstream" / "geo" / "gazetteer.json"
TILES = Path("/var/lib/upstream-tiles/www/upstream.pmtiles")
PMT = next(Path("/var/lib/upstream-tiles/bin").glob("pmtiles*"), None)
NM = Path("~/upstream-system/web/node_modules")
BBOX = (35.55, 47.15, 36.95, 48.15)          # w, s, e, n — Huliaipole / Polohy sector with margin
MAXZOOM = 13                                 # z13 ≈ 13 MB; deeper zooms are drawn from it
COVER = 0.10                                  # share of a frequency's intercepts that carry the battalion's men
KYIV = ZoneInfo("Europe/Kyiv")
OURS, PATA = ("Invisible Hand", "AllInARow"), "PATAGONIA_GP"
COLORS = ["#ffb000", "#3ec6c0", "#ff5a36", "#7bd34f", "#e05bd6", "#5b8cff", "#f2e14c", "#ff8fb1", "#b48cff"]
STATION_MGRS = re.compile(r"^(.*?)\(\s*MGRS:\s*([0-9]{2}[C-X][A-Z]{2}\d{4,10})\s*\)")
ROLE_UK = {"komsklad": "ком склад", "nakopych": "накопичувач", "bpla_log": "БпЛА логістики"}


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((a[0] - b[0]) * 111.2, (a[1] - b[1]) * 111.2 * math.cos(math.radians(a[0])))


def build_base(tmp: Path) -> Path:
    area = tmp / "area.pmtiles"
    subprocess.run([str(PMT), "extract", str(TILES), str(area), f"--bbox={','.join(map(str, BBOX))}",
                    f"--maxzoom={MAXZOOM}"], check=True, capture_output=True, timeout=300)
    return area


class Gazetteer:
    """The checked gazetteer (knowledge/upstream/geo/gazetteer.json) — one source of coordinates for every map."""

    def __init__(self, path: Path):
        self.d = json.loads(path.read_text(encoding="utf-8"))
        self.known = set(self.d)

    def places(self, header: str) -> tuple[list[tuple[str, dict]], list[tuple[str, str]]]:
        ok, off = [], []
        for n, _raw in gaz_split(header, self.known):
            e = self.d.get(n)
            if e and e.get("status") == "певно" and e.get("pick"):
                ok.append((e["pick"].get("uk") or n.title(), e["pick"]))
            else:
                off.append((n, e["status"] if e else "немає в довіднику"))
        return ok, off


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--points-days", type=int, default=120)
    ap.add_argument("--out-dir", default="/root/upstream-maps")
    ap.add_argument("--no-map", action="store_true", help="reference only, skip the HTML")
    ap.add_argument("--map-only", action="store_true", help="re-render the HTML from the last run, no corpus read")
    a = ap.parse_args()
    if a.map_only:
        return map_only(Path(a.out_dir))

    cores_raw = {k: v for k, v in json.loads(CORES.read_text()).items() if not k.startswith("_")}
    who = {fold(n): b for b, ns in cores_raw.items() for n in ns}
    tracked = {b.split(" / ")[0] for b in cores_raw}
    spelled = {fold(n): n for ns in cores_raw.values() for n in ns}
    now = datetime.now(KYIV)
    t0 = now - timedelta(days=a.days)
    tp = now - timedelta(days=max(a.points_days, a.days))
    day0 = t0.strftime("%Y-%m-%d")

    db = sqlite3.connect(CORPUS, uri=True)
    recs, seen = [], set()
    for ts, g, txt in db.execute(
            "SELECT timestamp, group_name, text FROM messages WHERE timestamp >= ? AND group_name IN (?,?,?) "
            "ORDER BY timestamp", (int(tp.timestamp() * 1000), *OURS, PATA)):
        r = parse(txt or "")
        if not r:
            continue
        src = "П" if g == PATA else "Н"
        body = re.sub(r"\W", "", " ".join(r["speech"]))[:200]
        if (src, r["day"], body) in seen:
            continue
        seen.add((src, r["day"], body))
        names, pts = set(), []
        for x in r["stations"]:
            m = STATION_MGRS.match(x)
            if m:
                ll = mgrs_to_ll(m.group(2))
                if ll:
                    pts.append((fold(m.group(1).strip()) or "?", ll))
            for n in re.split(r"[,/]", re.sub(r"\(.*?\)", "", x)):
                if n.strip():
                    names.add(fold(n.strip()))
        recs.append({"src": src, "day": r["day"], "time": r["time"][-5:], "f": nf(r["ref"].split()[-1]),
                     "head": r["header"], "names": names, "pts": pts, "win": r["day"] >= day0})

    # 1. frequencies per battalion (stations only, the window)
    hits = collections.defaultdict(collections.Counter)
    tot = collections.defaultdict(collections.Counter)
    span = collections.defaultdict(list)
    fmen = collections.defaultdict(collections.Counter)
    for r in recs:
        if not r["win"]:
            continue
        tot[r["f"]][r["src"]] += 1
        fm = formation_of(r["head"])
        for b in {who[n] for n in r["names"] if n in who and counts_here(who[n], fm, tracked)}:
            hits[r["f"]][b] += 1
            span[(r["f"], b)].append(r["day"])
            fmen[(r["f"], b)].update(spelled[n] for n in r["names"] if who.get(n) == b)
    main_of, visit_of, shared = {}, {}, []
    for f, per in hits.items():
        allh = sum(per.values())
        if allh < 10:
            continue
        (b0, n0), *_ = per.most_common(1)
        if n0 >= 0.6 * allh:
            # a frequency with 4000 intercepts and 70 of them with our man on it is a place he visits,
            # not his net (147.0001, 142.4000 — caught 11.11): main needs ≥10% coverage
            if n0 >= COVER * (tot[f]["Н"] + tot[f]["П"]):
                main_of[f] = b0
            else:
                visit_of[f] = b0
        big = [(b, n) for b, n in per.most_common() if n >= 0.25 * allh]
        if len(big) > 1:
            shared.append({"f": f, "bats": big})

    # 2. attribute intercepts, collect places and people
    centre_pts = [ll for r in recs for _, ll in r["pts"]]
    centre = (statistics.median(p[0] for p in centre_pts), statistics.median(p[1] for p in centre_pts)) \
        if centre_pts else (47.6, 36.05)
    tmp = Path(tempfile.mkdtemp(prefix="atlas_"))
    area = build_base(tmp)
    gz = Gazetteer(GAZ)

    place_day = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))  # bat->place->day
    place_ll, unplaced = {}, collections.Counter()
    people = collections.defaultdict(lambda: {"n": 0, "days": [], "f": collections.Counter()})
    points = []
    for r in recs:
        # a core name on a net signed for an UNTRACKED formation is a namesake: ЗУБ, ГЕН, МАРИЯ
        # on a 60 омсбр net dragged Білогір'я into 1 мсб 38 on the first run (counts_here, battalion_freqs)
        fm = formation_of(r["head"])
        bats = {who[n] for n in r["names"] if n in who and counts_here(who[n], fm, tracked)}
        if r["f"] in main_of:
            bats.add(main_of[r["f"]])
        for (cs, (lat, lon)) in r["pts"]:
            own = who.get(cs) if who.get(cs) in bats else None
            points.append({"lat": round(lat, 5), "lon": round(lon, 5), "cs": spelled.get(cs, cs), "f": r["f"],
                           "d": r["day"], "t": r["time"], "h": r["head"][:140],
                           "b": own or (sorted(bats)[0] if len(bats) == 1 else None), "core": bool(own),
                           "fm": fm})
        if not r["win"]:
            continue
        for n in r["names"]:
            if n in who and who[n] in bats:
                p = people[n]
                p["n"] += 1
                p["days"].append(r["day"])
                p["f"][r["f"]] += 1
        if not bats:
            continue
        found, off = gz.places(r["head"])
        for n, why in off:
            unplaced[f"{n} ({why})"] += 1
        for nm, e in found:
            place_ll[nm] = (round(e["lat"], 5), round(e["lon"], 5))
            for b in bats:
                place_day[b][nm][r["day"]] += 1

    roles = collections.defaultdict(set)
    try:
        rc = sqlite3.connect(f"file:{ROLES}?mode=ro", uri=True)
        for role, cs, unit, title in rc.execute(
                "SELECT role, callsign, unit, COALESCE(title,'') FROM verdict "
                "WHERE machine='active' AND COALESCE(owner,'') != 'no'"):
            roles[(fold(cs), unit)].add(ROLE_UK.get(role, title or role))
    except sqlite3.Error:
        pass

    bats_out = []
    for i, (b, ns) in enumerate(cores_raw.items()):
        formation = b.split(" / ")[0]
        ppl = []
        for n in {fold(x): x for x in reversed(ns)}.values():      # ЛЫСЫЙ/ЛИСИЙ fold to one man
            p = people.get(fold(n))
            ppl.append({"cs": n, "n": p["n"] if p else 0,
                        "first": min(p["days"]) if p else None, "last": max(p["days"]) if p else None,
                        "freqs": [f for f, _ in p["f"].most_common(3)] if p else [],
                        "roles": sorted(roles.get((fold(n), formation), []))})
        ppl.sort(key=lambda x: -x["n"])
        fr = []
        for f in sorted((f for f, bb in main_of.items() if bb == b), key=lambda f: -hits[f][b]):
            d = sorted(span[(f, b)])
            fr.append({"f": f, "ours": tot[f]["Н"], "pata": tot[f]["П"], "with_people": hits[f][b],
                       "first": d[0], "last": d[-1], "top": [n for n, _ in fmen[(f, b)].most_common(5)]})
        visits = [{"f": f, "with_people": hits[f][b], "all": tot[f]["Н"] + tot[f]["П"],
                   "top": [n for n, _ in fmen[(f, b)].most_common(3)]}
                  for f in sorted((f for f, bb in visit_of.items() if bb == b), key=lambda f: -hits[f][b])]
        pl = [{"name": nm, "lat": place_ll[nm][0], "lon": place_ll[nm][1], "n": sum(c.values()), "days": dict(c)}
              for nm, c in sorted(place_day[b].items(), key=lambda kv: -sum(kv[1].values()))]
        last = max((x["last"] for x in ppl if x["last"]), default=None)
        bats_out.append({"key": b, "formation": formation, "color": COLORS[i % len(COLORS)], "last": last,
                         "people": ppl, "freqs": fr, "visits": visits, "places": pl})

    ref = {"generated": now.isoformat(timespec="minutes"), "window": [day0, now.strftime("%Y-%m-%d")],
           "points_from": tp.strftime("%Y-%m-%d"), "battalions": bats_out,
           "shared": [{"f": s["f"], "bats": [[b, n] for b, n in s["bats"]]} for s in shared],
           "unplaced": unplaced.most_common(30), "centre": centre}
    (REF_DIR / "atlas.json").write_text(json.dumps(ref, ensure_ascii=False, indent=1), encoding="utf-8")
    md = render_md(ref, now)
    (REF_DIR / f"ATLAS_{now:%d.%m}.md").write_text(md, encoding="utf-8")
    print(md)

    if not a.no_map:
        cache = Path(a.out_dir) / ".cache"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "points.json").write_text(json.dumps(points, ensure_ascii=False), encoding="utf-8")
        shutil.copyfile(area, cache / "area.pmtiles")
        render_map(ref, points, area, Path(a.out_dir), now)
    print(f"→ {REF_DIR / 'atlas.json'}")


GLOSSARY = REPO / "knowledge" / "upstream" / "glossary" / "glossary.db"
MGRS_IN_TEXT = re.compile(r"\b(\d{2}[C-X])\s*([A-Z]{2})\s*(\d{4,5})\s*(\d{4,5})\b")


def pow_points() -> list[dict]:
    """Points with MGRS from POW debriefs (the glossary), inside the map's bbox — a separate layer.

    The same spot is often written twice (chunk overlap in ingest, or two prisoners naming it): one
    marker per document per ~30 m, descriptions joined. Dated by the entry, else by the file name.
    """
    out: dict[tuple, dict] = {}
    try:
        con = sqlite3.connect(f"file:{GLOSSARY}?mode=ro", uri=True)
        rows = con.execute("SELECT kind, term, meaning, unit, dated, source FROM entries").fetchall()
    except sqlite3.Error:
        return []
    w, s_, e, n = BBOX
    for kind, term, meaning, unit, dated, src in rows:
        for g in MGRS_IN_TEXT.findall(f"{term} {meaning}"):
            ll = mgrs_to_ll("".join(g))
            if not ll or not (s_ <= ll[0] <= n and w <= ll[1] <= e):
                continue
            m = re.match(r"(\d{4})(\d{2})(\d{2})-\d+_[0-9A-F]+_(.+?)\.\w+$", src or "")
            doc = re.sub(r"_+", " ", m.group(4)).strip() if m else (src or "?")
            key = (src, round(ll[0], 4), round(ll[1], 4))
            text = re.sub(r"\s+", " ", f"{term} — {meaning}").strip()
            if key in out:
                if text not in out[key]["what"]:
                    out[key]["what"].append(text)
                continue
            # the debrief's own date is at the end of its name («… 08.11.2026», «… 14.09.26»); the file
            # name's leading stamp is only when it reached us
            dd = re.search(r"(\d{2})\.(\d{2})\.(\d{2,4})\s*$", doc)
            doc_d = (f"{'20' + dd.group(3) if len(dd.group(3)) == 2 else dd.group(3)}-{dd.group(2)}-{dd.group(1)}" if dd
                     else f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else "")
            out[key] = {"lat": round(ll[0], 5), "lon": round(ll[1], 5), "mgrs": " ".join(g), "what": [text],
                        "unit": unit or "", "d": dated or doc_d, "doc_d": doc_d, "doc": doc, "kind": kind}
    return sorted(out.values(), key=lambda x: x["d"])


def render_map(ref: dict, points: list, area: Path, out: Path, now: datetime) -> Path:
    """Fill the template with the libraries, the data and the tile extract — one offline HTML file."""
    out.mkdir(parents=True, exist_ok=True)
    html = (HERE / "map_template.html").read_text(encoding="utf-8")
    lib = lambda p: (NM / p).read_text(encoding="utf-8").replace("</script", "<\\/script")   # noqa: E731
    data = dict(ref, points=points, pow=pow_points(), bbox=BBOX, maxzoom=MAXZOOM)
    for k, v in {"/*LEAFLET_CSS*/": (NM / "leaflet/dist/leaflet.css").read_text(),
                 "/*LEAFLET_JS*/": lib("leaflet/dist/leaflet.js"),
                 "/*PMTILES_JS*/": lib("pmtiles/dist/pmtiles.js"),
                 "/*PROTOMAPS_JS*/": lib("protomaps-leaflet/dist/protomaps-leaflet.js"),
                 "/*DATA*/": json.dumps(data, ensure_ascii=False).replace("</", "<\\/"),
                 "/*TILES_B64*/": base64.b64encode(area.read_bytes()).decode()}.items():
        html = html.replace(k, v, 1)
    path = out / f"MAP_{now:%d.%m}.html"
    path.write_text(html, encoding="utf-8")
    print(f"\n→ map {path} ({path.stat().st_size / 1e6:.1f} MB), {len(points)} positions")
    return path


def map_only(out: Path) -> None:
    """Re-render the HTML from the last run's data (atlas.json + cache) — seconds, for work on the map itself."""
    cache = out / ".cache"
    ref = json.loads((REF_DIR / "atlas.json").read_text(encoding="utf-8"))
    points = json.loads((cache / "points.json").read_text(encoding="utf-8"))
    render_map(ref, points, cache / "area.pmtiles", out, datetime.fromisoformat(ref["generated"]))


def render_md(ref: dict, now: datetime) -> str:
    L = [f"# Довідник батальйонів — {ref['window'][0][5:]} .. {now:%d.%m.%Y}", "",
         "Батальйон = його люди (ядро у `cores.json`). Частота — його, якщо ≥60% перехоплень з його людьми "
         "(лише станції в шапці) на ній — його і його люди є хоча б у 10% її перехоплень, інакше вона «епізодична». Райони — з шапок аналітиків, на мапі за OSM. "
         "Числа частот: `наші / Патагонія`, «з людьми» — де станцією записаний хтось із ядра.", ""]
    for b in ref["battalions"]:
        L.append(f"## {b['key']}" + (f" — востаннє {b['last']}" if b["last"] else " — у вікні не звучали"))
        heard = [p for p in b["people"] if p["n"]]
        L.append("**Люди:** " + ("; ".join(
            f"{p['cs']}" + (f" ({', '.join(p['roles'])})" if p["roles"] else "") + f" {p['n']}, {p['first'][5:]}..{p['last'][5:]}"
            for p in heard) or "—"))
        silent = [p["cs"] for p in b["people"] if not p["n"]]
        if silent:
            L.append(f"**Не звучали станцією у вікні:** {', '.join(silent)}")
        L.append("**Частоти:** " + ("; ".join(
            f"{f['f']} ({f['ours']}/{f['pata']}, з людьми {f['with_people']}, {f['first'][5:]}..{f['last'][5:]})"
            for f in b["freqs"]) or "основних не знайдено"))
        if b["visits"]:
            L.append("**Епізодично** (його люди звучать, але <10% перехоплень частоти): " + "; ".join(
                f"{v['f']} ({v['with_people']} з {v['all']}; {', '.join(v['top'])})" for v in b["visits"]))
        L.append("**Райони:** " + ("; ".join(f"{p['name']} {p['n']}" for p in b["places"][:8]) or "—"))
        L.append("")
    if ref["shared"]:
        L.append("## Спільні частоти")
        for s in ref["shared"]:
            L.append(f"- {s['f']} — " + "; ".join(f"{b} {n}" for b, n in s["bats"]))
        L.append("")
    if ref["unplaced"]:
        L.append("## Не знайдено на мапі (з шапок)")
        L.append(", ".join(f"{n} ({c})" for n, c in ref["unplaced"]))
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
