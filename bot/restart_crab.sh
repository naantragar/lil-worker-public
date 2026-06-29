#!/bin/bash
# Detached targeted restart of ONLY the crab (default) lil_worker instance.
# Does NOT touch the shared runtime daemon or the --instance-tag game instance.
set +e
sleep 3                      # let the confirmation message flush first
DIR=~/lil_worker/bot
PY="$DIR/.venv/bin/python"
BOT="$DIR/bot.py"
PAT="$PY $BOT"
cd "$DIR" || exit 1

[ -f lil_worker.pid ] && kill "$(cat lil_worker.pid)" 2>/dev/null
pkill -f "${PAT}\$" 2>/dev/null         # anchored: game instance (--instance-tag) survives
sleep 1
pkill -9 -f "${PAT}\$" 2>/dev/null
sleep 0.3

start_bot() {
  nohup env PYTHONUNBUFFERED=1 "$PY" "$BOT" >> lil_worker.log 2>&1 &
  echo $! > lil_worker.pid
}

start_bot
NEWPID=$(cat lil_worker.pid)
sleep 5
if ! kill -0 "$NEWPID" 2>/dev/null; then
  echo "$(date) [restart_crab] new bot died on startup -> rolling back to last_good" >> lil_worker.log
  cp backups/last_good/bot.py bot.py 2>/dev/null
  start_bot
fi
