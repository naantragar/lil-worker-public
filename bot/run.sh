#!/bin/bash
# lil_worker bot process manager
# Usage: ./run.sh {start|stop|restart|status|logs}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_SCRIPT="$SCRIPT_DIR/bot.py"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
PID_FILE="$SCRIPT_DIR/lil_worker.pid"
LOG_FILE="$SCRIPT_DIR/lil_worker.log"
STATE_FILE="$SCRIPT_DIR/bot_runtime_state.json"
HEARTBEAT_FILE="$SCRIPT_DIR/bot_heartbeat"
# CODEX: local persistent runtime layer for shell/session reuse.
RUNTIME_SCRIPT="$SCRIPT_DIR/runtime_daemon.py"
RUNTIME_CTL="$SCRIPT_DIR/runtime_ctl.py"
RUNTIME_PID="$SCRIPT_DIR/runtime.pid"
RUNTIME_LOG="$SCRIPT_DIR/runtime.log"
RUNTIME_SOCKET="$SCRIPT_DIR/.runtime.sock"
RUNTIME_TMUX_SESSION="lil_worker_runtime"
LAST_GOOD_DIR="$SCRIPT_DIR/backups/last_good"
# PRIMARY liveness window: the OS-thread heartbeat (bot_heartbeat) is load-immune, so a generous
# 90s only trips when the process/thread is genuinely stuck or dead — a busy event loop no longer
# false-restarts the bot mid-work (the old 20s footgun).
HEARTBEAT_STALE_SECONDS=90
# DEADLOCK window: the async loop_at must tick within this generous window. Long legitimate turns
# keep the loop turning between bursts, so only a truly wedged loop (~10 min silent) restarts.
LOOP_STALE_SECONDS=600

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
  # BUSY = HEALTHY: if a claude turn (a child process of THIS bot pid) is actively running, never
  # report unhealthy. This is the definitive guard against restarting the bot mid-work, independent
  # of any heartbeat tuning, and resolves the starvation-vs-deadlock ambiguity for long turns.
  # Read-only + precise (children of the exact bot pid only — no fuzzy match, no signals sent).
  local BOT_PID
  BOT_PID="$(cat "$PID_FILE" 2>/dev/null)"
  if [ -n "$BOT_PID" ] && pgrep -P "$BOT_PID" -f 'claude' >/dev/null 2>&1; then
    return 0
  fi
  if [ ! -f "$STATE_FILE" ]; then
    return 1
  fi
  "$VENV_PYTHON" - "$STATE_FILE" "$HEARTBEAT_FILE" "$HEARTBEAT_STALE_SECONDS" "$LOOP_STALE_SECONDS" <<'PY' >/dev/null
import json, sys, time
from pathlib import Path

state_path, hb_path = sys.argv[1], sys.argv[2]
thread_stale, loop_stale = int(sys.argv[3]), int(sys.argv[4])
now = time.time()

# Fail-safe: an unreadable/absent state file => treat as unhealthy (err toward recovery).
try:
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)

if str(state.get("phase") or "") == "failed":
    raise SystemExit(1)

# PRIMARY: the load-immune OS-thread heartbeat file. Fall back to the (loop-written) heartbeat_at
# only for back-compat with an older bot that predates the thread file.
try:
    thread_hb = float(Path(hb_path).read_text().strip())
except Exception:
    thread_hb = float(state.get("heartbeat_at") or 0)
if not thread_hb or now - thread_hb > thread_stale:
    raise SystemExit(1)   # process/thread genuinely stuck or dead

# SECONDARY (deadlock): loop_at is written ONLY by the event loop. If it is ancient while the thread
# heartbeat is fresh, the async loop is wedged even though the process lives -> restart. Generous
# window so heavy but healthy turns (loop still ticks between CPU bursts) never trip it.
loop_at = float(state.get("loop_at") or 0)
if loop_at and now - loop_at > loop_stale:
    raise SystemExit(1)
PY
}

# Kill the MAIN bot process(es) precisely, by cwd + instance identity read from /proc — NOT by a
# cmdline string. This catches duplicates regardless of absolute-vs-relative cmdline (the old
# anchored `pkill` missed relative ones → two bots on one token → getUpdates conflict), while never
# touching a SECONDARY instance (game/helper — they carry a LIL_WORKER_INSTANCE tag) or another
# project's bot.py that lives in a different working directory. Follows "identify by cwd before kill".
# $1 = signal (default TERM). Echoes each pid it signalled.
kill_main_bots() {
  local sig="${1:-TERM}" pid comm cwd inst
  for pid in $(pgrep -f 'bot\.py' 2>/dev/null); do
    comm="$(cat "/proc/$pid/comm" 2>/dev/null)"
    case "$comm" in python*) ;; *) continue ;; esac          # only python interpreters, not our bash/claude
    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null)"
    [ "$cwd" = "$SCRIPT_DIR" ] || continue                    # only THIS bot dir (spares other projects)
    inst="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n 's/^LIL_WORKER_INSTANCE=//p')"
    case "$inst" in ""|lil_worker) kill "-$sig" "$pid" 2>/dev/null && echo "$pid" ;; esac  # main only
  done
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
    # Clean up any ghost MAIN-bot processes before starting (cwd+instance precise; catches the
    # relative-cmdline duplicates the old anchored pkill missed; leaves game/helper/other projects).
    kill_main_bots TERM >/dev/null
    sleep 0.3
    kill_main_bots KILL >/dev/null
    # Start the MAIN bot with a CLEAN env: unset any inherited instance/token vars so bot/.env is
    # authoritative. Prevents a start invoked from a secondary/other-project context from bringing
    # the main bot up on the wrong token (the cross-context env-leak class of bug).
    nohup env -u TELEGRAM_BOT_TOKEN -u ALLOWED_USERS -u CLAUDE_MODEL -u CODEX_MODEL \
      -u CODEX_SANDBOX_MODE -u CODEX_APPROVAL_POLICY -u OPENAI_API_KEY -u OPENAI_VOICE_MODEL \
      -u LIL_WORKER_INSTANCE -u LIL_WORKER_DATA_DIR -u LIL_WORKER_BOT_CWD -u LIL_WORKER_EFFORT \
      PYTHONUNBUFFERED=1 "$VENV_PYTHON" "$BOT_SCRIPT" >> "$LOG_FILE" 2>&1 &
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
    # Kill the main bot precisely by cwd + instance (catches relative-path duplicates the old
    # anchored pkill missed; never touches secondary instances or other projects' bot.py).
    KILLED="$(kill_main_bots TERM)"
    [ -n "$KILLED" ] && STOPPED=true
    rm -f "$PID_FILE"
    sleep 0.5
    # Force-kill any survivors.
    kill_main_bots KILL >/dev/null
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
