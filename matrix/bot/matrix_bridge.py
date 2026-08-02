#!/usr/bin/env python3
"""matrix-bridge — private single-owner Matrix bot; the Matrix door to krevetka (Path A, unencrypted).

Separate service from lil_worker/bot/bot.py — shares nothing, imports nothing. Full media parity with
the Telegram bot (same OpenAI transcription/TTS engines, same [FILE]/[VOICE] markers).

Flow: sync_forever -> the owner's message in the one allowed room ->
  - text            -> claude
  - image (m.image) -> download -> claude (as image input)
  - voice (m.audio) -> download -> OpenAI transcribe -> claude
  - file  (m.file)  -> download -> .inbox/<ts>_<name> -> claude (path handed over, never executed)
then post the reply as Markdown->HTML, plus any [FILE ...]/[VOICE ...] the reply emitted.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from nio import (AsyncClient, BadEvent, MatrixRoom, RoomEncryptedAudio, RoomEncryptedFile,
                 RoomEncryptedImage, RoomEncryptedVideo, RoomMessageAudio, RoomMessageFile,
                 RoomMessageImage, RoomMessageText, RoomMessageVideo, RoomSendError)

HERE = Path(__file__).resolve().parent
load_dotenv(HERE.parent / ".env")
sys.path.insert(0, str(HERE))
import claude_bridge  # noqa: E402
import media  # noqa: E402
import render  # noqa: E402
from mblog import clip as _clip, log as _log, since as _since  # noqa: E402

HOMESERVER = os.environ["HOMESERVER_URL"]
BOT_MXID = os.environ["BOT_MXID"]
TOKEN = os.environ["BOT_ACCESS_TOKEN"]
OWNER = os.environ["OWNER_MXID"]
# One or more rooms (comma-separated in ALLOWED_ROOM_ID). Each room is an INDEPENDENT conversation
# with its own claude session (sessions.json is keyed by room id) — e.g. a main/dev room and a
# separate wiki/search room. Same bot, same rights; only the session (context) differs per room.
ROOM_ORDER = [r.strip() for r in os.environ["ALLOWED_ROOM_ID"].split(",") if r.strip()]
ROOMS = set(ROOM_ORDER)
MAIN_ROOM = ROOM_ORDER[0]  # fallback target so a finished job's result is never dropped silently
SESSIONS_FILE = HERE / "sessions.json"
# Repo root: CLAUDE_CWD when set, else derived from this file's location (matrix/bot/ → two up),
# so a fresh install works before anyone edits .env.
REPO_ROOT = Path(os.environ.get("CLAUDE_CWD") or Path(__file__).resolve().parents[2])
INBOX = REPO_ROOT / ".inbox"
TMP = Path("/tmp")

_started = time.time() * 1000
_client: AsyncClient | None = None
# Per-room reset generation. A turn captures the generation it started under; /new bumps it. Only a
# turn whose generation is still current may write its session id back.
#
# This replaces an earlier one-shot "_reset_pending" flag that was WRONG when /new was issued on an
# IDLE room: nothing consumed the flag, so the NEXT turn's session id was silently discarded and the
# room forgot the first thing said after the reset. A counter has no such ambiguity — it does not
# care whether a turn was in flight.
_session_gen: dict[str, int] = {}


def _gen(room_id: str) -> int:
    return _session_gen.get(room_id, 0)


def _sessions() -> dict:
    try:
        return json.loads(SESSIONS_FILE.read_text())
    except Exception:
        return {}


def _save_session(room_id: str, sid: str | None, gen: int | None = None) -> None:
    # A /new that happened DURING this turn invalidates it: writing the id back would silently
    # resurrect the context the user just dropped.
    if gen is not None and gen != _gen(room_id):
        _log("session write dropped — /new happened during this turn", room_id)
        return
    if sid:
        s = _sessions(); s[room_id] = sid; SESSIONS_FILE.write_text(json.dumps(s))


def _clear_session(room_id: str) -> bool:
    """Drop this room's claude session. Returns True if there was one."""
    s = _sessions()
    had = s.pop(room_id, None)
    SESSIONS_FILE.write_text(json.dumps(s))
    _session_gen[room_id] = _gen(room_id) + 1
    return bool(had)


def _safe_name(raw: str) -> str:
    base = Path(raw or "file").name
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".") or "file"
    return base[:80]


async def _download(url: str) -> bytes | None:
    resp = await _client.download(mxc=url)
    return getattr(resp, "body", None)


async def _download_event_media(event) -> bytes | None:
    """Fetch an attachment from ANY media event, plaintext or encrypted.

    Our rooms are unencrypted, so media normally arrives as `content.url` (plaintext mxc). But a
    message FORWARDED out of an encrypted room keeps the original encrypted-attachment descriptor:
    `content.file` (mxc + JWK key/iv/hashes) instead of `content.url`. matrix-nio then parses it as
    RoomEncrypted{Audio,Image,File,Video} — classes that are NOT subclasses of RoomMessage* — so a
    forwarded voice note silently reached no handler at all and was dropped without a word. Here we
    fetch the ciphertext and decrypt it locally with the key that travelled inside the event."""
    data = await _download(event.url)
    if data is None:
        return None
    key = getattr(event, "key", None)
    if not key:
        return data  # plaintext attachment
    try:
        from nio.crypto.attachments import decrypt_attachment
        return decrypt_attachment(data, key["k"], event.hashes["sha256"], event.iv)
    except Exception as e:
        _log("attachment decrypt failed", repr(e))
        return None


