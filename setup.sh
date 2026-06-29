#!/bin/bash
# lil_worker setup script
# Запускати з середини вже клонованого репо: cd lil_worker && bash setup.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "=== lil_worker setup ==="
echo ""

# ── 1. Перевірка що ми в репо ─────────────────────────────────────────────────

if [ ! -f "bot/bot.py" ]; then
  echo "[!] bot/bot.py не знайдено."
  echo "    Запускай цей скрипт з кореня репо lil_worker:"
  echo "    cd lil_worker && bash setup.sh"
  exit 1
fi

echo "[ok] Репо знайдено: $SCRIPT_DIR"

# ── 2. Перевірка claude CLI ───────────────────────────────────────────────────

if ! command -v claude &>/dev/null; then
  echo ""
  echo "[!] Claude Code CLI не знайдено."
  echo ""
  echo "    Встанови:"
  echo "    npm install -g @anthropic-ai/claude-code"
  echo ""
  echo "    Потім авторизуйся:"
  echo "    claude login"
  echo ""
  echo "    Після цього запусти setup.sh знову."
  exit 1
fi

echo "[ok] claude CLI: $(which claude)"

# ── 3. Перевірка Python ───────────────────────────────────────────────────────

if ! command -v python3 &>/dev/null; then
  echo "[..] python3 не знайдено. Встановлюю..."
  sudo apt-get update -qq && sudo apt-get install -y python3 python3-venv python3-pip
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "[ok] Python $PYTHON_VERSION"

# ── 4. Python venv + залежності ───────────────────────────────────────────────

if [ ! -d "bot/.venv" ]; then
  echo "[..] Створюю Python venv..."
  python3 -m venv bot/.venv
  echo "[ok] venv створено"
fi

echo "[..] Встановлюю залежності..."
bot/.venv/bin/pip install --quiet --upgrade pip
bot/.venv/bin/pip install --quiet -r bot/requirements.txt
echo "[ok] Залежності встановлено"

# ── 5. .env ───────────────────────────────────────────────────────────────────

if [ ! -f "bot/.env" ]; then
  echo ""
  echo "=== Налаштування .env ==="
  echo ""

  read -rp "TELEGRAM_BOT_TOKEN: " BOT_TOKEN
  read -rp "ALLOWED_USERS (Telegram ID через кому): " ALLOWED_USERS
  read -rp "OPENAI_API_KEY (Enter щоб пропустити): " OPENAI_KEY

  cat > bot/.env <<EOF
TELEGRAM_BOT_TOKEN=$BOT_TOKEN
ALLOWED_USERS=$ALLOWED_USERS
CLAUDE_MODEL=sonnet
OPENAI_API_KEY=$OPENAI_KEY
OPENAI_VOICE_MODEL=gpt-4o-mini-transcribe
EOF

  echo "[ok] .env створено"
else
  echo "[ok] .env вже існує, пропускаю"
fi

# ── 6. Дефолтні конфіги ──────────────────────────────────────────────────────

if [ ! -f "bot/model_config.json" ]; then
  echo '{"model": "sonnet"}' > bot/model_config.json
  echo "[ok] model_config.json (sonnet)"
fi

if [ ! -f "bot/transcribe_config.json" ]; then
  echo '{"language": null, "temperature": 0.2}' > bot/transcribe_config.json
  echo "[ok] transcribe_config.json (авто-детект)"
fi

# ── 7. Права ──────────────────────────────────────────────────────────────────

chmod +x bot/run.sh
echo "[ok] run.sh — права виставлено"

# ── Готово ────────────────────────────────────────────────────────────────────

echo ""
echo "=== Готово! ==="
echo ""
echo "Запуск:"
echo "  bot/run.sh start"
echo ""
echo "Статус:"
echo "  bot/run.sh status"
echo ""
echo "Логи (останні 50 рядків):"
echo "  tail -n 50 bot/lil_worker.log"
echo ""
