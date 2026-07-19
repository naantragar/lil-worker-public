#!/usr/bin/env python3
"""
selfmod_guard — PreToolUse hook that constrains a NON-privileged krevetka instance.

Wired by bot.py ONLY for secondary instances (INSTANCE_NAME != PRIVILEGED_INSTANCE):
the bot passes `--settings` with a PreToolUse hook -> `python3 selfmod_guard.py`.
The main instance never gets this hook, so it keeps full self-modification.

Contract (Claude Code PreToolUse hook):
  stdin  = JSON {tool_name, tool_input, ...}
  exit 0 = allow;  exit 2 = BLOCK (stderr is shown to the model as the reason)

===============================================================================
CAPS (profiles)
===============================================================================
A "cap" narrows a secondary instance further than the baseline. It is declared in
    <repo>/bot/caps/<instance-name>.json   ->  {"profile": "<name>"}
That directory lives inside PROTECTED_ROOT, so a capped instance CANNOT edit or
delete its own cap — only the main instance can (via `instance.sh cap ...`).
NOTE: the instance's own data dir (bot/instances/<n>/) is deliberately writable, so
the cap must NEVER be stored there.

Profiles:
  default            — baseline only: can't touch krevetka's code, can't kill the main bot.
  upstream-specialist   — baseline + writes confined to the upstream tree, infra deny-list,
                       systemctl/docker allow-lists, cannot stop itself, cannot read
                       krevetka's secrets.

Fail-closed: if a cap file exists but can't be parsed, every mutating tool is blocked.

This is a speed-bump against accidents and careless commands, NOT a sandbox: the agent
runs as root, so a determined operator can evade any regex (numeric PIDs, python one-liners,
base64). Real isolation = separate OS user / VM.
"""
import glob
import json
import os
import re
import sys

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
PROTECTED_ROOT = os.path.dirname(CODE_DIR)
CAPS_DIR = os.path.join(CODE_DIR, "caps")

INSTANCE_NAME = os.environ.get("LIL_WORKER_INSTANCE", "").strip()

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

# LIFECYCLE indicators: a secondary instance must NEVER kill/restart the MAIN bot, even though
# these commands don't "write a file" (so WRITE_RE misses them). This is the privilege boundary —
# the main bot's process lifecycle is main-only. Blocks: the main restart script, main run.sh
# lifecycle verbs, and any kill/pkill aimed at the bot process or its pid file.
LIFECYCLE_RE = re.compile(
    r"\brestart_crab\.sh\b"
    r"|\brun\.sh\b[^\n]*\b(restart|stop|start)\b"
    # Both names: the entry file was renamed bot.py -> krevetka.py precisely so a fuzzy match can't
    # hit another project's bot. This regex must track that, or the "cannot kill the main bot"
    # guarantee silently disappears with the rename.
    r"|\b(pkill|kill|killall)\b[^\n]*\b(krevetka|bot)\.py\b"
    r"|\b(pkill|kill|killall)\b[^\n]*lil_worker(\.pid|\b)"
)

# Baseline systemctl rule: a plain secondary instance manages no services at all. A cap may
# replace this with a narrower allow-list (upstream-specialist does), so it is checked separately.
SYSTEMCTL_MUTATE_RE = re.compile(r"\bsystemctl\b[^\n]*\b(restart|stop|kill|start)\b")


def _deny(reason, cap=None):
    where = f"cap '{cap}'" if cap else "baseline"
    sys.stderr.write(
        f"BLOCKED by selfmod_guard ({where}): {reason} "
        "(This is a secondary instance. Only the main krevetka instance can lift this.)"
    )
    sys.exit(2)


def _is_own(ap):
    return bool(_OWN) and (ap == _OWN or ap.startswith(_OWN + os.sep))


