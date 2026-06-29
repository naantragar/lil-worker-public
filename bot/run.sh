#!/bin/bash
# lil_worker bot process manager
# Usage: ./run.sh {start|stop|restart|status|logs}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_SCRIPT="$SCRIPT_DIR/bot.py"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
PID_FILE="$SCRIPT_DIR/lil_worker.pid"
LOG_FILE="$SCRIPT_DIR/lil_worker.log"
STATE_FILE="$SCRIPT_DIR/bot_runtime_state.json"
# CODEX: local persistent runtime layer for shell/session reuse.
RUNTIME_SCRIPT="$SCRIPT_DIR/runtime_daemon.py"
RUNTIME_CTL="$SCRIPT_DIR/runtime_ctl.py"
RUNTIME_PID="$SCRIPT_DIR/runtime.pid"
RUNTIME_LOG="$SCRIPT_DIR/runtime.log"
RUNTIME_SOCKET="$SCRIPT_DIR/.runtime.sock"
RUNTIME_TMUX_SESSION="lil_worker_runtime"
LAST_GOOD_DIR="$SCRIPT_DIR/backups/last_good"
HEARTBEAT_STALE_SECONDS=20

ensure_last_good_dir() {
  mkdir -p "$LAST_GOOD_DIR"
}

snapshot_last_good() {
  ensure_last_good_dir
  cp "$BOT_SCRIPT" "$LAST_GOOD_DIR/bot.py"
  cp "$0" "$LAST_GOOD_DIR/run.sh"
  cp "$SCRIPT_DIR/watchdog.sh" "$LAST_GOOD_DIR/watchdog.sh" 2>/dev/null || true
  cp "$SCRIPT_DIR/validate.sh" "$LAST_GOOD_DIR/validate.sh" 2>/dev/null || true
  cp "$SCRIPT_DIR/runtime_daemon.py" "$LAST_GOOD_DIR/runtime_daemon.py" 2>/dev/null || true
  cp "$SCRIPT_DIR/runtime_ctl.py" "$LAST_GOOD_DIR/runtime_ctl.py" 2>/dev/null || true
}

restore_last_good() {
  if [ ! -f "$LAST_GOOD_DIR/bot.py" ] || [ ! -f "$LAST_GOOD_DIR/run.sh" ]; then
    echo "Rollback unavailable: no last-known-good snapshot"
    return 1
  fi
  cp "$LAST_GOOD_DIR/bot.py" "$BOT_SCRIPT"
  cp "$LAST_GOOD_DIR/run.sh" "$0"
  [ -f "$LAST_GOOD_DIR/watchdog.sh" ] && cp "$LAST_GOOD_DIR/watchdog.sh" "$SCRIPT_DIR/watchdog.sh"
  [ -f "$LAST_GOOD_DIR/validate.sh" ] && cp "$LAST_GOOD_DIR/validate.sh" "$SCRIPT_DIR/validate.sh"
  [ -f "$LAST_GOOD_DIR/runtime_daemon.py" ] && cp "$LAST_GOOD_DIR/runtime_daemon.py" "$SCRIPT_DIR/runtime_daemon.py"
  [ -f "$LAST_GOOD_DIR/runtime_ctl.py" ] && cp "$LAST_GOOD_DIR/runtime_ctl.py" "$SCRIPT_DIR/runtime_ctl.py"
  chmod +x "$0" "$SCRIPT_DIR/watchdog.sh" "$SCRIPT_DIR/validate.sh" 2>/dev/null || true
}

# CODEX: start the auxiliary runtime separately from the main bot process.
start_runtime() {
  rm -f "$RUNTIME_SOCKET"
  if runtime_is_healthy; then
    echo "Runtime already running (PID $(cat "$RUNTIME_PID" 2>/dev/null || echo '?'))"
    return 0
  fi
  if [ -f "$RUNTIME_PID" ] && kill -0 "$(cat "$RUNTIME_PID")" 2>/dev/null; then
    echo "Runtime already running (PID $(cat "$RUNTIME_PID"))"
    return 0
  fi
  tmux kill-session -t "$RUNTIME_TMUX_SESSION" 2>/dev/null || true
  tmux new-session -d -s "$RUNTIME_TMUX_SESSION" \
    "cd '$SCRIPT_DIR' && exec env PYTHONUNBUFFERED=1 '$VENV_PYTHON' '$RUNTIME_SCRIPT' >> '$RUNTIME_LOG' 2>&1"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if runtime_is_healthy; then
      echo "Runtime started (PID $(cat "$RUNTIME_PID"))"
      return 0
    fi
    sleep 0.5
  done
  echo "Runtime failed to become healthy."
  return 1
}

# CODEX: stop the auxiliary runtime separately from the main bot process.
stop_runtime() {
  STOPPED=false
  tmux kill-session -t "$RUNTIME_TMUX_SESSION" 2>/dev/null && STOPPED=true
  if [ -f "$RUNTIME_PID" ] && kill -0 "$(cat "$RUNTIME_PID")" 2>/dev/null; then
    kill "$(cat "$RUNTIME_PID")"
    STOPPED=true
  fi
  rm -f "$RUNTIME_PID"
  rm -f "$RUNTIME_SOCKET"
  pkill -f "$VENV_PYTHON $RUNTIME_SCRIPT" 2>/dev/null && STOPPED=true
  sleep 0.3
  pkill -9 -f "$VENV_PYTHON $RUNTIME_SCRIPT" 2>/dev/null
  if $STOPPED; then
    echo "Runtime stopped."
  else
    echo "Runtime not running."
  fi
}

