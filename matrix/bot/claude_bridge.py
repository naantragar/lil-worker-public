"""Drive `claude -p` per message — MIRRORS lil_worker/bot/bot.py's bridging pattern, does NOT import
it. Runs in the lil_worker project cwd so it IS krevetka (same CLAUDE.md, knowledge, memory, tools).

v1: run to completion, parse the final stream-json result for text + session_id. Per-room session is
resumed so the conversation has continuity, exactly like bot.py resumes per-user sessions."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mblog import clip as _clip, log as _log, since as _since  # noqa: E402

# Repo root — same derivation as matrix_bridge: CLAUDE_CWD when set, else two levels up from here.
_REPO = Path(os.environ.get("CLAUDE_CWD") or Path(__file__).resolve().parents[2])

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


def _room_prompt(room_id: str | None) -> str:
    """Extra system prompt for THIS room, read fresh on every turn.

    Rooms are otherwise identical — same repo, same CLAUDE.md, same tools — and differ only by
    session. A room can now also carry its own behaviour: bot/room_profiles.json maps a room id to a
    markdown file under bot/prompts/, whose text is appended to SYSTEM_PROMPT.

    Read on EVERY turn on purpose: tuning such a room means editing that markdown and sending the
    next message, with no bridge restart and no turn lost. Any error here is swallowed — a broken
    profile must degrade to the plain krevetka, never to a dead room.
    """
    if not room_id:
        return ""
    try:
        here = Path(__file__).resolve().parent
        profiles = json.loads((here / "room_profiles.json").read_text())
        entry = profiles.get(room_id) or {}
        rel = entry.get("prompt")
        if not rel:
            return ""
        # One file or an ordered list of them. A tuned room grows a corpus (task, glossary,
        # examples, accepted pairs) and keeping those in separate files is what makes it editable —
        # a single blob would have to be rewritten wholesale on every correction.
        parts = [rel] if isinstance(rel, str) else list(rel)
        chunks = []
        for part in parts:
            try:
                chunks.append((here / part).read_text().strip())
            except Exception:
                continue
        return "\n\n".join(c for c in chunks if c)
    except Exception:
        return ""


# Every dash-like codepoint the model may reach for, mapped to a plain ASCII hyphen. Cheaper and
# far more reliable than asking for it in the prompt — a formatting habit this deep survives any
# number of rules, and the log it feeds wants one dash character, not five.
_DASHES = {
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",  # figure dash
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u2015": "-",  # horizontal bar
    "\u2212": "-",  # minus sign
    "\u2043": "-",  # hyphen bullet
    "\ufe58": "-",  # small em dash
    "\ufe63": "-",  # small hyphen-minus
    "\uff0d": "-",  # fullwidth hyphen-minus
}
_DASH_RE = re.compile("[" + "".join(_DASHES) + "]")

_TIME_IN = re.compile(r"\b\d{1,2}:\d{2}\b|\b\d{2}\.\d{2}\.\d{4}\b")
# Also catches PLACEHOLDER stamps — the model sometimes writes "12:XX" or "--:--" when it knows it
# has no time but still wants the slot filled. Those are just as wrong in a log as an invented one.
_D = r"[\dXxХх?\-]"
_DATE = rf"{_D}{{2}}\.{_D}{{2}}\.{_D}{{4}}"
_TIME = rf"{_D}{{1,2}}:{_D}{{2}}(?::{_D}{{2}})?"
# A date ALONE also counts — the model hedges with things like "12.07.2026 (?)," when it has no
# stamp but still wants the slot filled. Optional "(?)"/"?" hedge is eaten with it.
_TIME_LEAD = re.compile(
    rf"(?m)^[ \t]*(?:{_DATE}(?:\s*\(\?\)|\s*\?)?\s*,?\s*(?:{_TIME})?|{_TIME})\s*")


# Two leftovers the model keeps producing once a timestamp is not available: a hedging clause about
# the missing time ("ім час не вказано - …"), and a bullet dash opening the only line. Both are
# noise in a log line, and both resist prompting — the mere mention of timestamps in the rules is
# what makes it talk about them, so this is handled here instead.
_TIME_META = re.compile(r"(?mi)^\s*(?:ім\s+|йм\s+)?(?:час[ауи]?|дат[аи]|мітк\w*|позначк\w*)\b[^-\n]{0,40}-\s*")
_BULLET = re.compile(r"(?m)^[ \t]*[-•*]\s+")


def _room_postprocess(room_id: str | None, prompt: str, reply: str) -> str:
    """Profile-driven cleanup of the model's reply. Deterministic, because some habits cannot be
    prompted away.

    `strip_timestamp_if_absent`: the refraz room's log lines normally start with the timestamp of
    the intercept, and the example corpus is full of them — so when a fragment arrives WITHOUT any
    time, the model still reaches for one and copies a plausible-looking time out of the corpus.
    Five prompt revisions did not stop it; a fabricated timestamp in an intercept log is worse than
    an ugly one, so it is cut here: no time anywhere in the input → no time at the start of a line.
    """
    if not room_id or not reply:
        return reply
    try:
        here = Path(__file__).resolve().parent
        entry = (json.loads((here / "room_profiles.json").read_text()).get(room_id)) or {}
        out = reply
        if entry.get("strip_dialogue_echo"):
            # A summary must never contain the intercepted speech itself. The model occasionally
            # opens by echoing the first "–" line before summarising; drop any such line outright.
            kept = [ln for ln in out.splitlines() if not ln.lstrip().startswith(("–", "—", "- –"))]
            joined = "\n".join(kept).strip()
            if joined and joined != out.strip():
                _log("claude: dropped echoed dialogue line(s) from the summary")
                out = joined
        if entry.get("strip_timestamp_if_absent") and not _TIME_IN.search(prompt):
            cleaned = _TIME_LEAD.sub("", out).strip()
            if cleaned and cleaned != out.strip():
                _log("claude: stripped an invented timestamp (none in the input)")
            out = cleaned or out
            meta_free = _TIME_META.sub("", out).strip()
            if meta_free and meta_free != out.strip():
                _log("claude: dropped a hedge about the missing timestamp")
                out = meta_free
        if entry.get("normalize_dashes"):
            # Deliberately last: strip_dialogue_echo matches lines starting with "–", so flattening
            # dashes before it would hide exactly the lines it is meant to drop.
            flat = _DASH_RE.sub(lambda m: _DASHES[m.group()], out)
            if flat != out:
                _log("claude: normalised dashes to plain '-'")
                out = flat
            debulleted = _BULLET.sub("", out).strip()
            if debulleted and debulleted != out.strip():
                _log("claude: dropped a leading bullet dash")
                out = debulleted
        return out
    except Exception:
        pass
    return reply


def room_cfg(room_id: str | None) -> dict:
    """This room's profile entry, or {}. Never raises — a broken file must not take a room down."""
    if not room_id:
        return {}
    try:
        here = Path(__file__).resolve().parent
        return (json.loads((here / "room_profiles.json").read_text()).get(room_id)) or {}
    except Exception:
        return {}