async def _upload(data: bytes, content_type: str, filename: str) -> str | None:
    resp, _ = await _client.upload(
        lambda *_: data, content_type=content_type, filename=filename, filesize=len(data)
    )
    return getattr(resp, "content_uri", None)


# A Matrix event body over ~64 KiB is rejected with M_TOO_LARGE. Unlike the Telegram door (which has
# send_long_message) nothing here ever split a long answer, so a big report was refused wholesale.
_MAX_BODY = 32000


def _split_body(text: str) -> list[str]:
    """Chunk on line boundaries, never mid-line unless a single line is itself oversized."""
    if len(text) <= _MAX_BODY:
        return [text]
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > _MAX_BODY:            # pathological single line
            if cur:
                out.append(cur); cur = ""
            out.append(line[:_MAX_BODY]); line = line[_MAX_BODY:]
        if len(cur) + len(line) > _MAX_BODY:
            out.append(cur); cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out


async def _send_text(room_id: str, text: str) -> None:
    """Post text, and RAISE if the server refused it.

    nio does not throw on a rejected send — it RETURNS a RoomSendError (M_FORBIDDEN, M_TOO_LARGE for
    a body over 64 KiB, or a 502 from nginx while continuwuity restarts). Ignoring that return made
    every caller believe the message had landed; for a durable-job report that meant the two-phase
    marker was finalised on a send that never happened, and an expensive swarm's result was lost in
    silence. Raising lets _deliver_and_mark leave the marker at "delivering:" so it is retried."""
    for chunk in _split_body(text):
        resp = await _client.room_send(room_id, "m.room.message", {
            "msgtype": "m.text", "body": chunk,
            "format": "org.matrix.custom.html", "formatted_body": render.to_html(chunk),
        })
        if isinstance(resp, RoomSendError):
            raise RuntimeError(f"room_send refused for {room_id}: {getattr(resp, 'message', resp)}")


async def _send_file(room_id: str, path: str) -> None:
    p = Path(path)
    if not p.is_file():
        await _send_text(room_id, f"⚠️ файл не найден: {path}")
        return
    data = p.read_bytes()
    mxc = await _upload(data, "application/octet-stream", p.name)
    if mxc:
        await _client.room_send(room_id, "m.room.message", {
            "msgtype": "m.file", "body": p.name, "url": mxc,
            "info": {"mimetype": "application/octet-stream", "size": len(data)},
        })


async def _send_voice(room_id: str, ogg: Path) -> None:
    data = ogg.read_bytes()
    mxc = await _upload(data, "audio/ogg", ogg.name)
    if not mxc:
        return
    dur = media.audio_duration_ms(str(ogg))
    await _client.room_send(room_id, "m.room.message", {
        "msgtype": "m.audio", "body": "voice message", "url": mxc,
        "info": {"mimetype": "audio/ogg", "size": len(data), "duration": dur},
        "org.matrix.msc1767.audio": {"duration": dur, "waveform": [256] * 30},
        "org.matrix.msc3245.voice": {},
    })


# "typing…" upkeep. A single room_typing(timeout=10min) is NOT enough: the homeserver (and the
# client) cap the typing TTL to tens of seconds, so on a long turn the indicator lapsed while the
# turn was still running and the owner saw nothing happening. So re-assert it on a short cycle for
# the WHOLE turn — until the reply has actually been posted.
_TYPING_TTL_MS = 30000   # what we claim per ping
_TYPING_REFRESH_S = 20   # re-ping before that lapses


async def _typing_keepalive(room_id: str) -> None:
    """Re-assert "typing…" until cancelled. A transient failure must never kill the turn."""
    while True:
        try:
            # Bounded: nio's default request timeout is 60 s, so ONE stalled PUT used to blank the
            # indicator for a whole minute — longer than the TTL it was refreshing.
            await asyncio.wait_for(
                _client.room_typing(room_id, True, timeout=_TYPING_TTL_MS), timeout=10)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(_TYPING_REFRESH_S)


# Refcounted per room, because more than one thing legitimately wants the indicator up at once:
# the turn wrapper holds it across the queue wait, and _answer holds it across the model run. The
# indicator goes out only when the LAST holder releases it — otherwise an inner release would blank
# it while the outer stage was still working.
_typing_tasks: dict[str, asyncio.Task] = {}
_typing_holders: dict[str, int] = {}


def _typing_on(room_id: str) -> None:
    _typing_holders[room_id] = _typing_holders.get(room_id, 0) + 1
    if room_id not in _typing_tasks:
        _typing_tasks[room_id] = asyncio.create_task(_typing_keepalive(room_id))


async def _typing_off(room_id: str) -> None:
    left = _typing_holders.get(room_id, 1) - 1
    _typing_holders[room_id] = max(0, left)
    if left > 0:
        return
    task = _typing_tasks.pop(room_id, None)
    if task:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    try:
        await asyncio.wait_for(_client.room_typing(room_id, False), timeout=10)
    except Exception:
        pass


