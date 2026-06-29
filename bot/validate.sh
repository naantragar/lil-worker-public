#!/bin/bash
# Pre-restart validation for bot.py
# Usage: ./validate.sh         (level 1 — quick)
#        ./validate.sh --deep  (level 2 — full with dry-run)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT="$SCRIPT_DIR/bot.py"
VENV="$SCRIPT_DIR/.venv/bin/python"
DEEP=false
FAIL=false

if [ "$1" = "--deep" ]; then
  DEEP=true
fi

echo "=== Pre-restart validation ==="
echo "Level: $([ "$DEEP" = true ] && echo '2 (deep)' || echo '1 (quick)')"
echo ""

# --- Check 1: Syntax ---
echo -n "[1] Syntax check... "
if $VENV -m py_compile "$BOT" 2>/tmp/validate_err.txt; then
  echo "OK"
else
  echo "FAIL"
  cat /tmp/validate_err.txt
  FAIL=true
fi

# --- Check 2: Imports ---
echo -n "[2] Import check... "
IMPORT_OUT=$($VENV -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from bot import markdown_to_telegram_html, format_tool_notification
print('OK')
" 2>&1)
if echo "$IMPORT_OUT" | grep -q "OK"; then
  echo "OK"
else
  echo "FAIL"
  echo "$IMPORT_OUT"
  FAIL=true
fi

# --- Deep checks (level 2) ---
if [ "$DEEP" = true ]; then

  # --- Check 3: Critical functions ---
  echo -n "[3] Critical functions test... "
  FUNC_OUT=$($VENV -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from bot import markdown_to_telegram_html, format_tool_notification

# Test markdown renderer
r1 = markdown_to_telegram_html('**bold** and \`code\`')
assert '<b>' in r1 and '<code>' in r1, f'Renderer broken: {r1}'

# Test tool notification
n1 = format_tool_notification('Bash', {'command': 'ls', 'description': 'List files'})
assert n1 is not None, 'format_tool_notification returned None for Bash'

n2 = format_tool_notification('Read', {'file_path': '/tmp/x'})
assert n2 is None or n2 == '', f'Read should not generate notification, got: {n2}'

print('OK')
" 2>&1)
  if echo "$FUNC_OUT" | grep -q "OK"; then
    echo "OK"
  else
    echo "FAIL"
    echo "$FUNC_OUT"
    FAIL=true
  fi

  # --- Check 4: Backup ---
  echo -n "[4] Backup... "
  cp "$BOT" "$BOT.bak"
  echo "OK ($BOT.bak)"

  # --- Check 5: Dry-run (start and kill after 3s) ---
  echo -n "[5] Dry-run (3s)... "
  # Run bot briefly, capture output, kill after 3s
  timeout 5 $VENV "$BOT" --validate-startup > /tmp/validate_dryrun.txt 2>&1 &
  DRY_PID=$!
  sleep 3
  kill $DRY_PID 2>/dev/null
  wait $DRY_PID 2>/dev/null

  if grep -q "Validation startup path completed without live Telegram polling." /tmp/validate_dryrun.txt; then
    echo "OK (startup validates without live Telegram polling)"
  else
    echo "FAIL (bot did not start)"
    cat /tmp/validate_dryrun.txt
    FAIL=true
  fi

  rm -f "$SCRIPT_DIR/lil_worker.pid" "$SCRIPT_DIR/bot_runtime_state.json"

fi

# --- Result ---
echo ""
if [ "$FAIL" = true ]; then
  echo "=== VALIDATION FAILED — DO NOT RESTART ==="
  exit 1
else
  echo "=== ALL CHECKS PASSED ==="
  exit 0
fi
