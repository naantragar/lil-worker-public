#!/usr/bin/env bash
# setup.sh — interactive installer for the Matrix/Element door.
#
# Goal: the operator answers a handful of questions and ends up with a RUNNING bridge. Everything
# that can be derived, discovered or fetched is done here instead of being left as "now edit this
# file": the access token is obtained from a password, rooms are discovered and invites accepted,
# paths are derived from this script's location, and the systemd unit is generated with real paths.
#
#   bash matrix/setup.sh
#
# Re-running is safe: existing .env values are offered as defaults.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
ENV_FILE="$HERE/.env"
VENV="$HERE/bot/.venv"
PY="$VENV/bin/python"
API="$HERE/bot/setup_api.py"
UNIT_NAME="matrix-bridge-bot"
UNIT_PATH="/etc/systemd/system/$UNIT_NAME.service"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

ask() {  # ask "prompt" "default" -> echoes answer
  local prompt="$1" def="${2:-}" ans
  if [ -n "$def" ]; then read -r -p "$prompt [$def]: " ans || true; else read -r -p "$prompt: " ans || true; fi
  printf '%s' "${ans:-$def}"
}
ask_secret() {  # ask_secret "prompt" -> echoes answer, no echo on screen
  local prompt="$1" ans
  read -r -s -p "$prompt: " ans || true; echo >&2
  printf '%s' "$ans"
}
confirm() {  # confirm "question" -> 0 for yes (default yes)
  local ans; read -r -p "$1 [Y/n]: " ans || true
  case "${ans:-y}" in [yY]*|"") return 0 ;; *) return 1 ;; esac
}

# Previous values become defaults on re-run (never printed for secrets).
OLD_HS=""; OLD_BOT=""; OLD_OWNER=""; OLD_ROOMS=""; OLD_TOKEN=""; OLD_MODEL=""; OLD_OPENAI=""
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a; . "$ENV_FILE"; set +a
  OLD_HS="${HOMESERVER_URL:-}"; OLD_BOT="${BOT_MXID:-}"; OLD_OWNER="${OWNER_MXID:-}"
  OLD_ROOMS="${ALLOWED_ROOM_ID:-}"; OLD_TOKEN="${BOT_ACCESS_TOKEN:-}"
  OLD_MODEL="${CLAUDE_MODEL:-}"; OLD_OPENAI="${OPENAI_API_KEY:-}"
fi

bold "== Matrix/Element door — setup =="
echo "Repo: $REPO"
echo

# ── 1. Python venv + dependencies ────────────────────────────────────────────────────────────────
bold "[1/6] Python environment"
command -v python3 >/dev/null || fail "python3 not found — install it first (apt install python3 python3-venv)"
if [ ! -x "$PY" ]; then
  echo "      creating venv…"
  python3 -m venv "$VENV" || fail "could not create venv (apt install python3-venv)"
fi
"$PY" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
echo "      installing dependencies…"
"$PY" -m pip install -q -r "$HERE/bot/requirements.txt" || fail "dependency install failed"
echo "      ok"
echo

# ── 2. Homeserver + bot account ──────────────────────────────────────────────────────────────────
bold "[2/6] Bot account"
echo "The bot needs its OWN Matrix account (separate from yours)."
echo "No self-hosted server required — a free account on matrix.org works: https://app.element.io"
HS="$(ask 'Homeserver URL' "${OLD_HS:-https://matrix.org}")"
case "$HS" in http://*|https://*) ;; *) HS="https://$HS" ;; esac

TOKEN=""
if [ -n "$OLD_TOKEN" ] && confirm "Keep the existing bot access token?"; then
  TOKEN="$OLD_TOKEN"
else
  echo
  echo "  1) log in with the bot's password (the token is fetched for you — recommended)"
  echo "  2) paste an access token you already have"
  METHOD="$(ask 'Choose' '1')"
  if [ "$METHOD" = "2" ]; then
    TOKEN="$(ask_secret 'Bot access token')"
  else
    BOT_USER="$(ask 'Bot username or full MXID (e.g. mybot or @mybot:matrix.org)' "${OLD_BOT:-}")"
    BOT_PASS="$(ask_secret 'Bot password')"
    echo "      logging in…"
    LOGIN_JSON="$("$PY" "$API" login "$HS" "$BOT_USER" "$BOT_PASS")" || fail "login failed"
    TOKEN="$("$PY" -c 'import json,sys;print(json.loads(sys.argv[1])["access_token"])' "$LOGIN_JSON")"
    echo "      ok — token obtained"
  fi
fi

WHO_JSON="$("$PY" "$API" whoami "$HS" "$TOKEN")" || fail "token rejected by $HS"
BOT_MXID="$("$PY" -c 'import json,sys;print(json.loads(sys.argv[1])["user_id"])' "$WHO_JSON")"
echo "      bot identity: $BOT_MXID"
echo

# ── 3. Owner ─────────────────────────────────────────────────────────────────────────────────────
bold "[3/6] Owner"
echo "Only this account may talk to the bot. Everyone else is ignored."
OWNER="$(ask 'Your own MXID (e.g. @you:matrix.org)' "${OLD_OWNER:-}")"
[ -n "$OWNER" ] || fail "owner MXID is required"
case "$OWNER" in @*:*) ;; *) fail "MXID must look like @name:server" ;; esac
echo

# ── 4. Room discovery ────────────────────────────────────────────────────────────────────────────
bold "[4/6] Room"
echo "In Element, from YOUR account: create a room WITHOUT encryption and invite $BOT_MXID"
echo "(encryption cannot be turned off later, and this bridge does not read encrypted rooms)."
read -r -p "Press Enter once the invite is sent… " _ || true