async def _answer(room_id: str, prompt: str, images: list[str] | None = None) -> None:
    """Run claude for THIS room's session, then post text + any [FILE]/[VOICE] the reply emitted."""
    # Keepalive spans the claude run AND the sends (TTS synthesis + uploads take seconds too), so the
    # indicator stays up until the message really lands in the room.
    _typing_on(room_id)
    gen = _gen(room_id)  # captured BEFORE the run; a /new during it invalidates the session write
    sid = _sessions().get(room_id)
    _log(f"answer: prompt {len(prompt)} chars,"
         f" {'resuming ' + sid[:8] if sid else 'FRESH session'}"
         f"{f', {len(images)} image(s)' if images else ''}", _clip(prompt, 60))
    t_run = time.monotonic()
    try:
        try:
            reply, new_sid, _ok = await claude_bridge.run(
                prompt, sid, images=images, room_id=room_id)
            _log(f"answer: model finished in {_since(t_run)} → {len(reply)} chars"
                 f"{'' if _ok else ' (DEGRADED — timeout or empty result)'}")
        except Exception as e:
            _log(f"answer: model FAILED after {_since(t_run)}", repr(e))
            reply = f"⚠️ сбой: {e}"
        else:
            # Persisting the session must never be able to eat the answer. It used to sit inside the
            # try above, so a transient sessions.json write error (disk full, bad permissions)
            # replaced a perfectly good reply with "⚠️ сбой: …" — losing the turn's real output over
            # bookkeeping. Worst case now: continuity is lost, the answer is not.
            try:
                _save_session(room_id, new_sid, gen)
            except Exception as e:
                _log("session save failed (answer kept)", room_id, repr(e))

        text_no_files, file_paths = media.extract_file_blocks(reply)
        text_clean, voice_blocks = media.extract_voice_blocks(text_no_files)

        if text_clean:
            t_send = time.monotonic()
            await _send_text(room_id, text_clean)
            _log(f"answer: posted {len(text_clean)} chars in {_since(t_send)}")
        else:
            # Not hypothetical: a turn whose whole output was [FILE]/[VOICE] markers, or an empty
            # result, used to leave the room in total silence — indistinguishable from a hang.
            _log("answer: NOTHING to post as text"
                 f" (files={len(file_paths)}, voice={len(voice_blocks)})")
        for lang, speech, speed in voice_blocks:
            t_tts = time.monotonic()
            ogg = await media.synthesize(speech, speed=speed)
            _log(f"answer: TTS {len(speech)} chars in {_since(t_tts)}"
                 + ("" if ogg else " — FAILED"))
            if ogg:
                await _send_voice(room_id, ogg)
                try: ogg.unlink()
                except OSError: pass
        for fp in file_paths:
            t_f = time.monotonic()
            await _send_file(room_id, fp)
            _log(f"answer: sent file {_clip(fp, 60)} in {_since(t_f)}")
    finally:
        await _typing_off(room_id)


def _mine(room: MatrixRoom, event) -> bool:
    return (event.sender == OWNER and room.room_id in ROOMS
            and event.sender != BOT_MXID and event.server_timestamp >= _started)


def _room_label(room_id: str) -> str:
    """Human-readable room tag for logs — the name if nio knows it, else a short id."""
    try:
        room = (_client.rooms or {}).get(room_id) if _client else None
        name = getattr(room, "display_name", None) or getattr(room, "name", None)
        if name:
            return str(name)
    except Exception:
        pass
    return room_id[:14]


# ── Concurrency: one turn per room, rooms genuinely in parallel ───────────────────────────────────
# nio invokes event callbacks INLINE and SEQUENTIALLY from inside sync_forever
# (`await cb.async_execute(event, room)`), so awaiting a whole claude turn inside a callback froze
# the ENTIRE sync loop: a message posted in another room was not even *received* until the first
# turn finished — its "typing…" only lit up afterwards, and the bridge was deaf meanwhile. So a
# callback must return immediately and hand the work to a detached task.
# The per-room lock keeps ONE room strictly FIFO (two messages in the same room must never race for
# that room's claude session, which is keyed by room id); different rooms now run concurrently.
_room_locks: dict[str, asyncio.Lock] = {}
_turns: set[asyncio.Task] = set()  # strong refs — a bare create_task may be GC'd mid-flight


_turn_seq = 0  # per-turn id so interleaved lines from parallel rooms can be told apart


