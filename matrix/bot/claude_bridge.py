"""Drive `claude -p` per message — MIRRORS lil_worker/bot/bot.py's bridging pattern, does NOT import
it. Runs in the lil_worker project cwd so it IS krevetka (same CLAUDE.md, knowledge, memory, tools).

v1: run to completion, parse the final stream-json result for text + session_id. Per-room session is
resumed so the conversation has continuity, exactly like bot.py resumes per-user sessions."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mblog import clip as _clip, log as _log, since as _since  # noqa: E402

ALLOWED_TOOLS = "Read,Write,Edit,Bash,Glob,Grep,WebFetch,WebSearch,Task,Agent,Workflow,Skill"

# Turn deadlines (see the read loop). Silence, not wall-clock: a long turn that keeps streaming is
# fine; a turn that says nothing for this long is hung and must not keep its room hostage.
_SILENCE_LIMIT_S = 1800   # 30 min with no output at all
_HARD_LIMIT_S = 10800     # 3 h ceiling regardless of streaming

SYSTEM_PROMPT = (
    "You are krevetka (креветка), the user's local working agent, reached here through a private "
    "Matrix room in Element instead of Telegram. This is the SAME you: same code, knowledge, memory, "
    "tools, and the ability to modify your own code. Reply in the user's language (default Russian). "
    "Markdown is rendered to Matrix HTML, so use it normally. Media works like on Telegram: to send a "
    "file use a line `[FILE /absolute/path]`; to send a voice note use `[VOICE lang=\"ru\"]текст[/VOICE]` "
    "at the END of the reply — ONLY when the user explicitly asks for a voice message. Do not send "
    "files unless asked."
)

_MEDIA = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}


def _image_stdin(prompt: str, images: list[str]) -> bytes:
    """stream-json user message: base64 image blocks + text — the native claude CLI image input."""
    content = []
    for p in images:
        try:
            with open(p, "rb") as f:
                data = base64.b64encode(f.read()).decode()
            ext = p.rsplit(".", 1)[-1].lower() if "." in p else ""
            content.append({"type": "image", "source": {"type": "base64",
                            "media_type": _MEDIA.get(ext, "image/jpeg"), "data": data}})
        except Exception:
            pass
    if prompt:
        content.append({"type": "text", "text": prompt})
    return (json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n").encode()


def _model() -> str:
    try:
        cfg = os.path.join(os.environ.get("CLAUDE_CWD", "."), "bot", "model_config.json")
        return str(json.load(open(cfg)).get("model", "")).strip() or os.environ.get("CLAUDE_MODEL", "sonnet")
    except Exception:
        return os.environ.get("CLAUDE_MODEL", "sonnet")


def neutralize_leading_slash(prompt: str) -> str:
    """`claude -p "/jobs"` is parsed by the CLI as a SLASH COMMAND, not as a prompt: the model is
    never called, the answer is "Unknown command", and commands the CLI *does* know (/clear, /init,
    /model, every skill in this repo) would be EXECUTED in the repo cwd. Unknown slash text falls
    through to here from on_text, so wrap anything that starts with "/" as literal text.
    Mirrors bot/krevetka.py:neutralize_leading_slash — same hole, both doors."""
    if not prompt.lstrip().startswith("/"):
        return prompt
    return ("[user message, verbatim — the leading slash is part of the text, not a command]\n"
            + prompt)


async def run(prompt: str, session_id: str | None, images: list[str] | None = None,
              room_id: str | None = None) -> tuple[str, str | None, bool]:
    """Returns (reply_text, new_session_id, ok). `ok` is False when the turn was DEGRADED — killed on
    a deadline, or finished with no text at all — so the caller can fall back instead of publishing a
    placeholder as if it were the answer. With images, feeds them via stream-json stdin. Raises on
    hard failure. `room_id` (the originating Matrix room) is tagged into the child env so any durable
    job launched during this turn records its origin and reports BACK to this room, not to Telegram."""
    cmd = [
        os.environ.get("CLAUDE_BIN", "claude"), "-p",
        "--output-format", "stream-json", "--verbose",
        "--model", _model(),
        "--allowedTools", ALLOWED_TOOLS,
        "--append-system-prompt", SYSTEM_PROMPT,
    ]
    stdin_bytes = None
    if images:
        cmd += ["--input-format", "stream-json"]
        stdin_bytes = _image_stdin(prompt, images)  # stream-json content is never slash-parsed
    else:
        cmd += [neutralize_leading_slash(prompt)]
    if session_id:
        cmd += ["--resume", session_id]

    child_env = dict(os.environ)
    if room_id:
        child_env["KREVETKA_DOOR"] = "matrix"
        child_env["KREVETKA_ROOM"] = room_id
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=os.environ.get("CLAUDE_CWD", os.getcwd()),
        stdin=asyncio.subprocess.PIPE if stdin_bytes else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env,
        # Own process GROUP. `claude -p` spawns its own children (tool subprocesses, swarm agents);
        # proc.kill() signals exactly one pid, so on a deadline the grandchildren were orphaned and
        # kept burning CPU and tokens with nobody to collect them. With a group leader we can signal
        # the whole tree below (see _kill_tree).
        start_new_session=True,
    )
    # stderr must be drained CONCURRENTLY. It was only read after the process exited, so a child that
    # wrote more than the ~64 KB pipe buffer to stderr blocked on write() forever while we waited for
    # its stdout — a hang that also parks the room lock for good.
    stderr_buf: list[bytes] = []

    async def _drain_stderr() -> None:
        if proc.stderr is None:
            return
        try:
            while True:
                chunk = await proc.stderr.read(65536)
                if not chunk:
                    return
                stderr_buf.append(chunk)
                del stderr_buf[:-16]  # keep only the tail; stderr can be arbitrarily large
        except Exception:
            return

    stderr_task = asyncio.create_task(_drain_stderr())
    if stdin_bytes:
        proc.stdin.write(stdin_bytes)
        await proc.stdin.drain()
        proc.stdin.close()
    text, new_sid = "", None
    assert proc.stdout is not None

    started = time.monotonic()
    # Live picture of what the turn is DOING. Without it a long turn is a black box: the room goes
    # quiet and there is no way to tell "the model is grinding through 40 tool calls" from "it hung".
    # Every tool call is logged as it streams, and a heartbeat reports progress while it runs.
    _log(f"claude: pid {proc.pid}, model {_model()},"
         f" {'resume ' + session_id[:8] if session_id else 'fresh session'}"
         f"{f', {len(images)} image(s)' if images else ''}")
    stats = {"events": 0, "tools": 0, "last_tool": "", "last_event": time.monotonic(),
             "first_out": None, "workflows": 0}

    def _ingest(raw: bytes) -> None:
        nonlocal text, new_sid
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            return
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            return
        stats["events"] += 1
        stats["last_event"] = time.monotonic()
        if stats["first_out"] is None:
            stats["first_out"] = time.monotonic()
            _log(f"claude: first output after {_since(started)}")
        etype = evt.get("type")
        if etype == "assistant":
            for block in ((evt.get("message") or {}).get("content") or []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = block.get("name") or "?"
                    stats["tools"] += 1
                    stats["last_tool"] = name
                    inp = block.get("input") or {}
                    hint = (inp.get("command") or inp.get("file_path") or inp.get("pattern")
                            or inp.get("prompt") or inp.get("description") or "")
                    if name in ("Workflow", "Task", "Agent"):
                        stats["workflows"] += 1
                        # The exact failure seen on 2026-08-02: a swarm launched INSIDE a turn keeps
                        # running only as long as the turn does. Name it in the log so a later
                        # "where is my report?" has an answer.
                        _log(f"claude: tool {name} — a swarm inside the turn; it dies when the turn"
                             f" ends unless it was launched as a durable job", _clip(hint, 60))
                    else:
                        _log(f"claude: tool {name}", _clip(hint, 70))
        elif etype == "result":
            text = evt.get("result", "") or text
            new_sid = evt.get("session_id", new_sid)

    async def _heartbeat() -> None:
        """Say something every minute so a long turn is visibly ALIVE in the log."""
        while True:
            await asyncio.sleep(60)
            quiet = time.monotonic() - stats["last_event"]
            _log(f"claude: still running {_since(started)} — {stats['events']} events,"
                 f" {stats['tools']} tool calls, last '{stats['last_tool'] or '-'}',"
                 f" quiet for {quiet:.0f}s")

    heartbeat = asyncio.create_task(_heartbeat())

    # Read WITHOUT the StreamReader 64KB line limit: `async for line in stdout` (readline) raises
    # LimitOverrunError ("Separator is found, but chunk is longer than limit") on a long stream-json
    # line - e.g. a big tool result. Accumulate raw chunks and split on \n ourselves. (Same fix the
    # Telegram bot already carries in krevetka.py.)
    # A turn MUST have a deadline. Without one, a hung `claude -p` holds its room's lock forever:
    # the room stops answering entirely and the "typing…" indicator never goes out, with no way to
    # recover except restarting the bridge. Silence-based (not wall-clock), so a legitimately long
    # turn that keeps streaming is never cut off, with a hard ceiling as the final backstop.
    buf = b""
    timed_out = ""
    while True:
        try:
            chunk = await asyncio.wait_for(proc.stdout.read(65536), timeout=_SILENCE_LIMIT_S)
        except asyncio.TimeoutError:
            timed_out = f"нет вывода {_SILENCE_LIMIT_S // 60} мин"
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            idx = buf.index(b"\n")
            _ingest(buf[:idx])
            buf = buf[idx + 1:]
        if time.monotonic() - started > _HARD_LIMIT_S:
            timed_out = f"превышен потолок {_HARD_LIMIT_S // 3600} ч"
            break
    if buf.strip():
        _ingest(buf)  # trailing line with no newline at EOF

    async def _finish() -> str:
        """Stop draining and return the tail of stderr. Bounded: a wedged child must not hang us."""
        heartbeat.cancel()
        try:
            await asyncio.wait_for(stderr_task, timeout=5)
        except (asyncio.TimeoutError, Exception):
            stderr_task.cancel()
        return b"".join(stderr_buf).decode("utf-8", "replace")

    if timed_out:
        _log(f"claude: DEADLINE HIT ({timed_out}) after {_since(started)} —"
             f" killing the process group ({stats['tools']} tool calls so far)")
        _kill_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass  # reaped by init; never block the room on a corpse
        await _finish()
        # ok=False: this is a placeholder, not an answer. The caller decides what to show.
        return ((text + f"\n\n⚠️ Ход прерван по таймауту ({timed_out}) — комната освобождена."
                 if text else f"⚠️ Ход прерван по таймауту ({timed_out}), ответа не было."),
                new_sid, False)

    await proc.wait()
    err = await _finish()
    _log(f"claude: exit {proc.returncode} after {_since(started)} —"
         f" {stats['events']} events, {stats['tools']} tool calls, reply {len(text)} chars"
         + (f", {stats['workflows']} in-turn swarm launch(es)" if stats["workflows"] else "")
         + ("" if text else " — NO TEXT IN RESULT"))
    if err.strip():
        _log("claude: stderr tail", _clip(err[-300:], 200))
    if proc.returncode != 0 and not text:
        raise RuntimeError(f"claude -p exited {proc.returncode}: {err[-400:]}")
    return (text or "(пустой ответ)"), new_sid, bool(text)


def _kill_tree(proc) -> None:
    """SIGKILL the child's whole process group (it is a group leader — start_new_session=True), so
    the swarm agents and tool subprocesses it spawned die with it instead of being orphaned."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass
