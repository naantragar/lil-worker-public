#!/usr/bin/env bash
# A/B one calendar day, our direction only, Opus 5 high vs Opus 5.5 medium, same doctrine (v7)
# and window shape as the live digest. Nothing is sent. Optional FOCUS="HH:MM:SS FREQ" prints both
# models' verdict on that one intercept.
#     tools/alerts/ab_day.sh 2026-10-21 ["15:26:14 411.9630"]
set -u
export PATH="/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/../.." || exit 1
DAY=$1; FOCUS=${2:-}
OUT=knowledge/upstream/alerts/ab_day_$DAY; mkdir -p "$OUT"; rm -f "$OUT/times.txt"
b() {
  local tag=$1 model=$2 effort=$3 t0=$(date +%s)
  python3 tools/alerts/bench.py --day "$DAY" --src ours --model "$model" --effort "$effort" \
    --doctrine v7 --size 14 --overlap 3 --workers 4 --tag "$tag" > "$OUT/$tag.path" 2> "$OUT/$tag.log"
  echo "$tag sec=$(( $(date +%s) - t0 ))" >> "$OUT/times.txt"
}
b abd_o5 claude-opus-5 high &
b abd_o55 claude-opus-5-5 medium &
wait
{
  cat "$OUT/times.txt"
  for t in abd_o5 abd_o55; do
    echo "$t: $(grep -E 'red \+' "$OUT/$t.log" | tail -1) | $(grep -oE '\$[0-9.]+' "$OUT/$t.log" | tail -1)"
  done
  python3 - "$(tail -1 "$OUT/abd_o5.path")" "$(tail -1 "$OUT/abd_o55.path")" "$FOCUS" <<'PY'
import json, sys, collections
A = {x["id"]: x for x in json.load(open(sys.argv[1]))["alerts"]}
B = {x["id"]: x for x in json.load(open(sys.argv[2]))["alerts"]}
both = set(A) & set(B)
print(f"verdicts: opus5 {len(A)}, opus5.5 {len(B)}, common {len(both)}")
for (x, y), n in sorted(collections.Counter((A[i]["level"], B[i]["level"]) for i in both).items(), key=lambda kv: -kv[1]):
    print(f"  opus5={x:7} opus5.5={y:7} {n}")
for name, X, Y in (("opus5", A, B), ("opus5.5", B, A)):
    for i, a in sorted(X.items(), key=lambda kv: str(kv[1].get("ts", ""))):  # ts is int in some runs, str in others
        if a["level"] == "red":
            other = Y.get(i, {}).get("level", "-")
            print(f"  RED {name}: {a['hhmmss']} {a['freq']} [{a['class']}] other={other} | {a['why'][:160]}")
if sys.argv[3]:
    hh, fr = sys.argv[3].split()
    for name, X in (("opus5", A), ("opus5.5", B)):
        hits = [a for a in X.values() if a.get("hhmmss") == hh and str(a.get("freq", "")).startswith(fr[:7])]
        for a in hits:
            print(f"FOCUS {name}: {a['level']} [{a['class']}] {a['why']}")
        if not hits:
            print(f"FOCUS {name}: no verdict on that intercept")
PY
} > "$OUT/summary.txt" 2>&1
cat "$OUT/summary.txt"
true