def _spawn_turn(room_id: str, coro, kind: str = "turn", typing_from_start: bool = True) -> None:
    """Run `coro` off the sync loop, serialized against other turns in the SAME room.

    `typing_from_start` puts "typing…" up the moment the message is ACCEPTED — including the wait in
    the room queue, which is exactly when the owner walks in to check whether anything is happening.
    Media turns pass False: while we are still pulling THEIR upload off the homeserver there is
    nothing to claim we are typing about; those turns raise the indicator once the download is done.
    """
    global _turn_seq
    _turn_seq += 1
    tid = _turn_seq

    async def _serialized():
        lock = _room_locks.setdefault(room_id, asyncio.Lock())
        label = _room_label(room_id)
        if typing_from_start:
            _typing_on(room_id)
        # A message that arrives while the room is busy waits here, invisibly. That wait was the
        # single most confusing thing about this door: from the outside a queued message and a dead
        # bridge look identical. Log both the fact and how long it cost.
        queued_at = time.monotonic()
        if lock.locked():
            _log(f"#{tid} {kind} QUEUED in {label} — the room is busy with an earlier turn")
        try:
            async with lock:
                waited = time.monotonic() - queued_at
                _log(f"#{tid} {kind} START in {label}"
                     + (f" (waited {waited:.1f}s in queue)" if waited > 0.5 else ""))
                t0 = time.monotonic()
                try:
                    await coro
                    _log(f"#{tid} {kind} END in {label} after {_since(t0)}")
                except Exception as e:                 # a failed turn must never kill the bridge
                    _log(f"#{tid} {kind} FAILED in {label} after {_since(t0)}", repr(e))
                    try:
                        await _send_text(room_id, f"⚠️ сбой обработки: {e}")
                    except Exception as se:
                        _log(f"#{tid} could not even report the failure", repr(se))
        finally:
            if typing_from_start:
                await _typing_off(room_id)
    t = asyncio.create_task(_serialized())
    _turns.add(t)
    t.add_done_callback(_turns.discard)


def _spawn_free(coro) -> None:
    """Run `coro` off the sync loop WITHOUT the room lock — for control commands.

    Deliberate: /status and /jobs are most needed exactly WHILE a long turn is running. If they
    queued behind the room lock they would answer only after that turn finished, which is when the
    user no longer needs them."""
    async def _guarded():
        try:
            await coro
        except Exception as e:
            _log("command failed", repr(e))
    t = asyncio.create_task(_guarded())
    _turns.add(t)
    t.add_done_callback(_turns.discard)


def _jobs_snapshot(room_id: str = "") -> tuple[list[str], int]:
    """(lines, active_count) for the durable jobs. Reaps first so a dead runner isn't shown alive."""
    try:
        _bot_dir = str(JOBS_DIR.parent)
        if _bot_dir not in sys.path:  # long-lived process: don't grow sys.path every tick
            sys.path.insert(0, _bot_dir)
        import job_ctl
        job_ctl.reap_all()
        lines, active = [], 0
        for p in job_ctl._job_dirs()[-10:]:
            st = job_ctl._read_status(p)
            spec = job_ctl._read_spec(p)
            if st in job_ctl.ACTIVE:
                active += 1
            mark = "▶" if st in job_ctl.ACTIVE else ("✅" if st == "done" else
                                                     "⏹" if st == "cancelled" else "❌")
            origin = (spec.get("reply_to") or {}).get("room_id", "")
            here = " ← отсюда" if room_id and origin == room_id else ""
            lines.append(f"{mark} `{p.name}` · {st} · {spec.get('label','')}{here}")
        return lines, active
    except Exception as e:
        return [f"⚠️ не смог прочитать задачи: {e}"], 0


async def _cmd_new(room_id: str) -> None:
    had = _clear_session(room_id)
    await _send_text(room_id, "🧹 Сессия этой комнаты сброшена — дальше с чистого листа.\n"
                              + ("Прежний контекст отвязан (файлы, память и знания на месте — "
                                 "теряется только ход разговора)." if had else
                                 "Активной сессии и не было, так что ничего не потерялось.")
                              + "\n\nДругие комнаты не затронуты.")


async def _cmd_status(room_id: str) -> None:
    sid = _sessions().get(room_id)
    _, active = _jobs_snapshot()
    up = int((time.time() * 1000 - _started) / 1000)
    await _send_text(room_id, "\n".join([
        "**Статус**",
        f"· модель: `{claude_bridge._model()}`",
        f"· сессия комнаты: `{sid[:8] + '…' if sid else 'нет (следующее сообщение начнёт новую)'}`",
        f"· комнат подключено: {len(ROOMS)} (работают параллельно)",
        f"· фоновых задач активно: {active}",
        f"· мост поднят: {up // 3600}ч {(up % 3600) // 60}м назад",
    ]))


async def _cmd_jobs(room_id: str) -> None:
    lines, active = _jobs_snapshot(room_id)
    body = "\n".join(lines) if lines else "_(задач нет)_"
    # /cancel was deliberately removed from chat (the owner's decision: /new is the only action).
    # Advertising it here was a leftover that contradicted /help and just fell through to a claude
    # turn behind the room lock. Cancelling is an ops verb now.
    await _send_text(room_id, f"**Фоновые задачи** (активных: {active})\n\n{body}\n\n"
                              f"_Остановить — только из CLI:_ `python3 bot/job_ctl.py cancel <id>`")


async def _cmd_help(room_id: str) -> None:
    await _send_text(room_id, "\n".join([
        "**Команды**",
        "· `/new` — сбросить сессию ЭТОЙ комнаты (остальные не трогает)",
        "· `/status` — модель, сессия, комнаты, активные задачи",
        "· `/jobs` — фоновые durable-задачи и их статусы",
        "",
        "_Единственное действие — `/new`; остальное только смотрит._",
        "_Отвечают сразу, даже пока идёт длинный ответ._",
    ]))


