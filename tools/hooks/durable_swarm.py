#!/usr/bin/env python3
"""durable_swarm — PreToolUse hook that makes an inline `Workflow` call IMPOSSIBLE to get wrong.

The problem it removes: a `Workflow` runs INSIDE the one-shot `claude -p` turn. When the turn emits
its final reply the process exits and a still-running swarm dies with it — the work is done, the
report is never delivered, and from the outside it looks like the swarm "hung". That happened on
2026-08-02 in the Matrix room: five agents finished, nothing was reported, and the finding only
surfaced when the user asked again and the model read the swarm's own journal back to them.

The rule "launch long swarms as durable jobs" has been in CLAUDE.md for weeks and was still missed,
because it is a rule the model has to REMEMBER. This hook turns it into something the model cannot
forget: the Workflow call is intercepted, the same script is launched through
`tools/workflow_job.py` (detached, survives the turn, reports back through the wake poller), and
the inline call is refused with the job id in the reason.

Contract (Claude Code PreToolUse hook — same as bot/selfmod_guard.py):
    stdin  = JSON {tool_name, tool_input, ...}
    exit 0 = allow;  exit 2 = BLOCK, stderr is shown to the model as the reason

Deliberately FAIL-OPEN: if anything here breaks (bad JSON, launcher missing, gate error), the tool
call is allowed through. A broken guard must degrade to the old behaviour, never to "the model can
no longer run swarms at all".

Pass-through cases:
  * KREVETKA_JOB_ID set  — we are already INSIDE a durable job's nested turn; the swarm there IS
    the job, and converting it again would recurse forever.
  * KREVETKA_INLINE_SWARM=1 — explicit escape hatch for a quick swarm whose result is needed in
    this same turn. Not set anywhere by default.
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
LAUNCHER = REPO / "tools" / "workflow_job.py"
RUNNER = REPO / "bot" / "jobs" / "run_job.sh"   # job_ctl refuses to launch without it
AUTO_DIR = REPO / "tools" / "workflows" / "auto"
HOOK_LOG = REPO / "bot" / "jobs" / "durable_swarm_hook.log"
LAUNCH_TIMEOUT_S = 90


def _log(msg: str) -> None:
    """Diagnostics for the fail-open paths — a silently skipped conversion must leave a trace."""
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


def _label_from(script: str, fallback: str = "swarm") -> str:
    """Use the workflow's own meta.name as the job label so `job_ctl list` reads sensibly."""
    m = re.search(r"name:\s*['\"]([^'\"]{1,40})['\"]", script or "")
    return m.group(1) if m else fallback


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        _log(f"unreadable hook payload: {e!r}")
        _allow()

    if payload.get("tool_name") != "Workflow":
        _allow()
    if os.environ.get("KREVETKA_JOB_ID"):
        _allow()  # already running as a durable job
    if os.environ.get("KREVETKA_INLINE_SWARM") == "1":
        _allow()  # explicit opt-out

    ti = payload.get("tool_input") or {}
    script, script_path, named = ti.get("script"), ti.get("scriptPath"), ti.get("name")

    # Install-incomplete is NOT a policy decision: if the durable machinery is not there, blocking
    # would leave the model unable to run a swarm at all. Only refuse when we can offer the durable
    # path in exchange.
    if not LAUNCHER.exists() or not RUNNER.exists():
        _log(f"durable machinery missing ({LAUNCHER.exists()=}, {RUNNER.exists()=}) — allowing inline")
        _allow()

    if named and not (script or script_path):
        _block(
            f"Inline swarms are disabled: a Workflow that runs inside this turn is killed when the "
            f"turn ends, so its report is lost. `name: \"{named}\"` cannot be converted "
            f"automatically — resolve it to a script and launch it yourself:\n"
            f"    python3 tools/workflow_job.py launch --script <path.js> --label <label>\n"
            f"It then survives the turn and reports back on its own."
        )

    try:
        AUTO_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if script_path:
            path = Path(script_path)
            if not path.is_absolute():
                path = REPO / path
        else:
            path = AUTO_DIR / f"{stamp}.js"
            path.write_text(script or "")

        cmd = [sys.executable, str(LAUNCHER), "launch", "--script", str(path)]
        label = ti.get("label") or _label_from(script or "", fallback=path.stem)
        cmd += ["--label", str(label)[:40]]
        if ti.get("args") is not None:
            args_file = AUTO_DIR / f"{stamp}.args.json"
            args_file.write_text(json.dumps(ti["args"], ensure_ascii=False))
            cmd += ["--args-file", str(args_file)]
        if ti.get("resumeFromRunId"):
            cmd += ["--resume", str(ti["resumeFromRunId"])]

        res = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                             timeout=LAUNCH_TIMEOUT_S)
    except Exception as e:
        _log(f"conversion crashed ({e!r}) — allowing inline so work is not blocked")
        _allow()

    out = (res.stdout or "").strip()
    if res.returncode != 0:
        # A refused launch is usually the concurrency gate, which is a real answer, not a glitch:
        # running the swarm inline instead would be strictly worse (it would die with the turn).
        err = (res.stderr or out or "").strip()
        _log(f"launch failed rc={res.returncode}: {err[:400]}")
        _block(
            "Inline swarms are disabled (a swarm inside a turn dies with the turn), and launching "
            f"this one as a durable job failed:\n{err[-600:]}\n"
            "If this is the concurrency gate, wait for a running job to finish "
            "(`python3 bot/job_ctl.py list`) or cancel one, then launch it yourself with "
            "`python3 tools/workflow_job.py launch --script <path.js>`."
        )

    job_id = out.splitlines()[-1].strip() if out else "(id not reported)"
    _log(f"converted inline Workflow → durable job {job_id} (script {path})")
    _block(
        f"Inline Workflow is disabled by design and this swarm was launched as durable job "
        f"`{job_id}` instead — it now runs detached, survives the end of this turn, and its result "
        f"will be delivered to you automatically when it finishes.\n\n"
        f"Do NOT call Workflow again for this task and do NOT wait for it. Finish your reply now, "
        f"telling the user the swarm is running as job `{job_id}` and that the report will arrive "
        f"on its own. Script: {path}"
    )


if __name__ == "__main__":
    main()
