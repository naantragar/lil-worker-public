#!/usr/bin/env python3
"""workflow_job.py — run a Claude Code Workflow as a DURABLE background job.

The problem this fixes: a Workflow launched with the `Workflow` tool runs inside the ephemeral
`claude -p` turn that the Telegram bot spawns per message. The moment I emit my final text reply,
that turn ends, the process exits, and any still-running workflow is killed — the report is lost.
(This is why long deep-research/audit swarms died "between messages".)

The fix: run the workflow inside a DETACHED, nested `claude -p` whose ONLY task is to drive that one
workflow to completion and print its result. Because that nested turn does nothing else, it stays
alive until the workflow finishes; because it is detached (via job_ctl's start_new_session runner),
it outlives the outer bot turn. On completion, bot.py's existing job poller wakes an isolated turn
that reports the result in my own voice (`--wake`). No change to bot.py's hot path.

Proven end-to-end: a nested `claude -p` reliably invokes the Workflow tool, blocks on it, and emits
the final returned value as its output.

Usage:
    python3 tools/workflow_job.py launch --script <scriptPath> [--args-file <json>] \
        [--label L] [--model M] [--resume <runId>] [--effort E] [--owner UID] [--force]

    python3 tools/workflow_job.py list          # thin passthrough to job_ctl list

Notes:
- `--args-file` is a path to a JSON file holding the workflow's `args` value. Passing args via a
  file (not the CLI) sidesteps the resume-args-loss footgun and any shell-quoting of large prompts.
- `--resume <runId>` re-runs an interrupted workflow; completed agents replay from cache instantly.
- Never prints secrets. Delegates detachment, ownership and notification to job_ctl.py.
"""
import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
JOB_CTL = BOT_DIR / "job_ctl.py"
DEFAULT_MODEL_CFG = BOT_DIR / "model_config.json"

# The exact allowedTools the bot grants its own turns — the nested turn needs the same set so the
# workflow's subagents can read/search/edit/run without a permission denial.
ALLOWED_TOOLS = "Read,Write,Edit,Bash,Glob,Grep,WebFetch,WebSearch,Task,Agent,Workflow,Skill"


def _default_model() -> str:
    try:
        return str(json.loads(DEFAULT_MODEL_CFG.read_text()).get("model", "sonnet")).strip() or "sonnet"
    except Exception:
        return "sonnet"


def _inject_args(script_text: str, args_json: str) -> str:
    """Insert `globalThis.args = <literal>;` immediately after the mandatory `export const meta = {…}`
    block, so the workflow's global `args` is set DETERMINISTICALLY by the script itself.

    Why not the Workflow tool's `args` parameter: the nested model reliably stringifies it (passes
    `"{\\"who\\":…}"` instead of the object), so `args.foo` comes back undefined. Verified: model
    stringifies even under an explicit anti-stringification instruction. Injecting a real JS literal
    into the script removes the model from the args path entirely — works for object, array or string
    args, and stays cache-consistent across a resume (same injection both runs)."""
    m = re.search(r"export\s+const\s+meta\s*=\s*\{", script_text)
    if not m:
        raise ValueError("workflow script must begin with `export const meta = { … }`")
    i = m.end() - 1  # index of the opening brace of the meta literal
    depth, n = 0, len(script_text)
    while i < n:
        ch = script_text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        raise ValueError("could not find the end of the meta literal (unbalanced braces)")
    j = i + 1
    if j < n and script_text[j] == ";":  # skip an optional semicolon after the meta object
        j += 1
    # meta is a pure literal (no functions), so brace-matching it is safe.
    return script_text[:j] + f"\nglobalThis.args = {args_json};\n" + script_text[j:]


