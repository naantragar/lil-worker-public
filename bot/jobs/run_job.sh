#!/usr/bin/env bash
# run_job.sh <job_dir> — generic durable job runner for the wake-up feature (v0).
#
# Executes the command from <job_dir>/spec.json, captures combined output to result.txt,
# and records a terminal status (done|failed) + exit code. Launched DETACHED (own session,
# start_new_session) by job_ctl.py so it outlives the claude -p turn that started it.
# It does NOT touch Telegram — bot.py's poller notices the terminal status and notifies.
set -uo pipefail

JOB_DIR="${1:?usage: run_job.sh <job_dir>}"
cd "$JOB_DIR" || exit 97

SPEC="$JOB_DIR/spec.json"
STATUS="$JOB_DIR/status"
RESULT="$JOB_DIR/result.txt"
LOG="$JOB_DIR/run.log"
EXITF="$JOB_DIR/exit_code"

log() { printf '%s  %s\n' "$(date +%FT%T%z)" "$*" >> "$LOG"; }

# Extract fields from spec.json without a JSON dep (values are plain strings we wrote).
jget() { sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\\(.*\\)\".*/\\1/p" "$SPEC" | head -1; }

CMD_B64="$(jget cmd_b64)"
CWD="$(jget cwd)"
[ -n "$CWD" ] || CWD="$HOME"

# Mark the whole subtree as "already durable". tools/hooks/durable_swarm.py converts an inline
# Workflow into a durable job; without this marker the nested claude turn INSIDE a job would convert
# its own swarm again, forever. Set here (not in workflow_job.py) so it covers every durable job.
export KREVETKA_JOB_ID="$(basename "$JOB_DIR")"

echo "$$" > "$JOB_DIR/pid"
echo "running" > "$STATUS"
log "runner start: pid=$$ ppid=$PPID sid=$(ps -o sid= -p $$ 2>/dev/null | tr -d ' ') cwd=$CWD"

if [ -z "$CMD_B64" ]; then
  log "FATAL: no cmd in spec"
  echo "127" > "$EXITF"; echo "failed" > "$STATUS"; exit 1
fi

# Command is stored base64 in the spec (safe for any quoting/newlines).
CMD="$(printf '%s' "$CMD_B64" | base64 -d 2>/dev/null)"

# Run in the requested cwd, combined stdout+stderr → result.txt. `bash -c` so the job can be
# a full pipeline. Time it. Never let a job failure abort this wrapper (we record the code).
START_TS="$(date +%s)"
( cd "$CWD" && bash -c "$CMD" ) > "$RESULT" 2>&1
rc=$?
END_TS="$(date +%s)"
echo "$rc" > "$EXITF"
echo "$((END_TS - START_TS))" > "$JOB_DIR/duration_sec"

if [ "$rc" -eq 0 ]; then
  echo "done" > "$STATUS"
  log "runner done: rc=0 dur=$((END_TS - START_TS))s result_bytes=$(stat -c %s "$RESULT" 2>/dev/null)"
else
  echo "failed" > "$STATUS"
  log "runner failed: rc=$rc dur=$((END_TS - START_TS))s"
fi
exit 0
