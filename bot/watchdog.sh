#!/bin/bash
# lil_worker watchdog — RETIRED (single-supervisor model).
# The crontab health-check (`health || (sleep 5; health) || restart`, every 10 min) is now the ONE
# supervisor. Running this alongside cron re-introduces the TOCTOU double-launch/self-kill race that
# put two bots on one token. Do NOT start it. Kept only for reference/manual emergency use.
#
# lil_worker watchdog — keeps the bot alive
# Checks every 5 minutes if bot is running, restarts if dead.
# Runs as a background process via nohup — no cron/systemd needed.
#
# Usage:
#   ./watchdog.sh start   — start watchdog in background
#   ./watchdog.sh stop    — stop watchdog
#   ./watchdog.sh status  — check if watchdog is running

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/watchdog.pid"
LOG_FILE="$SCRIPT_DIR/lil_worker.log"
CHECK_INTERVAL=300  # 5 minutes

case "$1" in
  start|_run)
    # RETIRED: hard-refuse to launch. Single-supervisor model — the crontab health-check is the ONE
    # supervisor. Running this alongside cron re-opens the TOCTOU double-launch/self-kill race (two
    # bots on one token → getUpdates 409). Left non-functional on purpose so no reinstall/migration/
    # stray call can resurrect a second supervisor. Emergency manual recovery = `run.sh restart`.
    echo "watchdog.sh is RETIRED and will not start (single-supervisor model — cron is the supervisor)." >&2
    exit 1
    ;;

  _legacy_start_disabled)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Watchdog already running (PID $(cat "$PID_FILE"))"
      exit 0
    fi

    # Clean up any old watchdog processes
    pkill -f "watchdog.sh _run" 2>/dev/null
    sleep 0.3

    nohup "$0" _run >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Watchdog started (PID $!)"
    ;;

  stop)
    if [ -f "$PID_FILE" ]; then
      kill "$(cat "$PID_FILE")" 2>/dev/null
      rm -f "$PID_FILE"
    fi
    pkill -f "watchdog.sh _run" 2>/dev/null
    echo "Watchdog stopped."
    ;;

  status)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Watchdog running (PID $(cat "$PID_FILE"))"
    else
      # Fallback: check for process
      WD_PID=$(pgrep -f "watchdog.sh _run" 2>/dev/null | head -1)
      if [ -n "$WD_PID" ]; then
        echo "$WD_PID" > "$PID_FILE"
        echo "Watchdog running (PID $WD_PID, PID file recovered)"
      else
        echo "Watchdog not running."
      fi
    fi
    ;;

  _run)
    # Internal: the actual loop (called via nohup)
    echo "$(date '+%Y-%m-%d %H:%M:%S') [WATCHDOG] Started, checking every ${CHECK_INTERVAL}s"
    while true; do
      sleep "$CHECK_INTERVAL"
      if ! "$SCRIPT_DIR/run.sh" health >/dev/null 2>&1; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [WATCHDOG] Bot unhealthy, restarting..."
        "$SCRIPT_DIR/run.sh" start
        echo "$(date '+%Y-%m-%d %H:%M:%S') [WATCHDOG] Restart issued"
      fi
    done
    ;;

  *)
    echo "Usage: $0 {start|stop|status}"
    exit 1
    ;;
esac
