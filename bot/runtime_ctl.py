#!/usr/bin/env python3
"""CODEX: client for lil_worker persistent runtime daemon.

This helper exists for the Codex-oriented runtime layer and is intentionally
separate from the base Claude path.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path


SOCKET_PATH = Path(__file__).parent / ".runtime.sock"


def request(payload: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(SOCKET_PATH))
        sock.sendall((json.dumps(payload) + "\n").encode())
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    raw = b"".join(chunks).decode()
    return json.loads(raw.strip() or "{}")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health")
    sub.add_parser("sessions")

    exec_p = sub.add_parser("exec")
    exec_p.add_argument("--session", default="default")
    exec_p.add_argument("--timeout", type=int, default=60)
    exec_p.add_argument("command")

    args = parser.parse_args()

    if args.cmd == "health":
        resp = request({"action": "health"})
        print(json.dumps(resp, indent=2))
        return

    if args.cmd == "sessions":
        resp = request({"action": "sessions"})
        print(json.dumps(resp, indent=2))
        return

    if args.cmd == "exec":
        resp = request(
            {
                "action": "exec",
                "session": args.session,
                "timeout_s": args.timeout,
                "command": args.command,
            }
        )
        if "error" in resp:
            print(resp["error"], file=sys.stderr)
            raise SystemExit(1)
        sys.stdout.write(resp.get("output", ""))
        raise SystemExit(int(resp.get("exit_code", 0)))


if __name__ == "__main__":
    main()
