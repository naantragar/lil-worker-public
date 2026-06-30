#!/usr/bin/env python3
"""
selfmod_guard — PreToolUse hook that blocks a NON-privileged krevetka instance from
modifying the bot's own code/persona.

Wired by bot.py ONLY for secondary instances (INSTANCE_NAME != the privileged default):
the bot passes `--settings` with a PreToolUse hook -> `python3 selfmod_guard.py`.
The default/main instance never gets this hook, so it keeps full self-modification.

Contract (Claude Code PreToolUse hook):
  stdin  = JSON {tool_name, tool_input, ...}
  exit 0 = allow;  exit 2 = BLOCK (stderr is shown to the model as the reason)

Protected = the whole repo root (parent of this bot/ dir): bot/, CLAUDE.md, tools/,
skills/, ops/, etc. Reads are allowed; only MODIFICATION is blocked.
"""
import json
import os
import re
import sys

PROTECTED_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The instance's OWN data dir (its model/transcribe config, sessions, runtime state) is NOT
# protected — a secondary instance may manage its OWN settings (e.g. switch its own model),
# just not the shared bot code. instance.sh sets this to bot/instances/<name>/.
OWN_DATA_DIR = os.environ.get("LIL_WORKER_DATA_DIR", "").strip()
_OWN = os.path.abspath(OWN_DATA_DIR) if OWN_DATA_DIR else None

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# write/mutation indicators for Bash commands
WRITE_RE = re.compile(
    r"(>>?|\btee\b|\bsed\s+-i|\bperl\s+-[a-z]*i|\bcp\b|\bmv\b|\brm\b|\brmdir\b|\bln\b"
    r"|\bchmod\b|\bchown\b|\btruncate\b|\bdd\b|\bmkdir\b|\btouch\b|\bapply_patch\b"
    r"|\bgit\s+(commit|push|checkout|reset|apply|rm|mv|clean)\b"
    r"|open\([^)]*['\"][wa])"
)


def _deny(reason):
    sys.stderr.write(
        "BLOCKED by selfmod_guard: this is a secondary instance — it may NOT modify "
        "krevetka's own code/persona. " + reason + " (Self-modification is allowed only "
        "from the main bot.)"
    )
    sys.exit(2)


def _is_own(ap):
    return bool(_OWN) and (ap == _OWN or ap.startswith(_OWN + os.sep))


def _under_protected(path):
    if not path:
        return False
    ap = os.path.abspath(os.path.expanduser(path))
    if _is_own(ap):
        return False  # the instance's own settings/data — allowed
    return ap == PROTECTED_ROOT or ap.startswith(PROTECTED_ROOT + os.sep)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        # can't parse → fail safe = allow (don't break the secondary instance on bad input)
        sys.exit(0)

    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}

    if tool in EDIT_TOOLS:
        path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
        if _under_protected(path):
            _deny(f"refused {tool} on protected path: {path}")
        sys.exit(0)

    if tool == "Bash":
        cmd = ti.get("command", "") or ""
        if WRITE_RE.search(cmd):
            # ignore references to the instance's OWN data dir, then see if any protected
            # (shared-code) path remains.
            residual = cmd.replace(OWN_DATA_DIR, " ") if OWN_DATA_DIR else cmd
            if PROTECTED_ROOT in residual:
                _deny("refused Bash command that writes into the protected repo (shared code).")
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
