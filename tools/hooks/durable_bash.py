#!/usr/bin/env python3
"""durable_bash — PreToolUse hook that makes a LOST long-running Bash command impossible.

Same disease as `durable_swarm.py`, different tool. A Bash command started inside the one-shot
`claude -p` turn lives in that turn's process tree; when the turn ends, teardown kills it. On
2026-08-25 the intercept-analytics report run died this way TWICE in a row — the first time as a
plain background task (it never got past reading the window), the second time only its watcher died
because the run itself had been hand-wrapped in `systemd-run --scope`.

That hand-wrapping is the point. It worked, and it is exactly the failure mode we already rejected
for swarms: a rule the model has to REMEMBER is not a mechanism. So the same conversion is applied
here by code — the command is launched through `bot/job_ctl.py` (detached, own cgroup scope,
survives the turn, reports its result back through the door it came from) and the inline call is
refused with the job id in the reason.

WHAT GETS CONVERTED — no judgement is involved, both triggers are mechanical:

  1. `run_in_background: true` — always. This is the whole class that keeps dying, and the model
     asking for a background shell is asking for something that must outlive the turn by definition.
  2. A command matching an entry in `durable_commands.json` — the growable list of known long
     runners, converted even in the foreground. It starts with the analytics report run.

The honest limit, stated so nobody expects more: an unknown slow script run in the FOREGROUND is not
covered until it is added to the list. There is no way to know in advance that a command will take
forty minutes. What is covered is every background command, always, plus everything we have learned.

Contract (Claude Code PreToolUse hook — same as durable_swarm.py / selfmod_guard.py):
    stdin  = JSON {tool_name, tool_input, cwd, ...}
    exit 0 = allow;  exit 2 = BLOCK, stderr is shown to the model as the reason

Deliberately FAIL-OPEN: any breakage here (bad payload, missing job machinery, crash) allows the
call. A broken guard must degrade to the old behaviour, never to "no shell commands at all" — this
hook sits in front of EVERY Bash call, so its blast radius if it misbehaved would be the whole box.

Pass-through cases:
  * KREVETKA_JOB_ID set — we are already inside a durable job; converting its own shell commands
    would recurse forever.
  * KREVETKA_INLINE_BASH=1 — explicit escape hatch for a background command whose output is needed
    in this same turn. Not set anywhere by default.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
JOB_CTL = REPO / "bot" / "job_ctl.py"
RUNNER = REPO / "bot" / "jobs" / "run_job.sh"      # job_ctl refuses to launch without it
REGISTRY = Path(__file__).resolve().parent / "durable_commands.json"
HOOK_LOG = REPO / "bot" / "jobs" / "durable_bash_hook.log"
LAUNCH_TIMEOUT_S = 60


def _log(msg: str) -> None:
    """A silently skipped conversion must leave a trace — that is how we learn what to add."""
    try:
        HOOK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with HOOK_LOG.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}  {msg}\n")
    except Exception:
        pass


def _allow() -> None:
    sys.exit(0)


def _block(reason: str) -> None:
    print(reason, file=sys.stderr)
    sys.exit(2)


# CLAUDE.md requires every slow inline command to be wrapped in `timeout N` — that guard exists so a
# hung command cannot freeze the TURN. Once the command becomes a durable job the turn is no longer
# waiting on it, and the wrapper turns from a guard into a self-destruct: the very first converted
# analytics run was killed at 60s with exit 124, having done nothing but read the window. Strip it.
RE_TIMEOUT = re.compile(r"^\s*timeout\s+(?:-k\s+\S+\s+|-{1,2}\S+\s+)*\d+(?:\.\d+)?[smhd]?\s+")
RE_INLINE_PREFIX = re.compile(r"^\s*KREVETKA_INLINE_BASH=1\s+")


def _strip_timeout(command: str) -> tuple[str, bool]:
    stripped = RE_TIMEOUT.sub("", command, count=1)
    return stripped, stripped != command


def _registry() -> list[dict]:
    try:
        data = json.loads(REGISTRY.read_text())
        return [e for e in data.get("commands", []) if e.get("pattern")]
    except Exception as e:
        _log(f"registry unreadable ({e!r}) — background rule still applies")
        return []


def _match(command: str) -> dict | None:
    for entry in _registry():
        try:
            if re.search(entry["pattern"], command):
                return entry
        except re.error as e:
            _log(f"bad pattern {entry['pattern']!r}: {e}")
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        _log(f"unreadable hook payload: {e!r}")
        _allow()

    if payload.get("tool_name") != "Bash":
        _allow()
    if os.environ.get("KREVETKA_JOB_ID"):
        _allow()
    if os.environ.get("KREVETKA_INLINE_BASH") == "1":
        _allow()

    ti = payload.get("tool_input") or {}
    command = str(ti.get("command") or "").strip()
    if not command:
        _allow()

    # The escape hatch has to be readable HERE, and an env-var prefix on the command is not: the hook
    # is a separate process spawned by claude, so it inherits claude's environment, not the shell
    # assignment that the command string opens with. Documenting `KREVETKA_INLINE_BASH=1 <cmd>` while
    # only checking os.environ made the hatch a no-op — found the first time it was needed, on a
    # command that matched the registry by accident (a test string that merely quoted the path).
    if RE_INLINE_PREFIX.match(command):
        _allow()

    entry = None
    if ti.get("run_in_background"):
        why = "запущена в фоне — фоновая задача умирает вместе с ходом"
    else:
        entry = _match(command)
        if not entry:
            _allow()
        why = entry.get("why") or "известная долгая команда"

    # Install-incomplete is not a policy decision: with no durable machinery there is nothing to
    # offer in exchange, so blocking would just take the shell away.
    if not JOB_CTL.exists() or not RUNNER.exists():
        _log(f"job machinery missing ({JOB_CTL.exists()=}, {RUNNER.exists()=}) — allowing inline")
        _allow()

    command, dropped_timeout = _strip_timeout(command)
    if dropped_timeout:
        _log(f"dropped the turn-protection `timeout` prefix before launching: {command[:160]}")

    label = (entry or {}).get("label") or str(ti.get("description") or "").strip() or "bash"
    cwd = payload.get("cwd")
    if not (cwd and Path(str(cwd)).is_dir()):
        cwd = str(REPO)

    try:
        res = subprocess.run(
            [sys.executable, str(JOB_CTL), "launch", "--cmd", command,
             "--label", label[:40], "--cwd", str(cwd), "--wake"],
            cwd=str(REPO), capture_output=True, text=True, timeout=LAUNCH_TIMEOUT_S)
    except Exception as e:
        _log(f"conversion crashed ({e!r}) — allowing inline so work is not blocked")
        _allow()

    out = (res.stdout or "").strip()
    if res.returncode != 0:
        err = (res.stderr or out or "").strip()
        _log(f"launch failed rc={res.returncode}: {err[:400]}  cmd={command[:200]}")
        _block(
            f"Эта команда должна пережить текущий ход ({why}), но запустить её durable-джобом не "
            f"вышло:\n{err[-600:]}\n"
            "Если это гейт запуска — посмотри `python3 bot/job_ctl.py list`, дождись или отмени "
            "активную джобу, либо запусти вручную:\n"
            f"    python3 bot/job_ctl.py launch --cmd '<команда>' --label '{label[:40]}' --wake --force"
        )

    job_id = out.splitlines()[-1].strip() if out else "(id not reported)"
    _log(f"converted Bash → durable job {job_id} ({why}) cmd={command[:200]}")
    _block(
        f"Команда запущена как durable-джоба `{job_id}` вместо этого вызова — причина: {why}.\n\n"
        f"Она работает отдельно от этого хода, в своём systemd-scope, переживёт конец сессии и сама "
        f"доложит результат, когда закончит.\n\n"
        f"НЕ запускай её повторно и НЕ жди её. Заверши ответ, сказав пользователю, что задача идёт "
        f"как джоба `{job_id}` и отчёт придёт сам. Проверить ход: "
        f"`python3 bot/job_ctl.py list`.\n"
        f"Если её вывод нужен ПРЯМО в этом ходе — или это ложное срабатывание (команда просто "
        f"упоминает путь) — перезапусти её, начав строку с `KREVETKA_INLINE_BASH=1 `."
    )


if __name__ == "__main__":
    main()
