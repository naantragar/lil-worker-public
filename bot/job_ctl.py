#!/usr/bin/env python3
"""job_ctl.py — launch/list durable background jobs for the wake-up feature (v0).

A "job" is a long task that runs DETACHED (own session via start_new_session) so it outlives
the one-shot `claude -p` turn that launched it. It writes its state under jobs/<id>/; bot.py's
poller notices a terminal status and messages the owner in Telegram (that's krevetka waking up).

  python3 job_ctl.py launch --cmd '<shell>' [--label L] [--cwd DIR] [--owner UID] [--force]
  python3 job_ctl.py list

Gates: one active job at a time (unless --force); owner defaults to the first ALLOWED_USERS.
Never prints secrets. The command is stored base64 in spec.json (quoting-safe).
"""
import argparse
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent
JOBS_DIR = BOT_DIR / "jobs"
RUN_JOB = JOBS_DIR / "run_job.sh"
ENV_FILE = BOT_DIR / ".env"

# `cancelled` is terminal too: a deliberate stop must still be delivered (as a one-line notice, not a
# full wake-report) and must be prunable. It used to be in neither set, so a cancelled job was
# invisible to both pollers and never cleaned up.
TERMINAL = {"done", "failed", "cancelled"}
ACTIVE = {"queued", "running"}
# A job that never got past `queued` (its launcher itself failed) has no pid to check; give it this
# long before the reaper is allowed to call it dead.
QUEUED_GRACE_SEC = 180
# Ceiling across ALL origins, so parallel rooms can't fork-bomb the box with swarms.
MAX_GLOBAL_ACTIVE = 3


def _env(key: str, default: str = "") -> str:
    """Read one KEY=value from bot/.env without sourcing it."""
    try:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1]
    except OSError:
        pass
    return default


def _default_owner() -> int | None:
    raw = _env("ALLOWED_USERS", "")
    for tok in raw.split(","):
        tok = tok.strip()
        if tok.isdigit():
            return int(tok)
    return None


def _read_status(job_dir: Path) -> str:
    try:
        return (job_dir / "status").read_text().strip()
    except OSError:
        return "unknown"


def _job_dirs() -> list[Path]:
    if not JOBS_DIR.is_dir():
        return []
    return sorted([p for p in JOBS_DIR.iterdir() if p.is_dir() and (p / "spec.json").exists()])


