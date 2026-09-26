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

WHAT IS NEVER CONVERTED — the `never` list in the same file, and it outranks BOTH triggers above,
including `run_in_background: true`. A durable job is a task: it finishes and reports. A dev server
is a service: it never finishes, so the job sits at `running` forever, holds its room's launch gate,
and can only be ended by hand — which is then delivered as "задача скасована". Three of those piled
up in one evening (16.09.2026) from `npm run dev`. Inline in the background is the RIGHT place for a
service: it lives as long as the turn that needs it, and the turn's teardown cleans it up.

The honest limit, stated so nobody expects more: an unknown slow script run in the FOREGROUND is not
covered until it is added to the list. There is no way to know in advance that a command will take
forty minutes. What is covered is every background command, always, plus everything we have learned.

MENTIONING a long runner is not RUNNING it (fixed 2026-08-30). The registry match used to be a
plain substring search, so `ls tools/analytics_pipeline.sh` became a durable job — the path was in
the string, and that was the whole test. A match now has to survive four checks: it must not sit
inside a quoted string, its segment's verb must not be a mere inspector (ls/grep/cat/git/diff...),
it must stand in command position behind an interpreter (or be the executable itself), and the entry
must not be vetoed by its own `not_if` (which is how `--render-only`, a one-second rebuild of the
same report, stops being treated as a forty-minute run). The check is exercised by 25 cases in
`tools/hooks/test_durable_bash.py` — ten that must convert, fifteen that must not. Run it after ANY
change here: the first version of these checks silently stopped converting two REAL runners (an
absolute path, and a pattern that starts mid-path), and only the case list caught it.

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
# Not anchored at all. Anchoring was tried twice and failed twice for the same reason: a real command
# has the assignment after a `cd` line, or after a `;`, or inside a loop — never at offset zero. This
# is an explicit opt-in marker that only I write, so a plain substring is both sufficient and
# predictable, which an anchor demonstrably was not.
RE_INLINE_PREFIX = re.compile(r"\bKREVETKA_INLINE_BASH=1\b")


def _strip_timeout(command: str) -> tuple[str, bool]:
    stripped = RE_TIMEOUT.sub("", command, count=1)
    return stripped, stripped != command


def _registry(key: str = "commands") -> list[dict]:
    try:
        data = json.loads(REGISTRY.read_text())
        return [e for e in data.get(key, []) if e.get("pattern")]
    except Exception as e:
        _log(f"registry unreadable ({e!r}) — background rule still applies")
        return []


def _never(command: str) -> dict | None:
    """Is this a SERVICE rather than a task? Then it must stay inline, background or not.

    A durable job is defined by finishing: it runs, exits, and its result is delivered. A dev server
    never exits, so converting one yields a job pinned at `running` forever — it holds its room's
    launch gate and its report can never come. The only exit is a manual cancel, which then arrives
    as "задача скасована". On 16.09.2026 that happened three times in one evening with `npm run dev`.

    Left inline in the background the same server behaves exactly right: it lives as long as the turn
    that wanted to look at the page, and the turn's teardown disposes of it.

    Checked BEFORE both triggers, so it outranks `run_in_background: true` as well. A plain search is
    enough here — the worst a false positive can do is leave a command inline, which is the default
    for almost every command anyway; the patterns are kept tight so it cannot disarm a real runner.
    """
    for entry in _registry("never"):
        try:
            if re.search(entry["pattern"], command):
                return entry
        except re.error as e:
            _log(f"bad never-pattern {entry['pattern']!r}: {e}")
    return None


# A registry pattern is a PATH, and a path in a command string is not necessarily a command: on
# 2026-08-30 a plain `ls tools/analytics_pipeline.sh` was launched as a durable job, because the
# pattern was searched anywhere in the string. Mentioning a long runner — listing it, grepping it,
# diffing it, quoting it in an echo — is not running it. Two independent checks now separate the
# two, and both must pass before anything is converted.
#
# 1. The match has to sit where a command sits: at the start of a segment (`;`, `&&`, `||`, `|`,
#    newline), after the env assignments and wrappers that may precede it, and immediately behind an
#    interpreter — `python3 X`, `bash X`, `./X`. Everything the registry lists is a script; a script
#    that is being RUN always has one of those in front of it.
RE_INVOKED = re.compile(
    r"(?:^|[;&|]+|\n)\s*"                                  # start of a command segment
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"                  # VAR=value prefixes
    r"(?:(?:timeout|sudo|nice|ionice|nohup|env|time|stdbuf|setsid)\s+"
    r"(?:-\S+\s+|\d+(?:\.\d+)?[smhd]?\s+)*)*"              # wrappers with their own flags
    r"(?:(?:python3?|bash|sh)\s+)?$"                       # interpreter, or the script run directly
)


def _token_start(command: str, idx: int) -> int:
    """Back up to the start of the whitespace-delimited argument the match landed inside.

    Registry patterns begin mid-path (`tools/analytics_run.py`), so a match can start in the middle
    of `~/lil_worker/tools/analytics_run.py` — and then the interpreter would be judged
    against `…/lil_worker/` instead of against `python3 `. Both real long runners were skipped this
    way the first time the check was written, which is what the case list is for.
    """
    while idx > 0 and command[idx - 1] not in " \t\n;&|'\"":
        idx -= 1
    return idx


