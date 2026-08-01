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
import time

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

    def _ingest(raw: bytes) -> None:
        nonlocal text, new_sid
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            return
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            return
        if evt.get("type") == "result":
            text = evt.get("result", "") or text
            new_sid = evt.get("session_id", new_sid)

    # Read WITHOUT the StreamReader 64KB line limit: `async for line in stdout` (readline) raises
    # LimitOverrunError ("Separator is found, but chunk is longer than limit") on a long stream-json
    # line - e.g. a big tool result. Accumulate raw chunks and split on \n ourselves. (Same fix the
    # Telegram bot already carries in krevetka.py.)
    # A turn MUST have a deadline. Without one, a hung `claude -p` holds its room's lock forever:
    # the room stops answering entirely and the "typing…" indicator never goes out, with no way to
    # recover except restarting the bridge. Silence-based (not wall-clock), so a legitimately long
    # turn that keeps streaming is never cut off, with a hard ceiling as the final backstop.
    buf = b""
    started = time.monotonic()
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
        try:
            await asyncio.wait_for(stderr_task, timeout=5)
        except (asyncio.TimeoutError, Exception):
            stderr_task.cancel()
        return b"".join(stderr_buf).decode("utf-8", "replace")

    if timed_out:
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
