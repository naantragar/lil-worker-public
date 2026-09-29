#!/usr/bin/env python3
"""Who is committing, and on what — resolved the way the RUNTIME resolves it, not from a constant.

Honest attribution is the entire point of the ledger and the commit trailer, and a hardcoded model
name rots silently. It already had: the trailer in `ops/sync_repos.sh` said `Claude Opus 4.8`
because that string was typed in once, so every commit after we moved to Opus 5 was signed with the
wrong model — including the ones that said, in their own text, which model wrote them.

Reading `$CLAUDE_MODEL` is not enough either. That is the STATIC value from `instance.env`, while
the documented way to switch a model is to edit `model_config.json`, which takes effect on the next
message with no restart. The env is stale BY DESIGN the moment anyone uses the supported mechanism.

So this mirrors the two runtimes exactly, and must keep mirroring them:
  · Telegram — `bot/krevetka.py::load_claude_model`: `<LIL_WORKER_DATA_DIR>/model_config.json`,
    falling back to `$CLAUDE_MODEL`.
  · Matrix — `matrix/bot/claude_bridge.py::_model`: the room's pin in `room_profiles.json`,
    falling back to the shared `bot/model_config.json`.
Effort follows the same shape: the per-instance / per-room value if set, else the global
`~/.claude/settings.json` effortLevel. (`$CLAUDE_EFFORT` is deliberately NOT trusted — it is
ambient, inherited from whatever shell started the bot, and the game instance's own env file
documents that it lies.)

Usage:
    python3 tools/current_model.py             # claude-opus-5
    python3 tools/current_model.py --trailer   # Co-Authored-By: krevetka/twin2 (claude-opus-5, …
    python3 tools/current_model.py --json      # {"door": "telegram", "who": "twin2", …}

Never raises: attribution must not be able to break a commit. Anything unresolvable comes back as
`unknown`, which is still an honest answer and is greppable.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # <repo>/tools/.. -> <repo>
GLOBAL_MODEL = ROOT / "bot" / "model_config.json"
ROOM_PROFILES = ROOT / "matrix" / "bot" / "room_profiles.json"
SETTINGS = Path(os.path.expanduser("~/.claude/settings.json"))
VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _model_from(path: Path) -> str:
    return str(_json(path).get("model") or "").strip()


def _global_effort() -> str:
    e = str(_json(SETTINGS).get("effortLevel") or "").strip().lower()
    return e if e in VALID_EFFORTS else ""


def resolve() -> dict:
    """-> {door, who, model, effort}. `who` is the instance name or the room label."""
    room = os.environ.get("KREVETKA_ROOM", "").strip()
    env_model = str(os.environ.get("CLAUDE_MODEL") or "").strip()

    if room:
        cfg = _json(ROOM_PROFILES).get(room) or {}
        model = str(cfg.get("model") or "").strip() or _model_from(GLOBAL_MODEL) or env_model
        effort = str(cfg.get("effort") or "").strip().lower()
        if effort not in VALID_EFFORTS:
            effort = _global_effort()
        # A room without a label is still identifiable by the head of its id.
        who = str(cfg.get("label") or "").strip() or room[:12]
        return {"door": "matrix", "who": who, "model": model or "unknown", "effort": effort}

    data_dir = os.environ.get("LIL_WORKER_DATA_DIR", "").strip()
    if data_dir:
        model = _model_from(Path(data_dir) / "model_config.json") or env_model
        effort = str(os.environ.get("LIL_WORKER_EFFORT") or "").strip().lower()
        if effort not in VALID_EFFORTS:
            effort = _global_effort()
        who = os.environ.get("LIL_WORKER_INSTANCE", "").strip() or "lil_worker"
        return {"door": "telegram", "who": who, "model": model or "unknown", "effort": effort}

    # A plain shell, a cron job, a durable job that lost its env: the shared config is the best
    # honest answer, and naming no instance is better than naming the wrong one.
    return {
        "door": "shell",
        "who": "",
        "model": _model_from(GLOBAL_MODEL) or env_model or "unknown",
        "effort": _global_effort(),
    }


def trailer(info: dict | None = None) -> str:
    """The Co-Authored-By line.

    Two things in it are load-bearing and must not be dropped by a future edit: `krevetka`, so I
    recognise my own commits at a glance, and `noreply@anthropic.com`, which is the machine-readable
    marker that tells the post-commit hook this was an agent and not a human.
    """
    i = info or resolve()
    who = f"/{i['who']}" if i["who"] else ""
    detail = i["model"] + (f", effort {i['effort']}" if i["effort"] else "")
    return f"Co-Authored-By: krevetka{who} ({detail}) <noreply@anthropic.com>"


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve the model this door is actually running on")
    ap.add_argument("--trailer", action="store_true", help="print the Co-Authored-By line")
    ap.add_argument("--json", action="store_true", help="print the full resolution")
    a = ap.parse_args()
    info = resolve()
    if a.json:
        print(json.dumps(info, ensure_ascii=False))
    elif a.trailer:
        print(trailer(info))
    else:
        print(info["model"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # attribution must never break the caller
        sys.stderr.write(f"[current_model] fatal (ignored): {e}\n")
        print("unknown")
        sys.exit(0)