ROOMS_JSON="$("$PY" "$API" rooms "$HS" "$TOKEN")" || fail "could not list rooms"
# Accept every pending invite, then re-list so the picker shows joined rooms only.
PENDING="$("$PY" - "$ROOMS_JSON" <<'PYEOF'
import json, sys
for r in json.loads(sys.argv[1])["rooms"]:
    if r["membership"] == "invite":
        print(r["room_id"])
PYEOF
)"
if [ -n "$PENDING" ]; then
  while IFS= read -r rid; do
    [ -n "$rid" ] || continue
    echo "      joining $rid"
    "$PY" "$API" join "$HS" "$TOKEN" "$rid" >/dev/null || warn "      could not join $rid"
  done <<< "$PENDING"
  ROOMS_JSON="$("$PY" "$API" rooms "$HS" "$TOKEN")"
fi

mapfile -t ROOM_LINES < <("$PY" - "$ROOMS_JSON" <<'PYEOF'
import json, sys
for r in json.loads(sys.argv[1])["rooms"]:
    if r["membership"] != "join":
        continue
    flag = " [ENCRYPTED - not supported]" if r.get("encrypted") else ""
    print(r["room_id"] + "\t" + (r["name"] or "(no name)") + flag)
PYEOF
)

[ "${#ROOM_LINES[@]}" -gt 0 ] || fail "the bot is not in any room — invite it and re-run this script"

echo
echo "Rooms the bot is in:"
i=1
for line in "${ROOM_LINES[@]}"; do
  printf '  %d) %s\n' "$i" "$(printf '%s' "$line" | cut -f2-)"
  i=$((i + 1))
done
PICK="$(ask 'Room number (several: 1,3)' '1')"
SELECTED=""
IFS=',' read -ra PICKS <<< "$PICK"
for p in "${PICKS[@]}"; do
  p="$(printf '%s' "$p" | tr -d ' ')"
  case "$p" in ''|*[!0-9]*) fail "not a number: $p" ;; esac
  [ "$p" -ge 1 ] && [ "$p" -le "${#ROOM_LINES[@]}" ] || fail "no such room: $p"
  rid="$(printf '%s' "${ROOM_LINES[$((p - 1))]}" | cut -f1)"
  case "${ROOM_LINES[$((p - 1))]}" in *ENCRYPTED*) warn "      room $rid is encrypted — the bridge will not see messages there" ;; esac
  SELECTED="${SELECTED:+$SELECTED,}$rid"
done
echo "      selected: $SELECTED"
echo

# ── 5. Agent settings ────────────────────────────────────────────────────────────────────────────
bold "[5/6] Agent"
CLAUDE_BIN_PATH="$(command -v claude || true)"
if [ -z "$CLAUDE_BIN_PATH" ]; then
  warn "      'claude' not found in PATH — install the Claude Code CLI and run 'claude login' before starting"
  CLAUDE_BIN_PATH="claude"
else
  echo "      claude: $CLAUDE_BIN_PATH"
fi
MODEL="$(ask 'Model' "${OLD_MODEL:-claude-opus-5}")"
echo "Voice messages need an OpenAI key (transcription + TTS). Leave empty to skip."
OPENAI="$(ask 'OPENAI_API_KEY' "${OLD_OPENAI:-}")"
echo

# ── 6. Write config ──────────────────────────────────────────────────────────────────────────────
bold "[6/6] Config + service"
umask 077
cat > "$ENV_FILE" <<EOF
# Written by matrix/setup.sh — safe to edit by hand afterwards.
HOMESERVER_URL=$HS
BOT_MXID=$BOT_MXID
BOT_ACCESS_TOKEN=$TOKEN
OWNER_MXID=$OWNER
ALLOWED_ROOM_ID=$SELECTED
CLAUDE_BIN=$CLAUDE_BIN_PATH
CLAUDE_MODEL=$MODEL
CLAUDE_CWD=$REPO
STORE_PATH=$HERE/bot/store
E2EE=0
OPENAI_API_KEY=$OPENAI
OPENAI_VOICE_MODEL=gpt-4o-mini-transcribe
TTS_MODEL=gpt-4o-mini-tts
TTS_VOICE=marin
EOF
chmod 600 "$ENV_FILE"
mkdir -p "$HERE/logs"
echo "      wrote $ENV_FILE"

START_CMD="$PY $HERE/bot/matrix_bridge.py"
if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" = "0" ]; then
  if confirm "Install and start the systemd service ($UNIT_NAME)?"; then
    cat > "$UNIT_PATH" <<EOF
[Unit]
Description=lil_worker Matrix/Element bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$HERE
ExecStart=$START_CMD
Restart=on-failure
RestartSec=10
StandardOutput=append:$HERE/logs/matrix-bridge.log
StandardError=append:$HERE/logs/matrix-bridge.log

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now "$UNIT_NAME" >/dev/null 2>&1 || true
    systemctl restart "$UNIT_NAME"
    sleep 3
    if systemctl is-active --quiet "$UNIT_NAME"; then
      echo "      service running"
      FIRST_ROOM="${SELECTED%%,*}"
      "$PY" "$API" send "$HS" "$TOKEN" "$FIRST_ROOM" \
        "Мост поднят — пиши сюда." >/dev/null 2>&1 || true
    else
      warn "      service failed to start — last log lines:"
      tail -n 20 "$HERE/logs/matrix-bridge.log" 2>/dev/null || true
      exit 1
    fi
  fi
else
  echo "      no systemd / not root — start it manually:"
  echo "      $START_CMD"
fi

echo
bold "== Done =="
echo "Write in the room from $OWNER — the bot should answer."
echo
echo "Logs:    tail -n 50 $HERE/logs/matrix-bridge.log"
echo "Restart: $HERE/restart_bridge.sh"
echo "Chat:    /new — reset the room's session · /status — model, session, jobs"
