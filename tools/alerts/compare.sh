#!/usr/bin/env bash
# Run the alerting benchmark for the three candidate models over one day and score them together.
#
#     tools/alerts/compare.sh 2026-09-27
#
# Sequential on purpose: each run is already parallel inside, and three at once would just queue.
set -u
DAY="${1:-2026-09-27}"
cd "$(dirname "$0")/../.." || exit 1
RUNS="knowledge/upstream/alerts/runs"

for spec in "claude-haiku-4-5:" "claude-sonnet-5:" "claude-opus-5:high"; do
  model="${spec%%:*}"; effort="${spec##*:}"
  echo "=== $model ${effort:+effort $effort} ==="
  if [ -n "$effort" ]; then
    python3 tools/alerts/bench.py --day "$DAY" --model "$model" --effort "$effort" --workers 6
  else
    python3 tools/alerts/bench.py --day "$DAY" --model "$model" --workers 6
  fi
done

echo
echo "=== ОЦІНКА ==="
# shellcheck disable=SC2086
python3 tools/alerts/score.py \
  "$RUNS/${DAY}_claude-haiku-4-5.json" \
  "$RUNS/${DAY}_claude-sonnet-5.json" \
  "$RUNS/${DAY}_claude-opus-5_high.json"
