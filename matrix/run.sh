#!/usr/bin/env bash
# matrix-bridge bot process manager (bot only; homeserver is docker compose).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"; PY="$DIR/bot/.venv/bin/python"; APP="$DIR/bot/matrix_bridge.py"
case "${1:-}" in
  start)   nohup "$PY" "$APP" >>"$DIR/logs/matrix-bridge.log" 2>&1 & echo $! > "$DIR/bot/matrix-bridge.pid"; echo "started $(cat "$DIR/bot/matrix-bridge.pid")";;
  stop)    [ -f "$DIR/bot/matrix-bridge.pid" ] && kill "$(cat "$DIR/bot/matrix-bridge.pid")" 2>/dev/null && rm -f "$DIR/bot/matrix-bridge.pid" && echo stopped || echo "not running";;
  status)  systemctl is-active matrix-bridge-bot 2>/dev/null || { [ -f "$DIR/bot/matrix-bridge.pid" ] && kill -0 "$(cat "$DIR/bot/matrix-bridge.pid")" 2>/dev/null && echo "running (nohup)" || echo "stopped"; };;
  restart) systemctl restart matrix-bridge-bot 2>/dev/null || { "$0" stop || true; "$0" start; };;
  *) echo "usage: $0 {start|stop|status|restart}"; exit 1;;
esac