def _model(room_id: str | None = None) -> str:
    """Effective model for this room.

    A room may pin its own: a narrow formatting room does not need the flagship the main room runs
    on, and the choice must NOT leak — bot/model_config.json is shared by every room AND by the
    Telegram door, so switching it there would move everything at once. The per-room value wins and
    affects nothing else.
    """
    pinned = str(room_cfg(room_id).get("model") or "").strip()
    if pinned:
        return pinned
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
        "--model", _model(room_id),
        "--allowedTools", ALLOWED_TOOLS,
    ]
    _effort = str(room_cfg(room_id).get("effort") or "").strip()
    if _effort:
        cmd += ["--effort", _effort]
    _extra = _room_prompt(room_id)
    if _extra:
        _log(f"claude: room profile applied (+{len(_extra)} chars of system prompt)")
    cmd += ["--append-system-prompt", SYSTEM_PROMPT + ("\n\n" + _extra if _extra else "")]
    # Force every swarm through the durable-job path. An inline Workflow dies when this turn ends and
    # its report is lost (2026-08-02: five agents finished, nothing was ever reported). The hook
    # converts the call instead of relying on the model to remember the rule; it fails open.
    _hook = _REPO / "tools" / "hooks" / "durable_swarm.py"
    if _hook.exists():
        cmd += ["--settings", json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Workflow",
             "hooks": [{"type": "command", "command": f"python3 {_hook}"}]}]}})]
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
    _log(f"claude: pid {proc.pid}, model {_model(room_id)}"
         + (f" effort={_effort}" if _effort else "") + ","
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
    return _room_postprocess(room_id, prompt, text or "(пустой ответ)"), new_sid, bool(text)


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
