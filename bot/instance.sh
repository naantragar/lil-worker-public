#!/bin/bash
# =============================================================
# instance.sh — менеджер дополнительных инстансов креветки
# =============================================================
# Один код (bot.py) — несколько независимых ботов (инстансов).
# Каждый инстанс: свой токен, свой каталог данных, свой рабочий
# проект (cwd), своя сессия, своя модель. Код общий — правишь
# bot.py один раз, перезапускаешь инстансы, все свежие.
#
# Дефолтная (рабочая) креветка управляется через run.sh и сюда
# НЕ входит. Здесь — только дополнительные инстансы.
#
# Использование:
#   ./instance.sh create <name> <token> <cwd> [model]
#   ./instance.sh start   <name>
#   ./instance.sh stop    <name>
#   ./instance.sh restart <name>
#   ./instance.sh status  <name>
#   ./instance.sh list
#   ./instance.sh ensure-all      # поднять все упавшие (для cron)
#
# Пример:
#   ./instance.sh create game 8512xxx:yyy ~/match3 sonnet
#   ./instance.sh start game
# =============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT="$SCRIPT_DIR/bot.py"
VENV="$SCRIPT_DIR/.venv/bin/python"
INSTANCES_DIR="$SCRIPT_DIR/instances"
# Caps live OUTSIDE instances/ on purpose: instances/<n>/ is writable by that instance,
# so a cap stored there could be removed by the very instance it constrains.
CAPS_DIR="$SCRIPT_DIR/caps"
VALID_CAPS="upstream-specialist"

err() { echo "ERROR: $*" >&2; exit 1; }

inst_dir()  { echo "$INSTANCES_DIR/$1"; }
inst_env()  { echo "$INSTANCES_DIR/$1/instance.env"; }
inst_pid()  { echo "$INSTANCES_DIR/$1/lil_worker.pid"; }
inst_log()  { echo "$INSTANCES_DIR/$1/lil_worker.log"; }

resolve_model() {
  # Короткие алиасы → полные id; иначе как есть; пусто → sonnet
  case "$1" in
    opus)   echo "claude-opus-4-8" ;;
    sonnet) echo "claude-sonnet-4-6" ;;
    haiku)  echo "claude-haiku-4-5" ;;
    "")     echo "claude-sonnet-4-6" ;;
    *)      echo "$1" ;;
  esac
}

cmd_create() {
  local name="$1" token="$2" cwd="$3" model="$4"
  [ -n "$name" ]  || err "нужно имя инстанса"
  [ -n "$token" ] || err "нужен токен"
  [ -n "$cwd" ]   || err "нужен рабочий каталог (cwd)"
  [ -d "$cwd" ]   || err "каталог не существует: $cwd"
  echo "$name" | grep -qE '^[a-z0-9_-]+$' || err "имя только [a-z0-9_-]"

  local dir; dir="$(inst_dir "$name")"
  [ -e "$dir" ] && err "инстанс '$name' уже существует ($dir)"

  mkdir -p "$dir"
  local full_model; full_model="$(resolve_model "$model")"

  cat > "$(inst_env "$name")" <<EOF
# Инстанс креветки: $name
# Создан: $(date '+%Y-%m-%d %H:%M:%S')
# Переменные инстанса имеют приоритет над bot/.env (setdefault не перетирает).
# Общие секреты (OPENAI_API_KEY и т.п.) наследуются из bot/.env.
TELEGRAM_BOT_TOKEN=$token
LIL_WORKER_DATA_DIR=$dir
LIL_WORKER_BOT_CWD=$cwd
LIL_WORKER_INSTANCE=$name
CLAUDE_MODEL=$full_model
EOF

  echo "{\"model\": \"$full_model\"}" > "$dir/model_config.json"

  echo "✅ Инстанс '$name' создан:"
  echo "   каталог:  $dir"
  echo "   токен:    ${token:0:10}…"
  echo "   проект:   $cwd"
  echo "   модель:   $full_model"
  echo "   запуск:   ./instance.sh start $name"
}

cmd_start() {
  local name="$1"
  [ -n "$name" ] || err "нужно имя инстанса"
  local envf; envf="$(inst_env "$name")"
  [ -f "$envf" ] || err "инстанс '$name' не найден (нет $envf)"

  if cmd_running "$name"; then
    echo "Инстанс '$name' уже работает (PID $(cat "$(inst_pid "$name")"))"
    return 0
  fi

  # Чистим возможные осиротевшие процессы этого инстанса
  pkill -f "$BOT --instance-tag $name" 2>/dev/null
  sleep 0.3

  # Экспортируем переменные инстанса в окружение; bot.py читает bot/.env
  # через setdefault — наши значения имеют приоритет.
  set -a
  # shellcheck disable=SC1090
  . "$envf"
  set +a

  local log; log="$(inst_log "$name")"
  nohup env PYTHONUNBUFFERED=1 "$VENV" "$BOT" --instance-tag "$name" >> "$log" 2>&1 &
  echo "Запущен инстанс '$name' (PID $!). Лог: $log"
}

cmd_running() {
  local name="$1"
  local pidf; pidf="$(inst_pid "$name")"
  if [ -f "$pidf" ] && kill -0 "$(cat "$pidf" 2>/dev/null)" 2>/dev/null; then
    return 0
  fi
  # fallback: ищем по уникальному маркеру в cmdline
  pgrep -f "$BOT --instance-tag $name" >/dev/null 2>&1
}

