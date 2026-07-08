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
# START-GRACE window: a just-launched bot needs a few seconds to write its state + heartbeat files.
# While the process has been up less than this, health reports OK so the supervisor can't
# restart-loop a bot that is merely still booting (the fail-closed "state absent" check).
START_GRACE_SECONDS=30
# --- Circuit breaker (supervise) config ---
# supervise = the SOLE cron entrypoint. If the bot stays unhealthy across repeated restarts the
# breaker TRIPS: auto-restart halts (stops the restart-loop + Telegram startup-message spam) and one
# alert is sent. After a cooldown it makes a single half-open trial restart, and self-heals when the
# bot comes back. State files live in bot/ and are git-ignored (runtime artifacts, never public).
RESTART_LEDGER="$SCRIPT_DIR/restart_ledger"   # newline epochs, one per restart, pruned to window
BREAKER_FILE="$SCRIPT_DIR/breaker_tripped"    # presence = breaker OPEN (body: ts=/reason=)
ENV_FILE="$SCRIPT_DIR/.env"
BREAKER_THRESHOLD=3      # unhealthy restarts within the window before tripping
BREAKER_WINDOW=1800      # 30 min counting window
BREAKER_COOLDOWN=3600    # 1 h open before a single half-open trial restart

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

_sup_log() { echo "[$(date '+%F %T')] supervise: $*"; }

# Read one KEY from .env WITHOUT sourcing it (never execute .env content). Strips a trailing CR and
# surrounding double-quotes. Absent/empty -> empty string.
_sup_env_get() {
  [ -f "$ENV_FILE" ] || return 0
  local v
  v="$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2-)"
  v="${v%$'\r'}"; v="${v%\"}"; v="${v#\"}"
  printf '%s' "$v"
}

# Best-effort ONE Telegram message to the admin (first ALLOWED_USERS id). Never fails supervise,
# never logs the token (curl output -> /dev/null). Only the still-alive external cron can announce a
# down bot, since a tripped bot never boots to announce itself.
_sup_notify() {
  local msg="$1" token chat
  token="$(_sup_env_get TELEGRAM_BOT_TOKEN)"
  chat="$(_sup_env_get ALLOWED_USERS | cut -d, -f1 | tr -d ' ')"
  [ -n "$token" ] && [ -n "$chat" ] || return 0
  # Token goes in via a -K config on STDIN (a pipe), never on the command line — so it can't be read
  # from /proc/<pid>/cmdline or `ps` by other local accounts. chat_id/text stay in argv (not secret).
  printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" \
    | timeout 15 curl -s -o /dev/null -K - \
        --data-urlencode "chat_id=$chat" \
        --data-urlencode "text=$msg" >/dev/null 2>&1 || true
}

# Prune RESTART_LEDGER to restart timestamps within BREAKER_WINDOW of $1(now); echo the surviving
# count; delete the file when empty; sweep any orphaned mktemp litter. Called on BOTH healthy and
# unhealthy cycles so the count is a true rolling window — a flapping bot (down/up/down) still
# accumulates toward the threshold instead of being zeroed by every healthy sample.
_sup_prune_ledger() {
  local now="$1" n=0 t tmp
  rm -f "$RESTART_LEDGER".* 2>/dev/null
  [ -f "$RESTART_LEDGER" ] || { echo 0; return 0; }
  tmp="$(mktemp "${RESTART_LEDGER}.XXXXXX")" || { echo 0; return 0; }
  while IFS= read -r t; do
    case "$t" in ''|*[!0-9]*) continue ;; esac
    if [ $(( now - t )) -ge 0 ] && [ $(( now - t )) -lt "$BREAKER_WINDOW" ]; then
      echo "$t" >> "$tmp"; n=$(( n + 1 ))
    fi
  done < "$RESTART_LEDGER"
  if [ "$n" -gt 0 ]; then mv -f "$tmp" "$RESTART_LEDGER"; else rm -f "$tmp" "$RESTART_LEDGER"; fi
  echo "$n"
}