async def _dispatch_command(room_id: str, body: str) -> bool:
    """True if `body` was a command (already handled). Commands bypass the room lock on purpose."""
    parts = body.strip().split()
    cmd = parts[0].lower()
    if cmd == "/new":
        _spawn_free(_cmd_new(room_id))
    elif cmd == "/status":
        _spawn_free(_cmd_status(room_id))
    elif cmd == "/jobs":
        _spawn_free(_cmd_jobs(room_id))
    elif cmd in ("/help", "/commands"):
        _spawn_free(_cmd_help(room_id))
    else:
        return False
    _log("command", cmd)
    return True


async def on_text(room, event: RoomMessageText) -> None:
    if _mine(room, event) and (event.body or "").strip():
        body = event.body
        if body.lstrip().startswith("/") and await _dispatch_command(room.room_id, body):
            return
        _log("text", repr(body[:60]))
        _spawn_turn(room.room_id, _answer(room.room_id, body), "text")


async def on_image(room, event: RoomMessageImage) -> None:
    if _mine(room, event):
        _log("image", event.url)
        _spawn_turn(room.room_id, _turn_image(room.room_id, event), "image", typing_from_start=False)


async def _turn_image(room_id: str, event: RoomMessageImage) -> None:
    t0 = time.monotonic()
    data = await _download_event_media(event)
    if not data:
        _log("image: download FAILED")
        await _send_text(room_id, "⚠️ не смог скачать изображение")
        return
    _log(f"image: downloaded {len(data)/1024:.0f} KB in {_since(t0)}")
    ext = (event.body or "img").rsplit(".", 1)[-1].lower()
    ext = ext if ext in ("jpg", "jpeg", "png", "gif", "webp") else "jpg"
    p = TMP / f"mb_img_{int(time.time()*1000)}.{ext}"
    p.write_bytes(data)
    prompt = event.body if (event.body and not event.body.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))) else "Пользователь прислал изображение."
    try:
        await _answer(room_id, prompt, images=[str(p)])
    finally:
        try: p.unlink()
        except OSError: pass


async def on_audio(room, event: RoomMessageAudio) -> None:
    if _mine(room, event):
        _log("audio", event.url)
        _spawn_turn(room.room_id, _turn_audio(room.room_id, event), "voice", typing_from_start=False)


async def _turn_audio(room_id: str, event: RoomMessageAudio) -> None:
    # Every stage is timed separately: "the voice reply was slow" used to be unattributable — download
    # from the homeserver, OpenAI transcription and the model's own thinking were one opaque block.
    t0 = time.monotonic()
    data = await _download_event_media(event)
    if not data:
        _log("voice: download FAILED")
        await _send_text(room_id, "⚠️ не смог скачать голосовое")
        return
    _log(f"voice: downloaded {len(data)/1024:.0f} KB in {_since(t0)}")
    p = TMP / f"mb_voice_{int(time.time()*1000)}.ogg"
    p.write_bytes(data)
    dur_ms = media.audio_duration_ms(str(p))
    t1 = time.monotonic()
    # Download is over — from here the work is OURS, so the indicator goes up: transcription of a
    # long voice note is seconds of otherwise unexplained silence before the model even starts.
    _typing_on(room_id)
    try:
        try:
            text = await media.transcribe(str(p))
        except Exception as e:
            _log(f"voice: transcription FAILED after {_since(t1)}", repr(e))
            await _send_text(room_id, f"⚠️ ошибка транскрипции: {e}")
            return
        finally:
            try: p.unlink()
            except OSError: pass
        _log(f"voice: {dur_ms/1000:.0f}s of audio transcribed in {_since(t1)}"
             f" → {len(text)} chars", _clip(text, 60))
        if not text:
            await _send_text(room_id, "⚠️ не удалось распознать речь")
            return
        await _answer(room_id, text)
    finally:
        await _typing_off(room_id)


async def on_file(room, event: RoomMessageFile) -> None:
    if _mine(room, event):
        _log("file", event.body, event.url)
        _spawn_turn(room.room_id, _turn_file(room.room_id, event), "file", typing_from_start=False)


async def on_video(room, event) -> None:
    """Video is out of scope by decision — answer in one line, download nothing.

    Two separate holes closed here. Plain `m.video` had NO callback registered at all (only
    RoomEncryptedVideo was, and it went to on_file), so a video sent directly to the room produced
    total silence — not even a log line. And a video that DID reach on_file was downloaded in full
    and handed to the model as an .inbox file, which costs megabytes and a whole turn to arrive at
    "I can't work with this". There is no video understanding here, so refuse immediately."""
    if _mine(room, event):
        _log("video refused (not supported)", getattr(event, "body", ""))
        _spawn_free(_send_text(room.room_id, "🎬 Это видео — с ним я работать не умею."))