def _read_spec(job_dir: Path) -> dict:
    try:
        return json.loads((job_dir / "spec.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _read_pid(job_dir: Path) -> int | None:
    try:
        return int((job_dir / "pid").read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def reap_job(job_dir: Path) -> bool:
    """Heal ONE job whose runner died out-of-band. Returns True if it was healed.

    A runner can be killed without ever recording a terminal status: a systemd cgroup kill (the
    2026-07-30 incident — restarting matrix-bridge-bot wiped every job launched from Matrix), an OOM
    kill, or a manual `kill -9`. `run_job.sh` never reaches its bookkeeping lines, so `status` stays
    `running` FOREVER: the result is never delivered and the launch gate stays blocked silently.
    Cheap liveness check: the pid recorded by the runner no longer exists => it is dead."""
    status = _read_status(job_dir)
    if status not in ACTIVE:
        return False
    pid = _read_pid(job_dir)
    if pid is None:
        # `queued` with no pid yet is the normal startup window — only call it dead once the launcher
        # has had ample time (a Popen/systemd-run failure would otherwise wedge the gate forever).
        try:
            age = time.time() - (job_dir / "spec.json").stat().st_mtime
        except OSError:
            return False
        if age < QUEUED_GRACE_SEC:
            return False
        note = "runner never started (launcher failed before recording a pid)"
    else:
        if _pid_alive(pid):
            return False
        note = f"runner pid {pid} died without recording a terminal status (killed out-of-band)"
    try:
        with (job_dir / "result.txt").open("a") as f:
            f.write(f"\n--- РЕАПЕР {time.strftime('%Y-%m-%dT%H:%M:%S%z')} ---\n"
                    f"{note}. Задача помечена failed автоматически, чтобы доклад дошёл и гейт "
                    f"запуска не остался заблокированным. Частичные результаты роя (если он был) "
                    f"ищи в journal.jsonl: python3 tools/workflow_job.py harvest {job_dir.name}\n")
    except OSError:
        pass
    if not (job_dir / "exit_code").exists():
        (job_dir / "exit_code").write_text("143")
    if not (job_dir / "duration_sec").exists():
        (job_dir / "duration_sec").write_text("0")
    (job_dir / "status").write_text("failed")
    return True


def reap_all() -> list[str]:
    """Reap every stuck job. Safe to call from any poller tick — it only touches dead runners."""
    return [p.name for p in _job_dirs() if reap_job(p)]


def origin_key(spec: dict) -> str:
    """Which conversation a job reports back to. The launch gate is scoped by THIS, not global:
    rooms now run in parallel, so a swarm from one room must not block a swarm from another."""
    rt = spec.get("reply_to") or {}
    if rt.get("door") == "matrix":
        return f"matrix:{rt.get('room_id', '')}"
    return f"telegram:{rt.get('uid', spec.get('owner_uid', ''))}"


def _active_jobs() -> list[Path]:
    reap_all()  # never let a dead runner masquerade as active
    return [p for p in _job_dirs() if _read_status(p) in ACTIVE]


def cmd_launch(args: argparse.Namespace) -> None:
    if not RUN_JOB.exists():
        sys.exit(f"runner missing: {RUN_JOB}")

    owner = args.owner if args.owner is not None else _default_owner()
    if owner is None:
        sys.exit("no owner: pass --owner UID or set ALLOWED_USERS in bot/.env")

    # The gate is per ORIGIN (this room / this Telegram user), plus a global ceiling. A global
    # one-at-a-time gate used to make a swarm in krevetka-wiki block a swarm in krevetka-ops.
    _door = os.environ.get("KREVETKA_DOOR", "telegram")
    if _door == "matrix":
        reply_to = {"door": "matrix", "room_id": os.environ.get("KREVETKA_ROOM", "")}
    else:
        reply_to = {"door": "telegram", "uid": owner}
    my_origin = origin_key({"reply_to": reply_to, "owner_uid": owner})

    if not args.force:
        active = _active_jobs()
        same = [p for p in active if origin_key(_read_spec(p)) == my_origin]
        if same:
            sys.exit(f"an active job already exists for this origin: {same[0].name} "
                     f"(status={_read_status(same[0])}). Use --force to run anyway.")
        if len(active) >= MAX_GLOBAL_ACTIVE:
            sys.exit(f"{len(active)} jobs already active (global ceiling {MAX_GLOBAL_ACTIVE}): "
                     f"{', '.join(p.name for p in active)}. Use --force to run anyway.")

    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    # Default to the project root (parent of bot/), NOT ~/lil_worker — under root that resolves
    # to the non-existent /root/lil_worker. BOT_DIR.parent is always the real repo dir.
    cwd = args.cwd or str(BOT_DIR.parent)
    # `reply_to` (computed above, before the gate) is the origin door, so the result reports BACK to
    # where the job was launched. The launching claude -p turn's env carries it: Matrix door sets
    # KREVETKA_DOOR=matrix + KREVETKA_ROOM; Telegram leaves it unset → default telegram + owner_uid.
    # Delivery is partitioned by door: the Telegram poller (krevetka.py) skips matrix jobs; the Matrix
    # bridge's poller delivers them.
    spec = {
        "id": job_id,
        "label": args.label or "job",
        "owner_uid": owner,
        "reply_to": reply_to,
        "cwd": cwd,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cmd_b64": base64.b64encode(args.cmd.encode()).decode(),
        # wake=True → on completion the bot wakes an isolated claude turn to REPORT the result in
        # its own voice (v1); wake=False → plain raw-output notification (v0).
        "wake": bool(args.wake),
    }
    # spec.json + status MUST exist before the runner starts — run_job.sh reads the command out of
    # spec.json as its first action.
    def _write_spec() -> None:
        tmp = job_dir / "spec.json.tmp"
        tmp.write_text(json.dumps(spec, ensure_ascii=False, indent=2))
        os.replace(tmp, job_dir / "spec.json")

    # Detached TWICE over, because setsid alone is not enough:
    #  - start_new_session=True (setsid) → own session, no controlling tty, reparents to init. This
    #    survives the end of the launching `claude -p` turn.
    #  - systemd-run --scope → the runner also leaves the CGROUP of whatever unit launched it. Without
    #    this, a Matrix-launched job lives inside matrix-bridge-bot.service's cgroup, and that unit's
    #    KillMode=control-group means `systemctl restart matrix-bridge-bot` kills the job outright.
    #    (Real incident 2026-07-30: a live 10-agent swarm was wiped mid-flight this way.) `--scope`
    #    (not a transient service) is deliberate: it keeps the caller's environment, which the inner
    #    `claude` needs (HOME, PATH, credentials).
    runner = ["bash", str(RUN_JOB), str(job_dir)]
    launched_via = "popen"
    if shutil.which("systemd-run"):
        runner = ["systemd-run", "--scope", "--collect", "--quiet",
                  f"--unit=krevetka-job-{job_id}"] + runner
        launched_via = "systemd-run-scope"
    spec["launched_via"] = launched_via
    spec["scope_unit"] = f"krevetka-job-{job_id}.scope" if launched_via == "systemd-run-scope" else ""
    _write_spec()
    (job_dir / "status").write_text("queued")
    try:
        subprocess.Popen(
            runner,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(BOT_DIR),
        )
    except OSError as e:
        if launched_via == "popen":
            raise
        # systemd-run missing/broken (no dbus, etc.) — never lose the job over it. The runner never
        # started in this branch, so rewriting the spec here races with nothing.
        spec["launched_via"] = f"popen-fallback ({e})"
        spec["scope_unit"] = ""
        _write_spec()
        subprocess.Popen(
            ["bash", str(RUN_JOB), str(job_dir)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(BOT_DIR),
        )
    print(job_id)


def cmd_cancel(args: argparse.Namespace) -> None:
    """Stop a running job deliberately. Before this existed, cancelling meant killing the runner by
    hand and hand-writing the status — which left `cancelled` invisible to both pollers."""
    matches = [p for p in _job_dirs() if p.name == args.id or p.name.startswith(args.id)]
    if not matches:
        sys.exit(f"no such job: {args.id}")
    if len(matches) > 1:
        sys.exit("ambiguous id, matches: " + ", ".join(p.name for p in matches))
    job_dir = matches[0]
    status = _read_status(job_dir)
    if status not in ACTIVE:
        sys.exit(f"job {job_dir.name} is not active (status={status})")

    spec = _read_spec(job_dir)
    killed = []
    unit = spec.get("scope_unit") or ""
    if unit:  # stop the whole transient scope: the runner AND everything it spawned
        r = subprocess.run(["systemctl", "stop", unit], capture_output=True, text=True)
        killed.append(f"systemctl stop {unit} → rc={r.returncode}")
    pid = _read_pid(job_dir)
    if pid and _pid_alive(pid):
        try:  # runner is its own session leader (setsid), so its pgid covers all its children
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            killed.append(f"killpg {pid} SIGTERM")
        except OSError as e:
            killed.append(f"killpg {pid} failed: {e}")

    reason = args.reason or "cancelled by krevetka"
    try:
        with (job_dir / "result.txt").open("a") as f:
            f.write(f"\n--- ОТМЕНА {time.strftime('%Y-%m-%dT%H:%M:%S%z')} ---\n{reason}\n"
                    f"({'; '.join(killed) if killed else 'runner was already gone'})\n")
    except OSError:
        pass
    (job_dir / "exit_code").write_text("143")
    if not (job_dir / "duration_sec").exists():
        (job_dir / "duration_sec").write_text("0")
    (job_dir / "status").write_text("cancelled")
    print(f"cancelled {job_dir.name}: {reason}")


def cmd_reap(_a: argparse.Namespace) -> None:
    healed = reap_all()
    print("\n".join(healed) if healed else "(nothing to reap)")


def cmd_list(args: argparse.Namespace) -> None:
    reap_all()  # show the truth, not a dead runner still claiming to be `running`
    dirs = _job_dirs()
    if not dirs:
        print("(no jobs)")
        return
    for p in dirs:
        spec = _read_spec(p)
        notified = "✓" if (p / "notified").exists() else " "
        print(f"[{notified}] {p.name}  {_read_status(p):9}  {origin_key(spec):26}  {spec.get('label','')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="durable background job control (v0)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("launch")
    p.add_argument("--cmd", required=True, help="shell command/pipeline to run")
    p.add_argument("--label", help="short human label shown in the notification")
    p.add_argument("--cwd", help="working directory for the command")
    p.add_argument("--owner", type=int, help="Telegram user id to notify (default: first ALLOWED_USERS)")
    p.add_argument("--force", action="store_true", help="launch even if another job is active")
    p.add_argument("--wake", action="store_true",
                   help="on completion, wake an isolated claude turn to REPORT the result (v1)")
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("list")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("cancel", help="stop an active job and mark it cancelled (terminal)")
    p.add_argument("id", help="job id (or a unique prefix)")
    p.add_argument("--reason", help="why — goes into the result and the notice")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("reap", help="mark jobs whose runner died as failed (also runs automatically)")
    p.set_defaults(func=cmd_reap)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
