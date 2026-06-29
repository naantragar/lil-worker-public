#!/usr/bin/env python3
"""CODEX: persistent local shell runtime for lil_worker.

Provides a small Unix-socket JSON API with named bash sessions so agent
commands can reuse shell state across calls.

This is a Codex-oriented runtime layer added to reduce one-shot exec
limitations. It is not part of the original Claude flow.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


SOCKET_PATH = Path(__file__).parent / ".runtime.sock"
PID_PATH = Path(__file__).parent / "runtime.pid"
DEFAULT_SHELL = "/bin/bash"


@dataclass
class ShellSession:
    name: str
    proc: asyncio.subprocess.Process
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def exec(self, command: str, timeout_s: int) -> dict:
        self.last_used_at = time.time()
        marker = f"__LIL_RUNTIME_EXIT__{uuid.uuid4().hex}"
        wrapped = (
            f"{command}\n"
            "runtime_rc=$?\n"
            f"printf '{marker}:%s\\n' \"$runtime_rc\"\n"
        )

        async with self.lock:
            if self.proc.returncode is not None:
                raise RuntimeError(f"session {self.name!r} is not running")

            self.proc.stdin.write(wrapped.encode())
            await self.proc.stdin.drain()

            lines: list[str] = []

            async def _read_until_marker() -> int:
                while True:
                    line = await self.proc.stdout.readline()
                    if not line:
                        raise RuntimeError("shell session closed unexpectedly")
                    text = line.decode(errors="replace")
                    stripped = text.rstrip("\n")
                    if stripped.startswith(marker + ":"):
                        return int(stripped.split(":", 1)[1])
                    lines.append(text)

            exit_code = await asyncio.wait_for(_read_until_marker(), timeout=timeout_s)
            self.last_used_at = time.time()
            return {
                "session": self.name,
                "exit_code": exit_code,
                "output": "".join(lines),
            }

    async def terminate(self):
        if self.proc.returncode is not None:
            return
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()


class RuntimeServer:
    def __init__(self, shell: str):
        self.shell = shell
        self.sessions: dict[str, ShellSession] = {}
        self.server: asyncio.AbstractServer | None = None

    async def get_or_create_session(self, name: str) -> ShellSession:
        session = self.sessions.get(name)
        if session and session.proc.returncode is None:
            return session

        proc = await asyncio.create_subprocess_exec(
            self.shell,
            "--noprofile",
            "--norc",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={
                **os.environ,
                "PS1": "",
                "TERM": "dumb",
            },
        )
        session = ShellSession(name=name, proc=proc)
        self.sessions[name] = session
        return session

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        try:
            raw = await reader.readline()
            if not raw:
                return
            req = json.loads(raw.decode())
            action = req.get("action")

            if action == "health":
                resp = {
                    "status": "ok",
                    "socket": str(SOCKET_PATH),
                    "sessions": len(
                        [s for s in self.sessions.values() if s.proc.returncode is None]
                    ),
                }
            elif action == "sessions":
                resp = {
                    "sessions": [
                        {
                            "name": s.name,
                            "running": s.proc.returncode is None,
                            "created_at": s.created_at,
                            "last_used_at": s.last_used_at,
                        }
                        for s in self.sessions.values()
                    ]
                }
            elif action == "exec":
                session_name = req.get("session") or "default"
                command = req.get("command") or ""
                timeout_s = int(req.get("timeout_s") or 60)
                session = await self.get_or_create_session(session_name)
                resp = await session.exec(command, timeout_s)
            else:
                resp = {"error": f"unknown action: {action}"}
        except Exception as exc:
            resp = {"error": str(exc)}
        finally:
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    async def start(self):
        SOCKET_PATH.unlink(missing_ok=True)
        PID_PATH.write_text(str(os.getpid()))
        self.server = await asyncio.start_unix_server(
            self.handle_client, path=str(SOCKET_PATH)
        )

    async def shutdown(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for session in self.sessions.values():
            await session.terminate()
        SOCKET_PATH.unlink(missing_ok=True)
        PID_PATH.unlink(missing_ok=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shell", default=DEFAULT_SHELL)
    args = parser.parse_args()

    runtime = RuntimeServer(shell=args.shell)
    await runtime.start()

    stop_event = asyncio.Event()

    def _stop(*_args):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    await stop_event.wait()
    await runtime.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