class _RawMedia:
    """Minimal stand-in for a nio media event, built from the RAW event JSON.

    Needed because nio refuses to parse an encrypted-attachment message that arrives in an
    UNENCRYPTED room: its plaintext schema demands `content.url`, this content only has
    `content.file`, so nio yields a BadEvent and no media class at all. That is exactly what a
    message FORWARDED out of an encrypted room looks like — and it was being dropped in total
    silence (no reply, no warning). We rebuild the few attributes the turn handlers touch."""

    def __init__(self, content: dict) -> None:
        f = content.get("file") or {}
        self.url = f.get("url") or content.get("url")
        self.key = f.get("key")
        self.iv = f.get("iv")
        self.hashes = f.get("hashes")
        self.body = content.get("body") or "attachment"
        self.filename = content.get("filename")  # MSC2530: real name when `body` is a caption


async def on_bad_event(room, event: BadEvent) -> None:
    """Rescue media that nio could not parse (see _RawMedia). Anything else is left alone."""
    src = getattr(event, "source", None) or {}
    content = src.get("content") or {}
    msgtype = content.get("msgtype")
    if msgtype not in ("m.audio", "m.image", "m.file", "m.video"):
        return
    if not (content.get("file") or {}).get("url"):
        return
    # BadEvent carries sender/server_timestamp, so the usual ownership gate still applies.
    if not (event.sender == OWNER and room.room_id in ROOMS
            and event.sender != BOT_MXID and event.server_timestamp >= _started):
        return
    if msgtype == "m.video":
        # Same decision as on_video: nothing to gain from downloading and decrypting a video we
        # cannot read. Answer, don't fetch.
        _log("rescued forward is a video — refused", content.get("body"))
        _spawn_free(_send_text(room.room_id, "🎬 Это видео — с ним я работать не умею."))
        return
    shim = _RawMedia(content)
    _log("rescued encrypted-forward media", msgtype, shim.body)
    if msgtype == "m.audio":
        _spawn_turn(room.room_id, _turn_audio(room.room_id, shim), "voice(rescued)", typing_from_start=False)
    elif msgtype == "m.image":
        _spawn_turn(room.room_id, _turn_image(room.room_id, shim), "image(rescued)", typing_from_start=False)
    else:
        _spawn_turn(room.room_id, _turn_file(room.room_id, shim), "file(rescued)", typing_from_start=False)


async def _turn_file(room_id: str, event: RoomMessageFile) -> None:
    data = await _download_event_media(event)
    if not data:
        await _send_text(room_id, "⚠️ не смог скачать файл")
        return
    INBOX.mkdir(parents=True, exist_ok=True)
    # Under MSC2530 a captioned upload puts the CAPTION in `body` and the real name in `filename`.
    # Deriving the saved name from `body` alone therefore threw the extension away and saved
    # "разбери_этот_лог" with no suffix. Prefer `filename` when the client sent one; when it did not,
    # behave exactly as before so nothing regresses.
    real_name = getattr(event, "filename", None)
    name = _safe_name(real_name or event.body)
    dest = INBOX / f"{int(time.time())}_{name}"
    dest.write_bytes(data)
    _log("file saved ->", dest)
    # A caption is an INSTRUCTION, exactly as on Telegram. It used to be consumed only as the saved
    # filename, so "разбери этот лог и найди ошибку" was silently reduced to a file name and the
    # turn got the generic prompt instead.
    caption = (getattr(event, "body", "") or "").strip()
    if real_name:
        hint = caption if caption and caption != real_name else ""
    else:
        hint = caption if caption and caption != name and not caption.startswith(name) else ""
    await _answer(room_id, f"Пользователь прислал файл, сохранён по пути: {dest} "
                           f"(НЕ выполнять автоматически — прочитай и предложи, что делать)."
                           + (f"\nПодпись пользователя к файлу: {hint}" if hint else ""))


# ── Durable background jobs — report Matrix-originated jobs BACK to their room ────────────────
# A durable job (tools/workflow_job.py) launched from a Matrix turn is tagged reply_to.door=matrix
# (see claude_bridge env + bot/job_ctl.py). The Telegram poller skips those; THIS loop delivers them
# into the originating room, in my voice, mirroring the Telegram wake-report. Same job dir + the same
# `notified` marker, so the two doors never double-deliver.
JOBS_DIR = REPO_ROOT / "bot" / "jobs"
JOBS_POLL_SEC = 20
JOBS_PRUNE_DAYS = 3
JOBS_DELIVER_RETRY_SEC = 900  # a "delivering" marker older than this = the report never landed
WAKE_FEED = 8000  # chars of result fed into the report reasoning

def _jread(p: Path) -> str:
    try: return p.read_text(errors="replace").strip()
    except Exception: return ""

