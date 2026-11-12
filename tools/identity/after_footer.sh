#!/usr/bin/env bash
# The role register's daily step, run once the footer is pasted (the report's events are FINAL then).
#   tools/identity/after_footer.sh DD.MM
# ingest the day's events (ком склад / накопичувач) → posts named on air → delivery-drone operators
# → one message to the owner in the survey bot about roles that crossed the threshold
# → the battalion atlas (roles in it are fresh now) and its data drop for upstream's «Мапа» (12.11.2026).
set -u
cd "$(dirname "$0")/../.." || exit 1
day="${1:?usage: after_footer.sh DD.MM}"
python3 tools/identity/roles_db.py ingest --day "$day" || exit 1
python3 tools/identity/posady.py || echo "posady: failed, the rest goes on" >&2
python3 tools/identity/bpla_log.py || echo "bpla_log: failed, the rest goes on" >&2
python3 tools/identity/roles_db.py notify
# the atlas reads the whole corpus (a few minutes); a failure here must not fail the footer step
python3 tools/upstream/bat_atlas.py >/dev/null \
  && python3 tools/upstream/bat_atlas.py --export /var/lib/upstream-atlas/atlas.json \
  || echo "bat_atlas: failed, upstream keeps yesterday's atlas" >&2