def _quoted(prefix: str) -> bool:
    """Is the match inside a quoted string? Then it is text, not a command — `echo 'python3 x.py'`."""
    return prefix.count("'") % 2 == 1 or prefix.count('"') % 2 == 1
# 2. Belt and braces: if the segment's leading verb only LOOKS at things, never convert. These
#    finish in milliseconds, so a conversion is always wrong, and they are exactly the commands that
#    carry a long runner's path as an argument.
READ_ONLY_VERBS = {
    "ls", "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "wc", "stat",
    "file", "find", "echo", "printf", "diff", "cmp", "md5sum", "sha256sum", "readlink", "realpath",
    "dirname", "basename", "which", "type", "sed", "awk", "git", "cp", "mv", "chmod", "test",
}
RE_SEGMENT_HEAD = re.compile(
    r"(?:^|[;&|]+|\n)\s*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:(?:timeout|sudo|nice|ionice|nohup|env|time|stdbuf|setsid)\s+"
    r"(?:-\S+\s+|\d+(?:\.\d+)?[smhd]?\s+)*)*"
    r"([^\s;&|]+)\s*$"
)


def _verb_before(prefix: str) -> str:
    """The executable of the segment the match sits in — '' if it cannot be read."""
    m = RE_SEGMENT_HEAD.search(prefix)
    return Path(m.group(1)).name if m else ""


def _match(command: str) -> dict | None:
    for entry in _registry():
        try:
            m = re.search(entry["pattern"], command)
        except re.error as e:
            _log(f"bad pattern {entry['pattern']!r}: {e}")
            continue
        if not m:
            continue
        # A flag that makes the same script fast (`--render-only` rebuilds the files from saved
        # events with no model calls at all — one second) vetoes the entry.
        veto = entry.get("not_if")
        if veto:
            try:
                if re.search(veto, command):
                    _log(f"not converted, `not_if` matched ({veto}): {command[:160]}")
                    continue
            except re.error as e:
                _log(f"bad not_if {veto!r}: {e}")
        prefix = command[:_token_start(command, m.start())]
        if _quoted(prefix):
            _log(f"not converted, the path is inside a quoted string: {command[:160]}")
            continue
        verb = _verb_before(prefix)
        if verb in READ_ONLY_VERBS:
            _log(f"not converted, `{verb}` only inspects the path: {command[:160]}")
            continue
        if not RE_INVOKED.search(prefix):
            _log(f"not converted, path mentioned but not invoked: {command[:160]}")
            continue
        return entry
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
    # search(), not match(): `re.M` makes `^` match at every line start, but `match()` only ever
    # tries position 0, so the multi-line form stayed broken after the anchor was widened.
    if RE_INLINE_PREFIX.search(command):
        _allow()

    # A service is not a task. This outranks the background rule on purpose — see _never().
    service = _never(command)
    if service:
        _log(f"not converted, {service.get('why') or 'служба, а не задача'}: {command[:160]}")
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

    # A registry entry may carry a `followup`: text injected into the owner's LIVE chat once the
    # job's report has landed, as if he had typed it. That is how the report's self-check runs
    # without him asking for it every day — and it goes into the live session, not the isolated
    # wake turn, because the check is only as good as the context behind it.
    argv = [sys.executable, str(JOB_CTL), "launch", "--cmd", command,
            "--label", label[:40], "--cwd", str(cwd), "--wake"]
    followup = ((entry or {}).get("followup") or "").strip()
    if followup:
        argv += ["--followup", followup]
    try:
        res = subprocess.run(
            argv, cwd=str(REPO), capture_output=True, text=True, timeout=LAUNCH_TIMEOUT_S)
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
            f"Если это гейт запуска — посмотри `python3 {JOB_CTL} list`, дождись или отмени "
            "активную джобу, либо запусти вручную:\n"
            f"    python3 {JOB_CTL} launch --cmd '<команда>' --label '{label[:40]}' --wake --force"
        )

    job_id = out.splitlines()[-1].strip() if out else "(id not reported)"
    _log(f"converted Bash → durable job {job_id} ({why}) cmd={command[:200]}")
    _block(
        f"Команда запущена как durable-джоба `{job_id}` вместо этого вызова — причина: {why}.\n\n"
        f"Она работает отдельно от этого хода, в своём systemd-scope, переживёт конец сессии и сама "
        f"доложит результат, когда закончит.\n\n"
        f"НЕ запускай её повторно и НЕ жди её. Заверши ответ, сказав пользователю, что задача идёт "
        f"как джоба `{job_id}` и отчёт придёт сам. Проверить ход: "
        f"`python3 {JOB_CTL} list`.\n"
        f"Если её вывод нужен ПРЯМО в этом ходе — или это ложное срабатывание (команда просто "
        f"упоминает путь) — перезапусти её, начав строку с `KREVETKA_INLINE_BASH=1 `."
    )


if __name__ == "__main__":
    main()
