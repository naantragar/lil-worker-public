#!/usr/bin/env python3
"""Durable, append-only action ledger for agent side effects (commit/push/deploy/restart).

Why: after a session reset or a server restart the agent's conversation context is gone. Git
history + on-disk transcripts survive, but nothing *automatically* tells future-me "this action
was made by an agent (me, or a subagent I spawned) in session X". This ledger is that durable
record — read it and a "ghost" action becomes legible instead of looking like a third party's.

The record captures machine-derived provenance from the runtime env (inherited by child procs,
including git hooks and subagent bash):
  CLAUDE_CODE_SESSION_ID   -> session
  CLAUDE_CODE_CHILD_SESSION -> child_session (present  ==> the action came from a subagent)
  CLAUDE_MODEL / AI_AGENT   -> model / ai_agent

Design rules:
  * NEVER raise into the caller. A broken ledger must never break a commit/deploy. All failures
    are swallowed; exit code is always 0 on the write paths.
  * Codename-free: this file syncs to the public repo. The default path and labels are neutral.
    The codename appears only in the ledger DATA (at /root, never synced) and in commit trailers.

Ledger: JSONL, one object per line. Default /root/.claude/agent-actions.jsonl (env AGENT_LEDGER).
"""
import argparse
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone

LEDGER = os.environ.get("AGENT_LEDGER", "/root/.claude/agent-actions.jsonl")


def _is_subagent(session, child) -> bool:
    """True only when the child-session env looks like a REAL distinct subagent id.

    Note: CLAUDE_CODE_CHILD_SESSION is set to a trivial flag (e.g. "1") even in the main
    agent, so mere presence means nothing. We treat it as a subagent only when it is a
    long, id-shaped value that differs from the main session id.
    """
    if not child:
        return False
    c = str(child)
    if c in ("0", "1", "true", "false", "yes", "no"):
        return False
    return len(c) >= 8 and c != (session or "")


def _env_provenance() -> dict:
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    child = os.environ.get("CLAUDE_CODE_CHILD_SESSION")
    return {
        "session": session,
        "child_session": child,
        "from_subagent": _is_subagent(session, child),
        "model": os.environ.get("CLAUDE_MODEL"),
        "ai_agent": os.environ.get("AI_AGENT"),
    }


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append(entry: dict) -> None:
    """Append one JSON line. Best-effort; swallow everything."""
    try:
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:  # never propagate
        try:
            sys.stderr.write(f"[action_log] append failed (ignored): {e}\n")
        except Exception:
            pass


def _git(repo: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def cmd_record(a: argparse.Namespace) -> None:
    prov = _env_provenance()
    actor = a.actor or ("agent" if (prov["session"] or os.environ.get("CLAUDECODE")) else "external")
    _append({
        "ts": _now(),
        "action": a.action,
        "repo": a.repo,
        "ref": a.ref or "-",
        "summary": a.summary or "",
        "actor": actor,
        "via": a.via,
        "host": socket.gethostname(),
        **prov,
    })


def cmd_record_commit(a: argparse.Namespace) -> None:
    """Called by the post-commit hook. Reads subject + trailers from git itself."""
    repo, rev = a.repo, a.rev or "HEAD"
    h = _git(repo, "rev-parse", "--short", rev) or rev
    subject = _git(repo, "show", "-s", "--format=%s", rev)
    trailers = _git(repo, "show", "-s", "--format=%(trailers:only)", rev)
    author_email = _git(repo, "show", "-s", "--format=%ae", rev)
    # Machine-detectable agent marker (no codename): Anthropic noreply in the trailers.
    is_agent = "noreply@anthropic.com" in trailers
    prov = _env_provenance()
    _append({
        "ts": _now(),
        "action": "commit",
        "repo": repo,
        "ref": h,
        "summary": subject,
        "actor": "agent" if is_agent else "external",
        "author_email": author_email,
        "via": "hook",
        "host": socket.gethostname(),
        **prov,
    })


def _read_lines() -> list:
    try:
        with open(LEDGER, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except FileNotFoundError:
        return []
    except Exception:
        return []


def _fmt(e: dict) -> str:
    who = e.get("actor", "?")
    if e.get("from_subagent"):
        who += "/subagent"
    return (f"{e.get('ts','?')}  {e.get('action','?'):8}  {who:16}  "
            f"{e.get('ref','-'):10}  {os.path.basename(e.get('repo','') or '')}  "
            f"{e.get('summary','')}")


def cmd_tail(a: argparse.Namespace) -> None:
    rows = _read_lines()
    for e in rows[-a.n:]:
        print(_fmt(e))


def cmd_search(a: argparse.Namespace) -> None:
    term = a.term.lower()
    for e in _read_lines():
        if term in json.dumps(e, ensure_ascii=False).lower():
            print(_fmt(e))


def cmd_show(a: argparse.Namespace) -> None:
    for e in _read_lines():
        if a.repo and a.repo not in (e.get("repo") or ""):
            continue
        print(_fmt(e))


def main() -> int:
    p = argparse.ArgumentParser(description="Durable agent action ledger")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="record an arbitrary side effect")
    r.add_argument("--action", required=True, choices=["commit", "push", "deploy", "restart", "note"])
    r.add_argument("--repo", required=True)
    r.add_argument("--ref", default="")
    r.add_argument("--summary", default="")
    r.add_argument("--actor", default="")
    r.add_argument("--via", default="manual")
    r.set_defaults(func=cmd_record)

    rc = sub.add_parser("record-commit", help="(hook) record a commit by hash")
    rc.add_argument("repo")
    rc.add_argument("rev", nargs="?", default="HEAD")
    rc.set_defaults(func=cmd_record_commit)

    t = sub.add_parser("tail", help="print the last N entries")
    t.add_argument("n", nargs="?", type=int, default=20)
    t.set_defaults(func=cmd_tail)

    s = sub.add_parser("search", help="grep the ledger")
    s.add_argument("term")
    s.set_defaults(func=cmd_search)

    sh = sub.add_parser("show", help="filter by repo")
    sh.add_argument("--repo", default="")
    sh.set_defaults(func=cmd_show)

    args = p.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # never fail the caller
        sys.stderr.write(f"[action_log] fatal (ignored): {e}\n")
        sys.exit(0)