runtime_is_healthy() {
  if tmux has-session -t "$RUNTIME_TMUX_SESSION" 2>/dev/null; then
    :
  elif [ ! -f "$RUNTIME_PID" ] || ! kill -0 "$(cat "$RUNTIME_PID")" 2>/dev/null; then
    return 1
  fi
  "$VENV_PYTHON" "$RUNTIME_CTL" health >/dev/null 2>&1
}

bot_is_healthy() {
  if [ ! -f "$PID_FILE" ] || ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    return 1
  fi
  if [ ! -f "$STATE_FILE" ]; then
    return 1
  fi
  "$VENV_PYTHON" - "$STATE_FILE" "$HEARTBEAT_STALE_SECONDS" <<'PY' >/dev/null
import json, sys, time
from pathlib import Path
state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
stale = int(sys.argv[2])
now = time.time()
heartbeat_at = float(state.get("heartbeat_at") or 0)
phase = str(state.get("phase") or "")
if not heartbeat_at:
    raise SystemExit(1)
if now - heartbeat_at > stale:
    raise SystemExit(1)
if phase in {"failed"}:
    raise SystemExit(1)
PY
}

case "$1" in
  start)
    if ! start_runtime; then
      echo "Start aborted: runtime did not become healthy."
      exit 1
    fi
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Already running (PID $(cat "$PID_FILE"))"
      exit 1
    fi
    # Clean up any ghost processes before starting
    pkill -f "$VENV_PYTHON $BOT_SCRIPT$" 2>/dev/null
    sleep 0.3
    pkill -9 -f "$VENV_PYTHON $BOT_SCRIPT$" 2>/dev/null
    nohup env PYTHONUNBUFFERED=1 "$VENV_PYTHON" "$BOT_SCRIPT" >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Started (PID $!)"
    snapshot_last_good
    # Auto-start watchdog if available and not running
    WATCHDOG="$SCRIPT_DIR/watchdog.sh"
    if [ -x "$WATCHDOG" ]; then
      "$WATCHDOG" start 2>/dev/null
    fi
    ;;

  stop)
    stop_runtime
    STOPPED=false
    # Kill by PID file
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      kill "$(cat "$PID_FILE")"
      STOPPED=true
    fi
    rm -f "$PID_FILE"
    # Kill any remaining instances (prevents ghost processes)
    pkill -f "$VENV_PYTHON $BOT_SCRIPT$" 2>/dev/null && STOPPED=true
    sleep 0.5
    # Force kill if still alive
    pkill -9 -f "$VENV_PYTHON $BOT_SCRIPT$" 2>/dev/null
    if $STOPPED; then
      echo "Stopped."
    else
      echo "Not running."
    fi
    ;;

  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;

  status)
    # CODEX: report auxiliary runtime state explicitly so it does not get
    # confused with the main bot/Claude process state.
    if runtime_is_healthy; then
      echo "Runtime: healthy (PID $(cat "$RUNTIME_PID"))"
    elif [ -f "$RUNTIME_PID" ] && kill -0 "$(cat "$RUNTIME_PID")" 2>/dev/null; then
      echo "Runtime: process alive but unhealthy (PID $(cat "$RUNTIME_PID"))"
    else
      echo "Runtime: not running."
    fi
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Running (PID $(cat "$PID_FILE"))"
    else
      # Fallback: check for running process even without PID file
      LIVE_PID=$(pgrep -f "$VENV_PYTHON $BOT_SCRIPT$" 2>/dev/null | head -1)
      if [ -n "$LIVE_PID" ]; then
        echo "$LIVE_PID" > "$PID_FILE"
        echo "Running (PID $LIVE_PID, PID file recovered)"
      else
        echo "Not running."
      fi
    fi
    if [ -f "$STATE_FILE" ]; then
      echo "State file: $STATE_FILE"
    fi
    ;;

  health)
    if runtime_is_healthy && bot_is_healthy; then
      echo "OK"
    else
      echo "UNHEALTHY"
      exit 1
    fi
    ;;

  doctor)
    "$0" status
    echo "---"
    "$0" health || true
    echo "--- runtime-health ---"
    "$VENV_PYTHON" "$RUNTIME_CTL" health 2>/dev/null || echo "runtime health unavailable"
    echo "--- state ---"
    cat "$STATE_FILE" 2>/dev/null || echo "no state file"
    ;;

  snapshot)
    snapshot_last_good
    echo "Snapshot saved to $LAST_GOOD_DIR"
    ;;

  restore-last-good)
    restore_last_good
    echo "Restored from $LAST_GOOD_DIR"
    ;;

  runtime-health)
    # CODEX: runtime-specific health command.
    "$VENV_PYTHON" "$RUNTIME_CTL" health
    ;;

  runtime-sessions)
    # CODEX: runtime-specific session listing.
    "$VENV_PYTHON" "$RUNTIME_CTL" sessions
    ;;

  logs)
    tail -f "$LOG_FILE"
    ;;

  *)
    echo "Usage: $0 {start|stop|restart|status|health|doctor|snapshot|restore-last-good|logs|runtime-health|runtime-sessions}"
    exit 1
    ;;
esac