cmd_stop() {
  local name="$1"
  [ -n "$name" ] || err "нужно имя инстанса"
  local pidf; pidf="$(inst_pid "$name")"
  local stopped=false

  if [ -f "$pidf" ] && kill -0 "$(cat "$pidf" 2>/dev/null)" 2>/dev/null; then
    kill "$(cat "$pidf")" 2>/dev/null && stopped=true
  fi
  rm -f "$pidf"
  pkill -f "$BOT --instance-tag $name" 2>/dev/null && stopped=true
  sleep 0.5
  pkill -9 -f "$BOT --instance-tag $name" 2>/dev/null
  # Чистим tmux runtime инстанса
  tmux kill-session -t "${name}_runtime" 2>/dev/null

  if $stopped; then echo "Инстанс '$name' остановлен."; else echo "Инстанс '$name' не был запущен."; fi
}

cmd_restart() {
  cmd_stop "$1"
  sleep 1
  cmd_start "$1"
}

cmd_status() {
  local name="$1"
  [ -n "$name" ] || err "нужно имя инстанса"
  [ -f "$(inst_env "$name")" ] || err "инстанс '$name' не найден"
  if cmd_running "$name"; then
    echo "Инстанс '$name': Running (PID $(cat "$(inst_pid "$name")" 2>/dev/null))"
  else
    echo "Инстанс '$name': Not running"
  fi
}

cmd_list() {
  [ -d "$INSTANCES_DIR" ] || { echo "Нет ни одного инстанса."; return 0; }
  local found=false
  for d in "$INSTANCES_DIR"/*/; do
    [ -f "$d/instance.env" ] || continue
    found=true
    local name; name="$(basename "$d")"
    local cwd model
    cwd="$(grep '^LIL_WORKER_BOT_CWD=' "$d/instance.env" | cut -d= -f2-)"
    model="$(grep '^CLAUDE_MODEL=' "$d/instance.env" | cut -d= -f2-)"
    local state="Not running"
    cmd_running "$name" && state="Running (PID $(cat "$(inst_pid "$name")" 2>/dev/null))"
    printf "  %-12s %-14s %-26s %s\n" "$name" "$state" "$model" "$cwd"
  done
  $found || echo "Нет ни одного инстанса."
}

cmd_cap() {
  # cap set <name> <profile> | cap off <name> | cap show [name]
  local verb="$1" name="$2" profile="$3"
  local capf="$CAPS_DIR/$name.json"

  case "$verb" in
    set)
      [ -n "$name" ] && [ -n "$profile" ] || err "usage: cap set <name> <profile>  (profiles: $VALID_CAPS)"
      [ -f "$(inst_env "$name")" ] || err "инстанс '$name' не найден"
      echo "$VALID_CAPS" | tr ' ' '\n' | grep -qx "$profile" || err "неизвестный профиль '$profile' (есть: $VALID_CAPS)"
      mkdir -p "$CAPS_DIR"
      cat > "$capf" <<EOF
{
  "profile": "$profile",
  "set_by": "krevetka main instance",
  "set_at": "$(date '+%Y-%m-%d %H:%M:%S')"
}
EOF
      echo "✅ Колпак '$profile' надет на '$name'. Применится после: ./instance.sh restart $name"
      ;;
    off)
      [ -n "$name" ] || err "usage: cap off <name>"
      if [ -f "$capf" ]; then
        rm -f "$capf"
        echo "✅ Колпак снят с '$name' (остаётся базовая защита кода креветки)."
        echo "   Применится после: ./instance.sh restart $name"
      else
        echo "На '$name' колпака нет."
      fi
      ;;
    show|"")
      if [ -n "$name" ]; then
        [ -f "$capf" ] && { echo "$name: $(grep '"profile"' "$capf" | cut -d'"' -f4)"; } || echo "$name: без колпака"
      else
        [ -d "$CAPS_DIR" ] || { echo "Колпаков нет."; return 0; }
        local any=false
        for f in "$CAPS_DIR"/*.json; do
          [ -f "$f" ] || continue
          any=true
          printf "  %-12s %s\n" "$(basename "$f" .json)" "$(grep '"profile"' "$f" | cut -d'"' -f4)"
        done
        $any || echo "Колпаков нет."
      fi
      ;;
    *) err "usage: cap {set <name> <profile>|off <name>|show [name]}" ;;
  esac
}

cmd_ensure_all() {
  # Для cron: поднять все инстансы, которые должны работать, но упали
  [ -d "$INSTANCES_DIR" ] || return 0
  for d in "$INSTANCES_DIR"/*/; do
    [ -f "$d/instance.env" ] || continue
    local name; name="$(basename "$d")"
    if ! cmd_running "$name"; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') ensure-all: поднимаю '$name'"
      cmd_start "$name"
    fi
  done
}

case "$1" in
  create)     cmd_create "$2" "$3" "$4" "$5" ;;
  start)      cmd_start "$2" ;;
  stop)       cmd_stop "$2" ;;
  restart)    cmd_restart "$2" ;;
  status)     cmd_status "$2" ;;
  list)       cmd_list ;;
  cap)        cmd_cap "$2" "$3" "$4" ;;
  ensure-all) cmd_ensure_all ;;
  *)
    echo "Usage: $0 {create|start|stop|restart|status|list|cap|ensure-all}"
    echo "  create <name> <token> <cwd> [model]"
    echo "  start|stop|restart|status <name>"
    echo "  cap set <name> <profile> | cap off <name> | cap show [name]"
    echo "  list | ensure-all"
    exit 1
    ;;
esac
