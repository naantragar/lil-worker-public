#!/bin/bash
# lil_worker — one-command installer
# Usage: curl -fsSL https://raw.githubusercontent.com/naantragar/lil-worker-public/main/install.sh | bash
#
# What it does:
#   1. Installs system deps (git, curl, python3)
#   2. Installs Node.js 22
#   3. Installs Claude Code CLI
#   4. Clones the repo to ~/lil_worker
#   5. Creates Python venv + installs dependencies
#
# What you do after:
#   claude login
#   cd ~/lil_worker && bash setup.sh
#   bot/run.sh start

set -e

# On root (VPS, Docker) sudo doesn't exist — use direct commands
if [ "$(id -u)" = "0" ]; then
  SUDO=""
else
  SUDO="sudo"
fi

echo ""
echo "=============================="
echo "  lil_worker installer"
echo "=============================="
echo ""

# ── 1. System packages ───────────────────────────────────────────────────────

echo "[1/5] System packages..."

if ! command -v git &>/dev/null || ! command -v curl &>/dev/null; then
  $SUDO apt-get update -qq
  $SUDO apt-get install -y git curl python3 python3-venv python3-pip
  echo "      installed git, curl, python3"
else
  # Ensure python3-venv is present even if git/curl exist
  if ! python3 -m venv --help &>/dev/null 2>&1; then
    $SUDO apt-get update -qq
    $SUDO apt-get install -y python3-venv
  fi
  echo "      already installed"
fi

# ── 2. Node.js ───────────────────────────────────────────────────────────────

echo "[2/5] Node.js (>= 18 required for Claude CLI)..."

_install_node_apt() {
  echo "      trying system repo..."
  $SUDO apt-get install -y nodejs npm
}

_install_node_nodesource() {
  echo "      adding nodesource repo (may take a few minutes)..."
  curl -fsSL https://deb.nodesource.com/setup_22.x | $SUDO bash -
  echo "      installing nodejs..."
  $SUDO apt-get install -y nodejs npm
}

if command -v node &>/dev/null; then
  NODE_MAJOR=$(node --version | sed 's/v\([0-9]*\).*/\1/')
  if [ "$NODE_MAJOR" -ge 18 ]; then
    echo "      node $(node --version) ok"
    # npm might be missing even if node is installed
    if ! command -v npm &>/dev/null; then
      echo "      npm missing, installing..."
      $SUDO apt-get install -y npm
    fi
    echo "      node $(node --version), npm $(npm --version)"
  else
    echo "      found old version $(node --version), upgrading..."
    _install_node_nodesource
    echo "      upgraded to $(node --version)"
  fi
else
  # Try system repo first (fast, works on Ubuntu 22.04+)
  _install_node_apt
  if command -v node &>/dev/null; then
    NODE_MAJOR=$(node --version | sed 's/v\([0-9]*\).*/\1/')
    if [ "$NODE_MAJOR" -ge 18 ]; then
      echo "      installed $(node --version) from system repo"
    else
      echo "      system repo gave old $(node --version), switching to nodesource..."
      _install_node_nodesource
      echo "      installed $(node --version)"
    fi
  else
    # Fallback to nodesource
    _install_node_nodesource
    echo "      installed $(node --version)"
  fi
fi

# ── 3. Claude Code CLI ───────────────────────────────────────────────────────

echo "[3/5] Claude Code CLI..."

if command -v claude &>/dev/null; then
  echo "      already installed ($(claude --version 2>/dev/null || echo 'unknown version'))"
else
  echo "      running: npm install -g @anthropic-ai/claude-code (may take 1-3 min)..."
  npm install -g @anthropic-ai/claude-code
  echo "      installed ($(claude --version 2>/dev/null || echo 'ok'))"
fi

# ── 4. Clone repo ────────────────────────────────────────────────────────────

echo "[4/5] Cloning lil_worker..."

TARGET="$HOME/lil_worker"

if [ -d "$TARGET" ]; then
  echo "      $TARGET already exists, skipping clone"
else
  git clone -q https://github.com/naantragar/lil-worker-public.git "$TARGET"
  echo "      cloned to $TARGET"
fi

# ── 5. Python venv + dependencies ────────────────────────────────────────────

echo "[5/5] Python environment..."

cd "$TARGET"

# Install version-specific python venv package (Ubuntu 24 needs python3.12-venv)
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "      python $PY_VER"
$SUDO apt-get install -y "python${PY_VER}-venv" "python${PY_VER}-distutils" 2>/dev/null || true

# Remove broken venv if pip is missing
if [ -d "bot/.venv" ] && [ ! -f "bot/.venv/bin/pip" ]; then
  echo "      removing broken venv..."
  rm -rf bot/.venv
fi

if [ ! -d "bot/.venv" ]; then
  echo "      creating venv..."
  python3 -m venv bot/.venv || python3 -m venv --without-pip bot/.venv
fi

# Ensure pip is available — bootstrap if missing
if [ ! -f "bot/.venv/bin/pip" ]; then
  echo "      pip missing, bootstrapping..."
  curl -sS https://bootstrap.pypa.io/get-pip.py | bot/.venv/bin/python3
fi

bot/.venv/bin/pip install --quiet --upgrade pip
echo "      installing python packages..."
bot/.venv/bin/pip install -r bot/requirements.txt
echo "      dependencies installed"

# Make scripts executable
chmod +x bot/run.sh
[ -f bot/validate.sh ] && chmod +x bot/validate.sh

# ── 6. Auto-restart on reboot + watchdog ──────────────────────────────────────

echo "[6/6] Setting up auto-restart..."

chmod +x bot/watchdog.sh 2>/dev/null

# Try cron first (best option: survives reboot without login)
CRON_OK=false
if crontab -l >/dev/null 2>&1 || [ -w /var/spool/cron/crontabs/ ] 2>/dev/null; then
  CRON_REBOOT="@reboot sleep 15 && cd $TARGET && bot/run.sh start >> bot/lil_worker.log 2>&1"
  CRON_MARKER="# lil_worker auto-restart"

  if crontab -l 2>/dev/null | grep -qF "lil_worker auto-restart"; then
    echo "      cron already installed"
    CRON_OK=true
  else
    if (crontab -l 2>/dev/null; echo ""; echo "$CRON_MARKER"; echo "$CRON_REBOOT") | crontab - 2>/dev/null; then
      echo "      cron @reboot installed"
      CRON_OK=true
    fi
  fi
fi

if [ "$CRON_OK" = false ]; then
  echo "      cron not available (no permissions)"
  echo "      watchdog will handle crash recovery (starts with run.sh start)"
  echo "      NOTE: after server reboot, run manually: cd ~/lil_worker && bot/run.sh start"
fi

echo "      watchdog: bot/watchdog.sh (auto-starts with run.sh, checks every 5 min)"

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "=============================="
echo "  Installation complete!"
echo "=============================="
echo ""
echo "Next steps:"
echo ""
echo "  1. Log in to Claude:"
echo "     claude login"
echo ""
echo "  2. Configure the bot:"
echo "     cd ~/lil_worker && bash setup.sh"
echo ""
echo "  3. Start the bot:"
echo "     bot/run.sh start"
echo ""
echo "  4. Check in Telegram - send any message to your bot"
echo ""
echo "Auto-restart:"
echo "  - Watchdog checks every 5 min, restarts if crashed"
if [ "$CRON_OK" = true ]; then
  echo "  - Cron @reboot: bot starts automatically after server reboot"
else
  echo "  - After server reboot, start manually: cd ~/lil_worker && bot/run.sh start"
fi
echo ""
