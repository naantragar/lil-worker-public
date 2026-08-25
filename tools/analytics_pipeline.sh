#!/usr/bin/env bash
# One reporting window, end to end, with a stopwatch on every stage.
#
#   tools/analytics_pipeline.sh '2026-08-21 15:00' '2026-08-22 15:00' ZVIT_22.08.2026 'ЗАЛІЗНИЧНЕ-ЗАГІРНЕ'
#
# Stages: extraction+selection → entity harvest → roster/legend → assembly. Each is timed separately,
# because they cost wildly different amounts (the roster is ONE big Opus call and cannot be split,
# while extraction fans out over batches) and knowing which one dominates is what tells us where to
# spend effort next.
#
# Deliberately NOT `set -e`: a failed late stage must still leave the earlier stages' output on disk
# and must still print the timings. Each stage is guarded and the script reports what it got.
set -uo pipefail

FROM="${1:?from}"; TO="${2:?to}"; STEM="${3:?out stem}"; BAND="${4:-}"
cd ~/lil_worker
O=knowledge/upstream/reports_out
mkdir -p "$O"
TIMINGS="$O/${STEM}_timings.txt"
: > "$TIMINGS"

t0=$(date +%s)
say() { printf '%s\n' "$*" | tee -a "$TIMINGS"; }
stage() {                      # stage <name> <command...>
  local name="$1"; shift
  local s=$(date +%s)
  say "--- $name: старт"
  "$@"
  local rc=$? e=$(date +%s)
  say "--- $name: $(( e - s ))s (код $rc)"
  return $rc
}

say "вікно: $FROM  ->  $TO   (за часом самого перехоплення)"

# Three extraction passes are unioned, so the pool must be wide enough to keep them overlapping —
# otherwise the passes serialise and the run costs three times the wall clock instead of one.
PASSES="${PASSES:-3}"
JOBS="${JOBS:-9}"

stage "1 екстракція+відбір" \
  python3 tools/analytics_run.py --from "$FROM" --to "$TO" \
    --passes "$PASSES" --jobs "$JOBS" --out "$STEM" || exit 1

stage "2 сутності" \
  python3 tools/analytics_entities.py --from "$FROM" --to "$TO" --out "$O/${STEM}_entities.json" || exit 1

stage "3 реєстр і легенда" \
  python3 tools/analytics_sense.py --entities "$O/${STEM}_entities.json" \
    --events "$O/${STEM}_events.json" --out "$O/${STEM}_roster.json"
ROSTER_ARG=()
[ -s "$O/${STEM}_roster.json" ] && ROSTER_ARG=(--roster "$O/${STEM}_roster.json") \
  || say "УВАГА: реєстр не побудовано, звіт буде без нього"

stage "4 збірка" \
  python3 tools/analytics_render.py --from "$FROM" --to "$TO" \
    --events "$O/${STEM}_events.json" "${ROSTER_ARG[@]}" \
    ${BAND:+--band "$BAND"} --out "$O/$STEM" || exit 1

say "=== усього: $(( $(date +%s) - t0 ))s ==="
say "файл: $O/${STEM}.docx"