def _build_prompt(script: str, resume: str | None) -> str:
    """Instruction for the nested claude: call Workflow once, wait, print ONLY the final result.
    Args are already baked into the script (see _inject_args), so nothing to pass here."""
    call = ["1. Invoke the Workflow tool in ONE call with:",
            f"     - scriptPath = {script}"]
    if resume:
        call.append(f"     - resumeFromRunId = {resume} (completed agents replay from cache)")
    return "\n".join([
        "You are a headless workflow runner. Do EXACTLY this and nothing else:",
        "",
        *call,
        "",
        "2. Wait for the workflow to finish (you are notified on completion).",
        "3. Output ONLY the workflow's final returned value as JSON — no preamble, no explanation,",
        "   no markdown fences. If it is already text, print it verbatim.",
        "",
        "Do not do any other work. Do not ask questions.",
    ])


def cmd_launch(a: argparse.Namespace) -> None:
    script = str(Path(a.script).resolve())
    if not Path(script).exists():
        sys.exit(f"script not found: {script}")

    # Args, if any, are baked into a COPY of the script so the global `args` is set deterministically
    # (the nested model cannot be trusted to pass the tool `args` param as anything but a string).
    if a.args_file:
        args_path = Path(a.args_file).resolve()
        if not args_path.exists():
            sys.exit(f"args file not found: {args_path}")
        try:
            args_json = json.dumps(json.loads(args_path.read_text()), ensure_ascii=False)
        except Exception as e:
            sys.exit(f"args file is not valid JSON: {e}")
        try:
            wrapped = _inject_args(Path(script).read_text(), args_json)
        except ValueError as e:
            sys.exit(f"cannot inject args: {e}")
        # A stable sibling path so a resume of the same job re-wraps identically (cache-consistent).
        wrapped_path = Path(script).with_name(Path(script).stem + ".wfjob-args.js")
        wrapped_path.write_text(wrapped)
        script = str(wrapped_path)

    model = a.model or _default_model()
    prompt = _build_prompt(script, a.resume)

    # The nested, headless claude that drives the workflow. Same allowedTools as the bot's turns.
    inner = [
        "claude", "-p", prompt,
        "--model", model,
        "--allowedTools", ALLOWED_TOOLS,
    ]
    if a.effort:
        inner += ["--effort", a.effort]
    # Quote-safe assembly: job_ctl stores the cmd base64, but we still hand it a single shell string.
    # The env prefix is the whole point of the durability fix: a `claude -p` turn kills still-running
    # background tasks after a 600s ceiling ("Background tasks still running after 600s; terminating"),
    # which decapitated long swarms even inside the detached job. 0 = wait indefinitely; the runner
    # turn does nothing but drive this one workflow, so there is nothing else to time out.
    inner_cmd = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 " + " ".join(shlex.quote(x) for x in inner)

    label = a.label or f"workflow:{Path(script).stem}"
    ctl = [sys.executable, str(JOB_CTL), "launch", "--cmd", inner_cmd, "--label", label, "--wake"]
    if a.owner is not None:
        ctl += ["--owner", str(a.owner)]
    if a.force:
        ctl.append("--force")

    # job_ctl prints the job id on success; surface it verbatim.
    res = subprocess.run(ctl, capture_output=True, text=True)
    sys.stdout.write(res.stdout)
    sys.stderr.write(res.stderr)
    sys.exit(res.returncode)


def cmd_list(_a: argparse.Namespace) -> None:
    subprocess.run([sys.executable, str(JOB_CTL), "list"])


def main() -> None:
    ap = argparse.ArgumentParser(description="run a Claude Code Workflow as a durable background job")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("launch")
    p.add_argument("--script", required=True, help="path to the workflow script (.js)")
    p.add_argument("--args-file", help="path to a JSON file holding the workflow's `args` value")
    p.add_argument("--label", help="short human label shown in the wake report")
    p.add_argument("--model", help="model for the nested runner (default: bot/model_config.json)")
    p.add_argument("--resume", help="runId of an interrupted workflow to resume (cache replays)")
    p.add_argument("--effort", help="reasoning effort for the nested runner (low|medium|high|xhigh|max)")
    p.add_argument("--owner", type=int, help="Telegram user id to notify (default: first ALLOWED_USERS)")
    p.add_argument("--force", action="store_true", help="launch even if another job is active")
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("list")
    p.set_defaults(func=cmd_list)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
