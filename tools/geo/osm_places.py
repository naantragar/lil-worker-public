#!/usr/bin/env python3
"""Settlements from OUR OWN copy of OpenStreetMap (upstream's PMTiles archive) — no outside request.

    tools/geo/.venv/bin/python tools/geo/osm_places.py [--zoom 12]

Reads the `places` layer of the vector tiles over the front bbox and writes
knowledge/upstream/geo/src/osm_places.json. Zoom 12 already carries every village (measured 12.11.2026:
z12 3339 points, z13 3206, z14 3201 — the deeper zooms only lose edge duplicates).
"""
import argparse
import gzip
import json
import math
from pathlib import Path

import mapbox_vector_tile as mvt
from pmtiles.reader import MmapSource, Reader

REPO = Path(__file__).resolve().parents[2]
PMTILES = "/var/lib/upstream-tiles/www/upstream.pmtiles"
BBOX = (34.6, 46.4, 38.4, 48.6)          # W S E N — Zaporizhzhia front with margin
OUT = REPO / "knowledge" / "upstream" / "geo" / "src" / "osm_places.json"


def tile(lon, lat, z):
    n = 2 ** z
    return int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zoom", type=int, default=12)
    z = ap.parse_args().zoom
    w, s, e, n_ = BBOX
    x0, y0 = tile(w, n_, z)
    x1, y1 = tile(e, s, z)
    out = {}
    with open(PMTILES, "rb") as f:
        r = Reader(MmapSource(f))
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                b = r.get(z, x, y)
                if not b:
                    continue
                try:
                    b = gzip.decompress(b)
                except OSError:
                    pass
                layer = mvt.decode(b).get("places")
                if not layer:
                    continue
                ext = layer["extent"]
                for ft in layer["features"]:
                    g, p = ft["geometry"], ft["properties"]
                    if g["type"] != "Point":
                        continue
                    px, py = g["coordinates"]
                    n = 2 ** z
                    lon = (x + px / ext) / n * 360 - 180
                    yy = y + 1 - py / ext                 # decoded MVT y points up
                    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
                    out[(p.get("name"), round(lat, 3), round(lon, 3))] = {
                        "name": p.get("name"), "uk": p.get("name:uk"), "ru": p.get("name:ru"),
                        "kind": p.get("kind_detail") or p.get("kind"), "lat": lat, "lon": lon}
    OUT.write_text(json.dumps(list(out.values()), ensure_ascii=False))
    print(f"z{z}: {len(out)} places → {OUT}")


if __name__ == "__main__":
    main()
