#!/usr/bin/env bash
# restart_bridge.sh — the ONLY sanctioned way to restart the Matrix door.
#
# Why this exists: on 2026-07-30 a plain `systemctl restart matrix-bridge-bot` wiped a live 10-agent
# swarm. The unit runs with KillMode=control-group, and durable jobs used to live inside its cgroup,
# so the restart killed them (setsid escapes the process TREE, not the CGROUP).
#
# job_ctl now launches runners in their own transient scope, so a restart is no longer fatal — but a
# restart still drops any in-flight *conversation* turn, and this guard also catches the case where a
# job was launched before the fix (or through the popen fallback). It refuses while jobs are active.
#
# The old safety check `ps --ppid <MainPID>` was WORTHLESS here: a detached job is not a child of the
# bridge (it reparents to init), so that check can never see it. Ask job_ctl instead.
set -uo pipefail

UNIT="matrix-bridge-bot"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOB_CTL="$REPO/bot/job_ctl.py"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

# reap first: a job whose runner is already dead must not be counted as active
python3 "$JOB_CTL" reap >/dev/null 2>&1

ACTIVE="$(python3 - "$REPO" <<'PY'
import json, sys
from pathlib import Path
jobs = Path(sys.argv[1]) / "bot" / "jobs"
out = []
if jobs.is_dir():
    for d in sorted(jobs.iterdir()):
        if not (d / "spec.json").exists():
            continue
        try:
            st = (d / "status").read_text().strip()
        except OSError:
            continue
        if st in ("queued", "running"):
            try:
                label = json.loads((d / "spec.json").read_text()).get("label", "")
            except Exception:
                label = ""
            out.append(f"{d.name} [{st}] {label}")
print("\n".join(out))
PY
)"

if [ -n "$ACTIVE" ] && [ "$FORCE" -eq 0 ]; then
    echo "REFUSING to restart $UNIT — active durable job(s):"
    echo "$ACTIVE" | sed 's/^/  · /'
    echo
    echo "Wait for them, cancel one (python3 bot/job_ctl.py cancel <id> --reason '…'),"
    echo "or override with: $0 --force"
    exit 3
fi

[ -n "$ACTIVE" ] && echo "WARNING: restarting with active job(s) because --force was given:" && echo "$ACTIVE" | sed 's/^/  · /'

systemctl restart "$UNIT" || exit 1
sleep 3
systemctl is-active --quiet "$UNIT" && echo "$UNIT restarted OK" || { echo "$UNIT FAILED to come back"; exit 1; }
tail -n 3 "$REPO/matrix/logs/matrix-bridge.log" 2>/dev/null