def _under(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _abs(path):
    return os.path.abspath(os.path.expanduser(path))


def _under_protected(path):
    if not path:
        return False
    ap = _abs(path)
    if _is_own(ap):
        return False  # the instance's own settings/data — allowed
    return _under(ap, PROTECTED_ROOT)


def _normalize(cmd):
    """Join line-continuations and newlines so a multi-line command can't split a verb
    from its target and slip past the single-line regexes."""
    return re.sub(r"\\\s*\n", " ", cmd).replace("\n", " ")


# ---------------------------------------------------------------------------
# cap loading
# ---------------------------------------------------------------------------

def load_cap():
    """Return the cap profile name, or None. Raises ValueError if the file is corrupt."""
    if not INSTANCE_NAME:
        return None
    path = os.path.join(CAPS_DIR, f"{INSTANCE_NAME}.json")
    if not os.path.isfile(path):
        return None
    with open(path) as fh:
        data = json.load(fh)          # ValueError on corruption -> fail closed
    profile = (data.get("profile") or "").strip()
    return profile or None


# ---------------------------------------------------------------------------
# profile: upstream-specialist
# ---------------------------------------------------------------------------

UPSTREAM_WRITE_ROOTS = [
    "~/upstream-system",   # code: api, web, ops, docs, migrations
    "~/upstream-deploy",   # docker compose stack (api, worker, db)
    "~/wa-monitor",     # live WhatsApp collector
    "/tmp",
]

# Infra actions that could take the box (or an unrelated project) down. Denied outright.
UPSTREAM_DENY_BASH = [
    (r"\b(shutdown|reboot|poweroff|halt)\b|\binit\s+[06]\b",
     "power control (shutdown/reboot) is not allowed"),
    (r"\bsystemctl\b[^\n]*\b(poweroff|reboot|halt|isolate|mask|unmask)\b",
     "systemctl power/mask verbs are not allowed"),
    (r"\b(ufw|iptables|ip6tables|nft|nftables)\b",
     "firewall changes are not allowed"),
    (r"\b(useradd|userdel|usermod|adduser|deluser|groupadd|passwd|chpasswd|visudo)\b",
     "user/permission management is not allowed"),
    (r"\bcrontab\b|/etc/cron",
     "cron changes are not allowed"),
    (r"\b(mkfs|mkfs\.\w+|fdisk|parted|wipefs|mkswap)\b",
     "disk/filesystem operations are not allowed"),
    (r"\bdd\b[^\n]*\bof=\s*/dev/",
     "raw writes to block devices are not allowed"),
    (r"\bapt(-get)?\b[^\n]*\b(remove|purge|autoremove)\b|\bdpkg\b[^\n]*\s-(r|P)\b",
     "removing system packages is not allowed"),
    (r"\bdocker\b[^\n]*\b(system|volume|network|image|builder)\s+prune\b",
     "docker prune can destroy other projects' data"),
    (r"\bdocker\b[^\n]*\b(stop|kill|rm|restart|down|update)\b[^\n]*(legacy_api_container|legacy-deploy)"
     r"|\bdocker\b[^\n]*(legacy_api_container|legacy-deploy)[^\n]*\b(stop|kill|rm|restart|down)\b",
     "the legacy upstream container belongs to another stack — hands off"),
    (r"\b(instance\.sh|run\.sh|restart_crab\.sh|watchdog\.sh)\b",
     "krevetka's process-lifecycle scripts are main-only (you cannot even stop yourself)"),
    (r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/(\s|$)",
     "rm -rf / — no"),
    (r"\bnginx\b[^\n]*\s-s\s+(stop|quit)\b",
     "stopping nginx would take down unrelated sites; use reload"),
]

# Reading krevetka's own secrets (bot token, API keys, ssh/claude credentials) — denied for
# both Read and Bash. upstream's own .env files are unaffected.
SECRET_RE = re.compile(
    r"lil_worker/bot/\.env"
    r"|instances/[^/\s]+/instance\.env"
    r"|/root/\.claude/\.credentials\.json"
    r"|(^|/)\.ssh(/|\b)"
    r"|\bid_rsa\b|\bid_ed25519\b"
)

# Paths outside the upstream tree whose modification would be system-level.
UPSTREAM_SENSITIVE_WRITE_RE = re.compile(
    r"(^|\s|=)/etc/|(^|\s|=)/boot/|(^|\s|=)/usr/|(^|\s|=)/lib/|(^|\s|=)/var/spool/cron"
)

SYSTEMCTL_RE = re.compile(r"\bsystemctl\b((?:\s+--[\w=-]+)*)\s+([a-z-]+)(?:\s+([\w@.\-]+))?")
SYSTEMCTL_READ_VERBS = {
    "status", "show", "cat", "is-active", "is-enabled", "is-failed",
    "list-units", "list-unit-files", "list-timers", "show-environment",
}
NGINX_ALLOWED_VERBS = {"reload", "restart", "start"}


def _protected_pids():
    """PIDs of krevetka's own bot processes (main + every instance, including this one).
    Blocking these by number stops `kill 12345` from evading the name-based regexes."""
    pids = set()
    files = [os.path.join(CODE_DIR, "lil_worker.pid")]
    files += glob.glob(os.path.join(CODE_DIR, "instances", "*", "lil_worker.pid"))
    for f in files:
        try:
            with open(f) as fh:
                pid = fh.read().strip()
            if pid.isdigit():
                pids.add(pid)
        except OSError:
            pass
    return pids


def upstream_check_bash(cmd, norm):
    for pattern, reason in UPSTREAM_DENY_BASH:
        if re.search(pattern, norm):
            _deny(reason, "upstream-specialist")

    if SECRET_RE.search(norm):
        _deny("that path holds krevetka's own secrets (bot token / API keys / ssh)",
              "upstream-specialist")

    # kill/pkill by numeric PID aimed at any krevetka bot process
    if re.search(r"\b(kill|pkill|killall)\b", norm):
        for pid in _protected_pids():
            if re.search(rf"(^|\s){re.escape(pid)}(\s|$)", norm):
                _deny(f"PID {pid} is a krevetka bot process — its lifecycle is main-only",
                      "upstream-specialist")

    # systemctl: read verbs free; mutating verbs only on upstream-* units, plus nginx reload/restart
    for _flags, verb, unit in SYSTEMCTL_RE.findall(norm):
        if verb in SYSTEMCTL_READ_VERBS:
            continue
        unit_base = (unit or "").removesuffix(".service")
        if unit_base.startswith("upstream"):
            continue
        if unit_base == "nginx" and verb in NGINX_ALLOWED_VERBS:
            continue
        _deny(f"systemctl {verb} {unit or '(no unit)'} — you may only manage upstream-* units "
              f"and reload/restart nginx", "upstream-specialist")

    if WRITE_RE.search(norm) and UPSTREAM_SENSITIVE_WRITE_RE.search(norm):
        _deny("writing to system paths (/etc, /usr, /boot, /lib, cron) is not allowed",
              "upstream-specialist")


def upstream_check_write(path):
    if not path:
        return
    ap = _abs(path)
    if _is_own(ap):
        return  # own model_config/sessions
    if any(_under(ap, root) for root in UPSTREAM_WRITE_ROOTS):
        return
    _deny(f"writes are confined to the upstream tree; refused path: {ap}", "upstream-specialist")


def upstream_check_read(path):
    if path and SECRET_RE.search(_abs(path)):
        _deny("that file holds krevetka's own secrets", "upstream-specialist")


# ---------------------------------------------------------------------------
# profile: trusted-full
# ---------------------------------------------------------------------------
# "Everything the main instance can do, EXCEPT touching krevetka itself."
#
# For a fully trusted operator who needs real system reach on this box: env files, keys, /etc,
# cron, packages, docker, and systemctl on ANY unit. Deliberately WIDER than upstream-specialist
# (which confines writes to three trees) and also wider than the baseline (which forbids
# systemctl outright) — note that simply removing a cap would REMOVE service management, not add it.
#
# What survives, always:
#   * writes into krevetka's repo — blocked (EDIT_TOOLS via _under_protected, Bash via
#     WRITE_RE + PROTECTED_ROOT). The agent cannot modify krevetka's code, persona or knowledge.
#   * killing/restarting any krevetka bot — blocked (baseline LIFECYCLE_RE + protected PIDs).
#   * krevetka's lifecycle scripts — blocked. This is also what stops the instance from lifting
#     its OWN cap: `instance.sh cap off <self>` would otherwise write into bot/caps/ from inside a
#     script, where a path-based write check never sees it.
#   * power control — a reboot takes the whole box (and krevetka) down with it.
#
# Reading krevetka's secrets is ALLOWED under this profile (unlike upstream-specialist). That is the
# deliberate cost of "full access": grant it only to someone trusted with the bot tokens and keys.
TRUSTED_DENY_BASH = [
    (r"\b(shutdown|reboot|poweroff|halt)\b|\binit\s+[06]\b",
     "power control would take krevetka down with the box"),
    (r"\bsystemctl\b[^\n]*\b(poweroff|reboot|halt|isolate)\b",
     "systemctl power verbs would take krevetka down with the box"),
    (r"\b(instance\.sh|run\.sh|restart_crab\.sh|watchdog\.sh)\b",
     "krevetka's process-lifecycle scripts are main-only (this is also what keeps a cap from "
     "lifting itself)"),
    (r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/(\s|$)",
     "rm -rf / — no"),
]


def trusted_check_bash(cmd, norm):
    for pattern, reason in TRUSTED_DENY_BASH:
        if re.search(pattern, norm):
            _deny(reason, "trusted-full")

    # kill/pkill by NUMERIC pid aimed at any krevetka bot process (the name-based regexes are
    # already handled by the baseline LIFECYCLE_RE).
    if re.search(r"\b(kill|pkill|killall)\b", norm):
        for pid in _protected_pids():
            if re.search(rf"(^|\s){re.escape(pid)}(\s|$)", norm):
                _deny(f"PID {pid} is a krevetka bot process — its lifecycle is main-only",
                      "trusted-full")


# ---------------------------------------------------------------------------

def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        # can't parse the hook payload → fail safe = allow (don't break the instance on bad input)
        sys.exit(0)

    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}

    try:
        cap = load_cap()
    except Exception:
        if tool in EDIT_TOOLS or tool == "Bash":
            _deny("the cap file is corrupt — refusing every mutating tool until it is repaired "
                  "by the main instance")
        sys.exit(0)

    if tool == "Read":
        if cap == "upstream-specialist":
            upstream_check_read(ti.get("file_path") or ti.get("notebook_path") or ti.get("path"))
        sys.exit(0)

    if tool in EDIT_TOOLS:
        path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
        if cap == "upstream-specialist":
            upstream_check_write(path)      # whitelist; strictly narrower than the baseline
            sys.exit(0)
        if _under_protected(path):
            _deny(f"refused {tool} on krevetka's protected path: {path}")
        sys.exit(0)

    if tool == "Bash":
        cmd = ti.get("command", "") or ""
        norm = _normalize(cmd)
        # Baseline first: never kill/restart the MAIN bot, regardless of writes.
        if LIFECYCLE_RE.search(norm):
            _deny("refused a command that could kill/restart the MAIN bot — process lifecycle is "
                  "main-only; a secondary instance manages only itself.")
        if cap not in ("upstream-specialist", "trusted-full") and SYSTEMCTL_MUTATE_RE.search(norm):
            # upstream-specialist replaces this with a per-unit allow-list (see upstream_check_bash)
            _deny("refused systemctl — service lifecycle is main-only for a plain secondary "
                  "instance.")
        if WRITE_RE.search(cmd):
            # ignore references to the instance's OWN data dir, then see if any protected
            # (shared-code) path remains.
            residual = cmd.replace(OWN_DATA_DIR, " ") if OWN_DATA_DIR else cmd
            if PROTECTED_ROOT in residual:
                _deny("refused Bash command that writes into krevetka's repo (shared code).")
        if cap == "upstream-specialist":
            upstream_check_bash(cmd, norm)
        elif cap == "trusted-full":
            trusted_check_bash(cmd, norm)
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
