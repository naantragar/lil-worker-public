#!/usr/bin/env bash
# Fold the newest report day into the dossier. Seconds, no model call.
#
#     tools/callsign_roles/daily.sh [YYYY-MM-DD HH:MM]
#
# Run it after a report finishes. The dossier is rebuilt from every ZVIT_*_events.json on disk
# rather than appended to, deliberately: the fold is cheap and idempotent, so a re-run after a
# report is REGENERATED (as 05.09 was today) picks up the corrected lines instead of carrying both
# the old and the new observation forever.
#
# The air counter needs the corpus, so this is the only slow part - about six minutes for a 14-day
# window. Pass --air-days 0 to skip it when only the relations matter.
set -euo pipefail
cd "$(dirname "$0")/../.."

TO="${1:-}"
ARGS=()
[ -n "$TO" ] && ARGS+=(--air-to "$TO")

echo "== досьє позивних: згортання =="
python3 tools/callsign_roles/dossier.py "${ARGS[@]}"

echo
echo "щоб подивитися картку:  python3 tools/callsign_roles/dossier.py --show ПОЗИВНИЙ"