async def _deliver_job_to_room(job_dir: Path, spec: dict, status: str, room_id: str,
                               prefix: str = "") -> None:
    label = spec.get("label", "job")
    rc = _jread(job_dir / "exit_code"); dur = _jread(job_dir / "duration_sec")
    result = _jread(job_dir / "result.txt")
    if status == "cancelled":
        # Deliberate stop — a full reasoning wake-report would be waste. One line is honest.
        await _send_text(room_id, f"{prefix}⏹ Фонова задача «{label}» скасована.\n\n{result[-1500:]}")
        return
    if len(result) > WAKE_FEED:
        result = "…(обрізано)…\n" + result[-WAKE_FEED:]
    try:
        cmd = base64.b64decode(spec.get("cmd_b64", "")).decode(errors="replace")
    except Exception:
        cmd = ""
    await _send_text(room_id, f"{prefix}🦐 Прокинулась — фонова задача «{label}» завершилась, дивлюсь результат…")
    prompt = (
        "[АВТОНОМНЕ ПРОБУДЖЕННЯ — доповідь про фонову задачу]\n"
        f"Мітка: {label}\njob: {spec.get('id', job_dir.name)} · тривалість {dur}s · код виходу {rc} "
        f"({'успішно' if status == 'done' else 'ПОМИЛКА'})\nКоманда: {cmd}\n"
        "--- Результат (stdout+stderr) ---\n" f"{result or '(порожній вивід)'}\n"
        "--- Кінець результату ---\n\n"
        "Ти — креветка. Це не інтерактивний чат, а автономне пробудження, щоб стисло доповісти "
        "користувачу про завершену фонову задачу: що зроблено, головне з результату, чи все гаразд "
        "і що варто зробити далі. Відповідай мовою користувача (типово російською), стисло, без "
        "зайвих преамбул. Не вигадуй того, чого немає в результаті."
    )
    try:
        # room_id is passed so that a durable job launched DURING this report is tagged with this
        # room and reports back here — without it the report turn carried no door/room env and any
        # such job silently defaulted to the Telegram door.
        reply, _sid, ok = await claude_bridge.run(prompt, None, room_id=room_id)  # isolated session
    except Exception as e:
        reply, ok = "", False
        _log("job report reasoning failed", job_dir.name, e)
    # `ok` is what makes the v0 fallback below reachable at all. claude_bridge.run NEVER returns an
    # empty string — a deadline kill yields "⚠️ Ход прерван…" and an empty result yields
    # "(пустой ответ)", both truthy — so the old `if reply.strip()` check always won and a degraded
    # turn was published as if it were the report, with the real result.txt never shown.
    shown = False
    if ok and reply.strip():
        text_no_files, file_paths = media.extract_file_blocks(reply)
        text_clean, _v = media.extract_voice_blocks(text_no_files)
        if text_clean:
            await _send_text(room_id, text_clean)
        for fp in file_paths:
            await _send_file(room_id, fp)
        shown = bool(text_clean or file_paths)
    if not shown:  # v0 fallback: raw tail — the owner sees the result even when the report degraded
        await _send_text(room_id, f"«{label}» · {dur}s · rc={rc} "
                         f"({'ok' if status == 'done' else 'ПОМИЛКА'})\n\n{(result or '(порожньо)')[-3000:]}")

# Job dirs whose delivery THIS process has already dispatched and not yet finished. The on-disk
# "delivering:" marker alone was not enough: its staleness timer starts at DISPATCH, but the delivery
# is queued behind the room lock, which legitimately holds for up to _SILENCE_LIMIT_S (30 min) or the
# 3 h ceiling. So the poller kept re-dispatching the same report every JOBS_DELIVER_RETRY_SEC and the
# owner got 1 + floor(hold/900) copies of it, each a full reasoning turn.
_delivering: set[str] = set()
JOBS_DELIVER_MAX_ATTEMPTS = 3  # then stop retrying and say so, instead of looping forever


def _attempts(marker: str) -> int:
    """Attempt count carried in the marker as "delivering:<ts>#<n>" (absent in the v1 format = 0)."""
    try:
        return int(marker.rsplit("#", 1)[1])
    except (IndexError, ValueError):
        return 0


async def _deliver_and_mark(job_dir: Path, spec: dict, status: str, room_id: str,
                            stray: str = "") -> None:
    """Deliver, then complete the marker. If this raises, the marker stays "delivering:" and the
    poller retries it after JOBS_DELIVER_RETRY_SEC instead of losing the report."""
    try:
        await _deliver_job_to_room(job_dir, spec, status, room_id, stray)
        (job_dir / "notified").write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    finally:
        _delivering.discard(job_dir.name)


def _reap_stuck_jobs() -> None:
    """Shared reaper from bot/job_ctl.py (single source of truth with the Telegram door): a runner
    killed out-of-band leaves status=running forever, so the report never arrives and the launch gate
    stays blocked. Imported lazily so the bridge never fails to start over it."""
    try:
        _bot_dir = str(JOBS_DIR.parent)
        if _bot_dir not in sys.path:  # long-lived process: don't grow sys.path every tick
            sys.path.insert(0, _bot_dir)  # lil_worker/bot
        import job_ctl  # stdlib-only, safe to import here
        for name in job_ctl.reap_all():
            _log("reaper: job", name, "runner died out-of-band → marked failed")
    except Exception as e:
        _log("reaper failed", e)


