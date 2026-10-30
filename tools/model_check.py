#!/usr/bin/env python3
"""Which model and effort every door and every scheduled job ACTUALLY runs on — one screen.

Built 24.10.2026 when everything moved to Opus 5.5 medium: the model is chosen in a dozen places
(per-instance model_config.json, instance.env, Matrix room pins, script defaults, a hardcoded
constant in watch/runner.py, the global effortLevel) and nothing showed them side by side. This
resolves each one the way its runtime does and flags anything off the target.

    python3 tools/model_check.py              # inventory, flags off-target lines
    python3 tools/model_check.py --smoke      # + one tiny live call per distinct model/effort pair
    python3 tools/model_check.py --target claude-opus-5 --effort high   # check against another target

Effort "global(x)" means nothing pins it, so it silently follows ~/.claude/settings.json — flagged,
because a change to the global level would move that door without anyone deciding it.
Read-only. Exit code 0 always (it reports, it does not gate).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INST = ROOT / "bot" / "instances"
SETTINGS = Path(os.path.expanduser("~/.claude/settings.json"))
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def _json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _env(p: Path) -> dict:
    out = {}
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Z_]+)=(.*)", line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"')
    except Exception:
        pass
    return out


GLOBAL_EFFORT = str(_json(SETTINGS).get("effortLevel") or "").lower()


def _eff(*cands: str) -> str:
    for c in cands:
        c = (c or "").strip().lower()
        if c in EFFORTS:
            return c
    return f"global({GLOBAL_EFFORT or '?'})"


def _alive(pidfile: Path) -> str:
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, 0)
        return "up"
    except Exception:
        return "down"


def telegram() -> list[dict]:
    rows = []
    # main bot: run.sh scrubs LIL_WORKER_EFFORT, so only the config file or the global level count
    cfg = _json(ROOT / "bot" / "model_config.json")
    rows.append({"what": "telegram: lil_worker (main)", "model": cfg.get("model", "?"),
                 "effort": _eff(cfg.get("effort")), "state": _alive(ROOT / "bot" / "lil_worker.pid")})
    for d in sorted(p for p in INST.iterdir() if p.is_dir()):
        cfg, env = _json(d / "model_config.json"), _env(d / "instance.env")
        if not cfg.get("model") and not env.get("CLAUDE_MODEL"):
            continue  # push-only bots (alerts_red/grey) never call a model
        rows.append({"what": f"telegram: {d.name}",
                     "model": cfg.get("model") or env.get("CLAUDE_MODEL", "?"),
                     "effort": _eff(cfg.get("effort"), env.get("LIL_WORKER_EFFORT")),
                     "state": _alive(d / "lil_worker.pid")})
    return rows


def matrix() -> list[dict]:
    shared = _json(ROOT / "bot" / "model_config.json").get("model", "?")
    rows = []
    for rid, cfg in _json(ROOT / "matrix" / "bot" / "room_profiles.json").items():
        # claude_bridge passes --effort only when the room pins one; otherwise the CLI uses global
        rows.append({"what": f"matrix: {cfg.get('label') or rid[:10]}",
                     "model": cfg.get("model") or shared, "effort": _eff(cfg.get("effort")),
                     "state": ""})
    return rows


# Scheduled / pipeline jobs: the default a script runs with when called the way cron/the cycle calls it.
SCRIPTS = [
    ("cron 15:10: red digest", "tools/alerts/report1500.sh",
     r'MODEL="\$\{ALERTS_MODEL:-([^}]+)\}"', r'EFFORT="\$\{ALERTS_EFFORT:-([^}]+)\}"'),
    ("report: generation run.py", "tools/analytics2/run.py",
     r'"--model", default="([^"]+)"', r'"--effort", default="([^"]+)"'),
    ("report: audit", "tools/upstream/report_audit.py",
     r'"--model", default="([^"]+)"', r'"--effort", "([a-z]+)"'),
    ("report: cleanup", "tools/upstream/report_cleanup.py",
     r'"--model", default="([^"]+)"', r'"--effort", "([a-z]+)"'),
    ("timer 15 min: watch-runner", "watch/runner.py",
     r'^MODEL = "([^"]+)"', r'^EFFORT = "([^"]+)"'),
    ("dream stories: gen.py", "~/dream_story_bot/gen.py",
     r'"--model", default="([^"]+)"', r'"--effort", default="([^"]+)"'),
]


def scripts() -> list[dict]:
    rows = []
    for what, path, mrx, erx in SCRIPTS:
        p = Path(path) if path.startswith("/") else ROOT / path
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            rows.append({"what": what, "model": "missing file", "effort": "", "state": ""})
            continue
        m = re.search(mrx, src, re.M)
        e = re.search(erx, src, re.M)
        rows.append({"what": what, "model": m.group(1) if m else "?",
                     "effort": _eff(e.group(1) if e else ""), "state": ""})
    return rows


def smoke(pairs: set[tuple[str, str]]) -> None:
    print("\nlive check (claude -p 'Reply OK'):")
    for model, effort in sorted(pairs):
        cmd = ["claude", "-p", "--model", model, "Reply OK"]
        if effort in EFFORTS:
            cmd[4:4] = ["--effort", effort]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd="/tmp")
            ok = p.returncode == 0 and "ok" in p.stdout.lower()
            note = p.stdout.strip()[:30] if ok else (p.stderr or p.stdout).strip()[-120:]
        except Exception as ex:
            ok, note = False, str(ex)
        print(f"  {'OK ' if ok else 'FAIL'} {model:18} {effort or '-':8} {time.time() - t0:5.1f}s  {note}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="claude-opus-5-5")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()

    rows = telegram() + matrix() + scripts()
    off = 0
    w = max(len(r["what"]) for r in rows)
    for r in rows:
        good = r["model"] == a.target and r["effort"] == a.effort
        off += not good
        state = f"  [{r['state']}]" if r["state"] else ""
        print(f"{'ok' if good else '!!'}  {r['what']:{w}}  {r['model']:18} {r['effort']}{state}")
    print(f"\ntarget {a.target} / {a.effort}: {len(rows) - off} of {len(rows)} on target"
          + (f", {off} off" if off else "") + f"; global effortLevel = {GLOBAL_EFFORT or '?'}")
    if a.smoke:
        smoke({(r["model"], r["effort"]) for r in rows if r["model"].startswith("claude-")})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