bot_is_healthy() {
  if [ ! -f "$PID_FILE" ] || ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    return 1
  fi
  # START-GRACE: while the process has been up less than the grace window, treat it as healthy so a
  # fail-closed "state/heartbeat file absent" check below can't restart-loop a bot that is still
  # booting (state + heartbeat are written a moment after the process appears). Only reached when the
  # PID is alive, so a bot that crashes at boot still fails (kill -0 above) and gets recovered.
  local BOT_PID_G UPTIME_G
  BOT_PID_G="$(cat "$PID_FILE" 2>/dev/null)"
  UPTIME_G="$(ps -o etimes= -p "$BOT_PID_G" 2>/dev/null | tr -d ' ')"
  if [ -n "$UPTIME_G" ] && [ "$UPTIME_G" -lt "$START_GRACE_SECONDS" ] 2>/dev/null; then
    return 0
  fi
  # BUSY = HEALTHY (for the SOFT signals only): a claude turn that is a child of THIS bot pid means
  # active work — it must never be restarted on a heartbeat blip. Precise + read-only (children of the
  # exact bot pid; no fuzzy match, no signals). Captured as a flag so the DEADLOCK check below can
  # still override it — see the reorder note.
  local BOT_PID BUSY
  BOT_PID="$(cat "$PID_FILE" 2>/dev/null)"
  BUSY=0
  if [ -n "$BOT_PID" ] && pgrep -P "$BOT_PID" -f 'claude' >/dev/null 2>&1; then
    BUSY=1
  fi
  # DEADLOCK OVERRIDES BUSY (the load-bearing ordering): a wedged event loop (loop_at ancient) is
  # UNHEALTHY even while a claude child is alive. Otherwise a hung-but-busy bot reports healthy
  # forever and the SOLE cron supervisor could never recover it. Safe because a legitimate long turn
  # keeps loop_at fresh — the async loop_heartbeat ticks every 5s while the loop merely AWAITS the
  # claude subprocess — so this only trips a genuinely stuck loop (>LOOP_STALE_SECONDS), never a
  # healthy turn. Needs the state file to read loop_at; if it's absent we can't assess deadlock, so
  # fall back to the old busy-first behavior. BUSY still suppresses the softer heartbeat-staleness
  # restart inside the checker.
  if [ ! -f "$STATE_FILE" ]; then
    [ "$BUSY" = 1 ] && return 0 || return 1
  fi
  "$VENV_PYTHON" - "$STATE_FILE" "$HEARTBEAT_FILE" "$HEARTBEAT_STALE_SECONDS" "$LOOP_STALE_SECONDS" "$BUSY" <<'PY' >/dev/null
import json, sys, time
from pathlib import Path

state_path, hb_path = sys.argv[1], sys.argv[2]
thread_stale, loop_stale, busy = int(sys.argv[3]), int(sys.argv[4]), sys.argv[5] == "1"
now = time.time()

# Fail-safe: an unreadable state file => can't assess the deadlock signal. Preserve the old
# busy-first behavior (a busy bot stays healthy; an idle one errs toward recovery).
try:
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0 if busy else 1)

# DEADLOCK (loop_at) — checked FIRST and OVERRIDES busy. loop_at is written ONLY by the event loop;
# if it is ancient the loop is wedged even though the process/child lives -> restart.
loop_at = float(state.get("loop_at") or 0)
if loop_at and now - loop_at > loop_stale:
    raise SystemExit(1)

# From here BUSY suppresses the softer signals: an active claude turn is healthy regardless of them.
if busy:
    raise SystemExit(0)

if str(state.get("phase") or "") == "failed":
    raise SystemExit(1)

# PRIMARY liveness: the load-immune OS-thread heartbeat file. Fall back to the (loop-written)
# heartbeat_at only for back-compat with an older bot that predates the thread file.
try:
    thread_hb = float(Path(hb_path).read_text().strip())
except Exception:
    thread_hb = float(state.get("heartbeat_at") or 0)
if not thread_hb or now - thread_hb > thread_stale:
    raise SystemExit(1)   # process/thread genuinely stuck or dead
PY
}