async def _poll_jobs_loop() -> None:
    while True:
        try:
            if JOBS_DIR.is_dir():
                _reap_stuck_jobs()
                now = time.time()
                for job_dir in sorted(JOBS_DIR.iterdir()):
                    if not job_dir.is_dir() or not (job_dir / "spec.json").exists():
                        continue
                    if job_dir.name in _delivering:
                        continue  # this process is already delivering it (possibly still queued)
                    notified = job_dir / "notified"
                    attempt = 0
                    if notified.exists():
                        marker = _jread(notified)
                        # Two-phase marker. "delivering:<ts>" is written BEFORE dispatch so a crash
                        # mid-report cannot double-deliver; it is overwritten with the final
                        # timestamp once the report is out. If a bridge restart happens in between,
                        # the job used to be lost FOREVER (marked handled, never delivered) — so a
                        # stale "delivering" is retried instead of abandoned.
                        if marker.startswith("delivering:"):
                            try:
                                stale = now - notified.stat().st_mtime > JOBS_DELIVER_RETRY_SEC
                            except OSError:
                                stale = False
                            if not stale:
                                continue
                            attempt = _attempts(marker)
                            if attempt >= JOBS_DELIVER_MAX_ATTEMPTS:
                                # Now that a refused room_send RAISES, a permanently undeliverable
                                # report (room forbidden, server down for good) would otherwise be
                                # retried every 900 s forever. Stop, and leave a breadcrumb.
                                notified.write_text(
                                    f"failed-delivery:{time.strftime('%Y-%m-%dT%H:%M:%S%z')}")
                                _log("job report undeliverable after",
                                     attempt, "attempts — giving up", job_dir.name)
                                continue
                            _log("job report never completed — retrying", job_dir.name,
                                 f"(attempt {attempt + 1}/{JOBS_DELIVER_MAX_ATTEMPTS})")
                        else:
                            try:
                                if now - notified.stat().st_mtime > JOBS_PRUNE_DAYS * 86400:
                                    shutil.rmtree(job_dir, ignore_errors=True)
                            except OSError:
                                pass
                            continue
                    if _jread(job_dir / "status") not in ("done", "failed", "cancelled"):
                        continue
                    try:
                        spec = json.loads((job_dir / "spec.json").read_text())
                    except Exception:
                        continue
                    rt = spec.get("reply_to") or {}
                    if rt.get("door") != "matrix":
                        continue  # not ours — the Telegram poller handles it
                    room_id = rt.get("room_id")
                    status = _jread(job_dir / "status")
                    stray = ""
                    if room_id not in ROOMS:
                        # The room list changed since launch (renamed/left/reconfigured). Never drop
                        # a finished job's result on the floor — deliver it to the main room and say
                        # where it was meant to go.
                        stray = f"⚠️ Задача була запущена з кімнати `{room_id}`, якої більше немає " \
                                f"в списку — доповідаю сюди.\n\n"
                        _log("job room gone, falling back to main room", job_dir.name, room_id)
                        room_id = MAIN_ROOM
                    # Phase 1 of the marker: claimed, not yet delivered (see the stale check above).
                    notified.write_text(
                        f"delivering:{time.strftime('%Y-%m-%dT%H:%M:%S%z')}#{attempt + 1}")
                    _delivering.add(job_dir.name)
                    _log(f"job {job_dir.name} finished ({status}) → dispatching report to"
                         f" {_room_label(room_id)}, attempt {attempt + 1}")
                    # Через _spawn_turn: serialized against that room's live turns (a report must not
                    # land mid-answer) while the poll loop stays free to keep ticking.
                    _spawn_turn(room_id, _deliver_and_mark(job_dir, spec, status, room_id, stray), "job-report")
        except Exception as e:
            _log("jobs poll error", e)
        await asyncio.sleep(JOBS_POLL_SEC)


async def main() -> None:
    global _client
    _client = AsyncClient(HOMESERVER, BOT_MXID)
    _client.access_token = TOKEN
    who = await _client.whoami()
    if getattr(who, "user_id", None) != BOT_MXID:
        raise SystemExit(f"token/whoami mismatch: {who}")
    _client.user_id = BOT_MXID
    _client.add_event_callback(on_text, RoomMessageText)
    _client.add_event_callback(on_image, RoomMessageImage)
    _client.add_event_callback(on_audio, RoomMessageAudio)
    _client.add_event_callback(on_file, RoomMessageFile)
    # Forwarded-from-an-encrypted-room media arrives as these DISTINCT classes (not subclasses
    # of RoomMessage*), so without these four registrations such a message hits no handler and
    # is dropped in silence — exactly how a forwarded voice note went missing.
    _client.add_event_callback(on_video, RoomMessageVideo)   # plain m.video had NO handler at all
    _client.add_event_callback(on_image, RoomEncryptedImage)
    _client.add_event_callback(on_audio, RoomEncryptedAudio)
    _client.add_event_callback(on_file, RoomEncryptedFile)
    _client.add_event_callback(on_video, RoomEncryptedVideo)  # forwarded video: refuse, don't fetch
    # …and the case that actually bit us: an encrypted attachment forwarded INTO an unencrypted room
    # fails nio's schema entirely and arrives as BadEvent.
    _client.add_event_callback(on_bad_event, BadEvent)
    # Room NAMES are unknown until the first sync, so list ids here; every later line uses the name.
    _log(f"bridge UP as {BOT_MXID}, owner {OWNER}, {len(ROOM_ORDER)} room(s):",
         ", ".join(r[:14] for r in ROOM_ORDER))
    jobs_task = asyncio.create_task(_poll_jobs_loop())  # report Matrix-originated durable jobs here
    try:
        await _client.sync_forever(timeout=30000, full_state=False)
    finally:
        jobs_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
