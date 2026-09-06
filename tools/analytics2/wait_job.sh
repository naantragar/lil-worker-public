#!/usr/bin/env bash
# Emit one line when a durable job leaves the running state, then exit.
#
#     tools/analytics2/wait_job.sh <job-id>
#
# For Monitor: a single notification, no unbounded tail. Exists because the report re-runs and the
# footer paste have to happen in order - handing the owner a .docx built by the PREVIOUS run is the
# one mistake this step can make.
set -uo pipefail
cd "$(dirname "$0")/../.."
JOB="${1:?job id}"
while true; do
    line=$(python3 bot/job_ctl.py list 2>/dev/null | grep -- "$JOB")
    if [ -z "$line" ]; then
        echo "джоба $JOB зникла зі списку"
        exit 0
    fi
    if ! printf '%s' "$line" | grep -q running; then
        echo "ЗАВЕРШЕНО: $line"
        exit 0
    fi
    sleep 20
done