# Kill the MAIN bot process(es) precisely, by cwd + instance identity read from /proc — NOT by a
# cmdline string. This catches duplicates regardless of absolute-vs-relative cmdline (the old
# anchored `pkill` missed relative ones → two bots on one token → getUpdates conflict), while never
# touching a SECONDARY instance (game/helper — they carry a LIL_WORKER_INSTANCE tag) or another
# project's bot.py that lives in a different working directory. Follows "identify by cwd before kill".
# $1 = signal (default TERM). Echoes each pid it signalled.
kill_main_bots() {
  local sig="${1:-TERM}" pid comm cwd inst script a
  for pid in $(pgrep -f 'bot\.py' 2>/dev/null); do
    case "$(cat "/proc/$pid/comm" 2>/dev/null)" in python*) ;; *) continue ;; esac  # python only, not our bash/claude
    # Identify OUR bot by the RESOLVED script path (== $BOT_SCRIPT), NOT by process cwd: the bot's
    # cwd just reflects whoever launched it (watchdog from the parent dir vs a manual run from bot/),
    # so cwd is unreliable. Reconstruct the .../bot.py arg from argv and resolve relative→cwd.
    script=""
    while IFS= read -r -d '' a; do case "$a" in *bot.py) script="$a" ;; esac; done < "/proc/$pid/cmdline" 2>/dev/null
    [ -n "$script" ] || continue
    case "$script" in
      /*) : ;;
      *) cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null)"; script="$cwd/$script" ;;
    esac
    [ "$script" = "$BOT_SCRIPT" ] || continue                 # only OUR bot.py (spares other projects)
    inst="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n 's/^LIL_WORKER_INSTANCE=//p')"
    case "$inst" in ""|lil_worker) kill "-$sig" "$pid" 2>/dev/null && echo "$pid" ;; esac  # main only
  done
}

case "$1" in
  start)
    # Runtime is an AUXILIARY component: the bot answers messages by invoking `claude -p` directly and
    # does NOT route through the runtime socket (bot.py treats a down runtime as a startup warning, not
    # fatal). So a broken runtime must never block the bot from starting — attempt it, warn, continue.
    if ! start_runtime; then
      echo "WARN: auxiliary runtime did not become healthy — starting bot anyway (non-fatal)."
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
    # SINGLE-SUPERVISOR model: the crontab health-check (`health || (sleep 5; health) || restart`) is
    # the ONE supervisor. run.sh deliberately no longer resurrects watchdog.sh here — two supervisors
    # firing `start`/`restart` in the same window caused a TOCTOU double-launch/self-kill race
    # (two bots on one token → getUpdates 409). watchdog.sh is retired; do NOT re-add an auto-start.
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
    # Health reflects the BOT only. The auxiliary runtime is intentionally EXCLUDED: a broken runtime
    # must not mark a perfectly-fine bot UNHEALTHY (the supervisor would then restart the bot for an
    # aux-component failure — a self-outage vector). Runtime state stays visible via `status`/`doctor`.
    if bot_is_healthy; then
      echo "OK"
    else
      echo "UNHEALTHY"
      exit 1
    fi
    ;;

  supervise)
    # SOLE cron supervisor entrypoint (cron: */5 ... timeout 120 bot/run.sh supervise >> log 2>&1).
    # Replaces the old inline `health || (sleep 5; health) || restart` brace-group and adds a circuit
    # breaker so a persistently-broken bot can't restart-loop / startup-message-spam forever. cron runs
    # as an INDEPENDENT root process (not a child of the bot), so calling restart here is correct — it
    # only fires when the bot is already unhealthy, i.e. no live turn to protect.
    now="$(date +%s)"

    _sup_reset_if_healthy() {
      if [ -f "$BREAKER_FILE" ]; then
        _sup_log "bot healthy again -> clearing tripped breaker"
        _sup_notify "lil_worker: recovered — bot healthy again, circuit breaker reset."
        rm -f "$BREAKER_FILE"
      fi
      # Age out old restarts but do NOT wipe: a flapping bot must keep accumulating toward the window
      # threshold across interspersed healthy samples (else it never trips and spams forever).
      _sup_prune_ledger "$now" >/dev/null
    }

    # (a) healthy now -> reset breaker + prune, done
    if bot_is_healthy; then _sup_reset_if_healthy; exit 0; fi
    # (b) debounce a single transient blip
    sleep 5
    if bot_is_healthy; then _sup_reset_if_healthy; exit 0; fi

    # ---- still unhealthy ----
    # (c) breaker already OPEN?
    if [ -f "$BREAKER_FILE" ]; then
      tripped_ts="$(sed -n 's/^ts=//p' "$BREAKER_FILE" 2>/dev/null | head -n1)"
      case "$tripped_ts" in ''|*[!0-9]*) tripped_ts=0 ;; esac
      age=$(( now - tripped_ts ))
      # A missing/corrupt ts (interrupted or disk-full write) or a future ts (clock skew) would else
      # read as "cooldown long expired" and half-open-restart EVERY cycle. Treat any bad ts as a fresh
      # trip: re-arm to now and skip this cycle.
      if [ "$tripped_ts" -eq 0 ] || [ "$age" -lt 0 ]; then
        printf 'ts=%s\nreason=re-armed (bad/absent ts)\n' "$now" > "$BREAKER_FILE"
        _sup_log "breaker marker had bad ts -> re-armed to now, skip restart"
        exit 0
      fi
      if [ "$age" -lt "$BREAKER_COOLDOWN" ]; then
        _sup_log "breaker OPEN (${age}s/${BREAKER_COOLDOWN}s cooldown) -> skip restart"
        exit 0
      fi
      # half-open: exactly ONE trial restart per cooldown; re-arm ts so the next open window starts now
      _sup_log "breaker HALF-OPEN -> single trial restart"
      printf 'ts=%s\nreason=half-open trial\n' "$now" > "$BREAKER_FILE"
      "$SCRIPT_DIR/run.sh" restart
      exit 0
    fi

    # (d) count restarts within the rolling window (prune-and-count in one place)
    count="$(_sup_prune_ledger "$now")"

    if [ "$count" -ge "$BREAKER_THRESHOLD" ]; then
      _sup_log "breaker TRIP: ${count} restarts within ${BREAKER_WINDOW}s -> halting auto-restart"
      printf 'ts=%s\nreason=%s restarts in %ss\n' "$now" "$count" "$BREAKER_WINDOW" > "$BREAKER_FILE"
      _sup_notify "lil_worker: circuit breaker TRIPPED — bot still unhealthy after ${count} restarts in $(( BREAKER_WINDOW/60 ))min. Auto-restart halted, needs a look. Trial retry in $(( BREAKER_COOLDOWN/60 ))min."
      rm -f "$RESTART_LEDGER"
      exit 0
    fi

    # under threshold -> record this restart + do it
    echo "$now" >> "$RESTART_LEDGER"
    _sup_log "unhealthy -> restart #$(( count + 1 )) within window"
    "$SCRIPT_DIR/run.sh" restart
    exit 0
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
    echo "Usage: $0 {start|stop|restart|status|health|supervise|doctor|snapshot|restore-last-good|logs|runtime-health|runtime-sessions}"
    exit 1
    ;;
esac
