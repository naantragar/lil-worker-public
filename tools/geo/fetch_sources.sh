#!/bin/bash
# Rebuild the local geodata sources. Nothing of ours leaves the box: GeoNames is downloaded as the
# whole-country dump (the same file for anyone), OSM places are read from upstream's own tile archive.
set -euo pipefail
cd "$(dirname "$0")/../.."
SRC=knowledge/upstream/geo/src
mkdir -p "$SRC"
[ -x tools/geo/.venv/bin/python ] || { python3 -m venv tools/geo/.venv && tools/geo/.venv/bin/pip -q install pmtiles mapbox-vector-tile; }
timeout 120 curl -sf -o "$SRC/UA.zip" https://download.geonames.org/export/dump/UA.zip
unzip -o -q "$SRC/UA.zip" UA.txt -d "$SRC" && rm "$SRC/UA.zip"
tools/geo/.venv/bin/python tools/geo/osm_places.py
echo "sources ready: $(wc -l < "$SRC/UA.txt") GeoNames rows"
