#!/usr/bin/env python3
"""Per-room model switch for the Matrix door — the switch I can throw MYSELF, mid-conversation.

The Telegram door has one global dial (`bot/model_config.json`), and it is shared: moving it swings
every Matrix room and Telegram at once. `matrix/bot/room_profiles.json` already lets a room pin its
own model, but editing that JSON by hand is exactly the kind of thing that loses a `label` or a
prompt list when done in a hurry. This tool is the safe door to that field: it preserves every other
key, writes atomically, and knows which room it is in (`KREVETKA_ROOM`, set by the bridge for the
turn), so from inside a room the command needs no arguments at all.

    python3 tools/room_model.py show                 # this room + everything else
    python3 tools/room_model.py set claude-sonnet-5  # pin THIS room
    python3 tools/room_model.py set opus --effort high --room '!abc:server'
    python3 tools/room_model.py clear                # back to the global config

An id that is not in KNOWN is smoke-tested (`claude -p --model <id>`) before it is written — model
names outrun my knowledge cutoff in both directions, so "I have not heard of it" is not a verdict,
and neither is "it looks right". `--force` skips the test; nothing writes an id that failed it.

No restart: `claude_bridge._model()` re-reads the file on every turn.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROFILES = REPO / "matrix" / "bot" / "room_profiles.json"
GLOBAL_CFG = REPO / "bot" / "model_config.json"

# Ids known good at the time of writing. Not a whitelist of what may be used — anything else is
# allowed too, it just has to pass the smoke test first.
KNOWN = {"claude-opus-5", "claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"}
ALIASES = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save(profiles: dict) -> None:
    """Atomic: a half-written profiles file would strip every room of its prompt at once."""
    PROFILES.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=PROFILES.parent, delete=False,
                                     encoding="utf-8") as tmp:
        json.dump(profiles, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(PROFILES)


def _room(arg: str | None) -> str:
    room = arg or os.environ.get("KREVETKA_ROOM") or ""
    if not room:
        sys.exit("no room: pass --room '!id:server' (KREVETKA_ROOM is only set inside a Matrix turn)")
    return room


def _global_model() -> str:
    return str(_load(GLOBAL_CFG, {}).get("model", "")).strip() or "(unset)"


def _verify(model: str) -> bool:
    """`claude -p --model X` must actually answer. Cheap, and the only honest check there is."""
    try:
        r = subprocess.run([os.environ.get("CLAUDE_BIN", "claude"), "-p", "--model", model,
                            "Reply with OK and nothing else."],
                           capture_output=True, text=True, timeout=120, cwd=str(REPO))
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def cmd_show(args) -> None:
    profiles = _load(PROFILES, {})
    room = args.room or os.environ.get("KREVETKA_ROOM")
    print(f"global (bot/model_config.json): {_global_model()}")
    if room:
        entry = profiles.get(room) or {}
        pin = entry.get("model")
        print(f"this room {room}"
              f"{' [' + entry['label'] + ']' if entry.get('label') else ''}: "
              f"{pin + ' (pinned)' if pin else _global_model() + ' (inherited)'}"
              f"{', effort ' + entry['effort'] if entry.get('effort') else ''}")
    others = [(r, e) for r, e in profiles.items() if r != room]
    if others:
        print("other rooms:")
        for r, e in others:
            print(f"  {r} {'[' + e.get('label', '') + '] ' if e.get('label') else ''}"
                  f"-> {e.get('model') or 'inherited'}"
                  f"{', effort ' + e['effort'] if e.get('effort') else ''}")


def cmd_set(args) -> None:
    room = _room(args.room)
    model = ALIASES.get(args.model.strip(), args.model.strip())
    if args.effort and args.effort not in EFFORTS:
        sys.exit(f"unknown effort '{args.effort}' (use: {', '.join(sorted(EFFORTS))})")
    if model not in KNOWN and not args.force:
        print(f"'{model}' is not in the known list — smoke-testing it...", flush=True)
        if not _verify(model):
            sys.exit(f"'{model}' did not answer — NOT written. Check the id, or --force it.")
        print("smoke test passed")
    profiles = _load(PROFILES, {})
    entry = dict(profiles.get(room) or {})
    was = entry.get("model") or f"{_global_model()} (inherited)"
    entry["model"] = model
    if args.effort:
        entry["effort"] = args.effort
    elif args.clear_effort:
        entry.pop("effort", None)
    if args.label and not entry.get("label"):
        entry["label"] = args.label
    profiles[room] = entry
    _save(profiles)
    print(f"{room}: {was} -> {model}"
          f"{', effort ' + entry['effort'] if entry.get('effort') else ''}"
          " (takes effect on the next message, no restart)")


def cmd_clear(args) -> None:
    room = _room(args.room)
    profiles = _load(PROFILES, {})
    entry = dict(profiles.get(room) or {})
    if not entry.pop("model", None) and not entry.pop("effort", None):
        print(f"{room}: nothing pinned, already on the global {_global_model()}")
        return
    entry.pop("effort", None)
    if entry:
        profiles[room] = entry
    else:
        profiles.pop(room, None)
    _save(profiles)
    print(f"{room}: pin removed -> global {_global_model()}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Per-room model pin for the Matrix door")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("show", help="effective model for this room and every other")
    p.add_argument("--room")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("set", help="pin a model for one room")
    p.add_argument("model", help="id (claude-sonnet-5) or alias (opus/sonnet/haiku)")
    p.add_argument("--room")
    p.add_argument("--effort", help=f"one of: {', '.join(sorted(EFFORTS))}")
    p.add_argument("--clear-effort", action="store_true", help="drop an effort pin")
    p.add_argument("--label", help="name for a room that has none yet")
    p.add_argument("--force", action="store_true", help="skip the smoke test")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("clear", help="drop the pin, fall back to bot/model_config.json")
    p.add_argument("--room")
    p.set_defaults(func=cmd_clear)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
