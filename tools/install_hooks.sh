#!/usr/bin/env bash
# Install the attribution post-commit hook into one or more repos' .git/hooks.
#   usage: tools/install_hooks.sh [repo ...]      (default: the repo this script lives in)
#   e.g.:  tools/install_hooks.sh ~/lil_worker ~/some-other-repo
# Idempotent; backs up any pre-existing non-attribution hook. Run once per repo/clone.
#
# The absolute path to action_log.py is injected into the hook here, so the hook source itself
# stays free of machine-specific paths.
set -euo pipefail

TOOLS="$(cd "$(dirname "$0")" && pwd)"
SRC="$TOOLS/hooks/post-commit"
LOGPATH="$TOOLS/action_log.py"
[ -f "$SRC" ] || { echo "missing hook source: $SRC" >&2; exit 1; }
[ -f "$LOGPATH" ] || { echo "missing logger: $LOGPATH" >&2; exit 1; }

if [ "$#" -eq 0 ]; then
  set -- "$(cd "$TOOLS/.." && pwd)"
fi

for repo in "$@"; do
  hooks="$repo/.git/hooks"
  if [ ! -d "$hooks" ]; then
    echo "skip (not a git repo): $repo"
    continue
  fi
  dst="$hooks/post-commit"
  if [ -e "$dst" ] && ! grep -q 'action_log.py' "$dst" 2>/dev/null; then
    cp "$dst" "$dst.bak.$(date +%s)"
    echo "backed up existing hook: $dst.bak.*"
  fi
  sed "s#__ACTION_LOG_PATH__#$LOGPATH#" "$SRC" > "$dst"
  chmod +x "$dst"
  echo "installed post-commit -> $repo"
done
