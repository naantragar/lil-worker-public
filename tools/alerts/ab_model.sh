#!/usr/bin/env bash
# A/B for the red digest's model (24.10.2026): Opus 5 high (live) vs Opus 5.5 medium (candidate).
#   1. golden day 27.09 - both models, production window shape (14/3), all sources so the whole
#      hand-written ground truth applies; scored with score.py.
#   2. today's live window - 5.5 only, the last 29 h (covers the 15:10 run's 24 h), compared per
#      intercept id against today's Opus 5 run file. Only ids present in both are compared.
# Output: knowledge/upstream/alerts/ab_55/ (logs + summary.txt). Sends nothing anywhere.
set -u
export PATH="/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/../.." || exit 1
OUT=knowledge/upstream/alerts/ab_55; mkdir -p "$OUT"
BASE=knowledge/upstream/alerts/runs/last24h_v7_claude-opus-5_high_daily1500.json
cp "$BASE" "$OUT/today_opus5_high.json"

b() {  # tag model effort extra-args...
  local tag=$1 model=$2 effort=$3; shift 3
  local t0=$(date +%s)
  python3 tools/alerts/bench.py --model "$model" --effort "$effort" --doctrine v7 \
    --size 14 --overlap 3 --workers 4 --tag "$tag" "$@" > "$OUT/$tag.path" 2> "$OUT/$tag.log"
  echo "$tag sec=$(( $(date +%s) - t0 ))" >> "$OUT/times.txt"
}
rm -f "$OUT/times.txt"
b ab_gold_o5  claude-opus-5   high   --day 2026-09-27 --src all &
b ab_gold_o55 claude-opus-5-5 medium --day 2026-09-27 --src all &
b ab_today_o55 claude-opus-5-5 medium --hours 29 --src ours &
wait

{
  echo "== time"; cat "$OUT/times.txt"
  for t in ab_gold_o5 ab_gold_o55 ab_today_o55; do
    echo "== $t: $(grep -E 'red \+' "$OUT/$t.log" | tail -1) | $(grep -oE '\$[0-9.]+' "$OUT/$t.log" | tail -1)"
  done
  for t in ab_gold_o5 ab_gold_o55; do
    echo "== score $t"; python3 tools/alerts/score.py "$(tail -1 "$OUT/$t.path")" 2>&1
  done
  echo "== today, per intercept (ids in both runs)"
  python3 - "$OUT/today_opus5_high.json" "$(tail -1 "$OUT/ab_today_o55.path")" <<'PY'
import json, sys, collections
a = {str(x["id"]): x.get("level") for x in json.load(open(sys.argv[1]))["alerts"]}
b = {str(x["id"]): x.get("level") for x in json.load(open(sys.argv[2]))["alerts"]}
both = set(a) & set(b)
print(f"opus5 {len(a)} verdicts, opus5.5 {len(b)}, common {len(both)}")
c = collections.Counter((a[i], b[i]) for i in both)
for (x, y), n in sorted(c.items(), key=lambda kv: -kv[1]):
    print(f"  opus5={x:7} opus5.5={y:7} {n}")
for i in sorted(both):
    if (a[i] == "red") != (b[i] == "red"):
        print(f"  RED DIFF id={i} opus5={a[i]} opus5.5={b[i]}")
PY
} > "$OUT/summary.txt" 2>&1
cat "$OUT/summary.txt"
true
