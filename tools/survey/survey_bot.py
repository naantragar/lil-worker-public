#!/usr/bin/env python3
"""A questionnaire bot: hand the owner one block at a time, collect ОК or a correction.

Why this exists. The annotation sheets (readings to check, the «хто водить» answer key) are text
files, and a text file is not something anyone marks up on a phone. The work then does not happen,
and the whole identity system waits on it. A web page would need a subdomain, TLS and a front end;
this needs none of that — the upstream alerts bot token already exists and its alerting has been
switched off.

    python3 tools/survey/survey_bot.py            # long-polls, one owner, stdlib only

SHAPE OF THE DATA (all plain JSON under knowledge/upstream/surveys/):

    <name>.json           {"title": …, "options"?: […], "items": [{"id": 1, "text": "…"}, …]}
    answers/<name>.json   [{"id": 1, "verdict": <key of the set's option>, "comment": …, "ts": …}]
    state.json            {"<uid>": {"survey", "msg_id", "awaiting_comment"?, "verdict"?}}

Progress is derived from the answers file, never stored separately — so the two cannot disagree, and
an interrupted session resumes exactly where it stopped. A re-answer overwrites the old row rather
than appending, so the file is always "one verdict per item".

The bot itself is stdlib-only (urllib), so no dependency of its own can break a running service.
The ONE exception is voice comments, which shell out to `transcribe.py` in the main bot's venv —
a separate process on purpose, so a transcription that hangs cannot take the questionnaire down.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "knowledge" / "upstream" / "surveys"
ANSWERS = DIR / "answers"
STATE = DIR / "state.json"
ENV = Path("~/upstream-backups/monitor.env")
OWNER = 323182394
CHUNK = 3500              # Telegram hard limit is 4096; leave room for the keyboard


def token() -> str:
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("UPSTREAM_ALERT_TG_TOKEN="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"немає UPSTREAM_ALERT_TG_TOKEN у {ENV}")


API = f"https://api.telegram.org/bot{token()}"

# --- IPv4 only, and this is the fix for the stalls, not a precaution -------------------------
# Symptom (14.09.2026): every so often an update took EXACTLY the socket timeout and then
# succeeded — 70.2s at timeout 70, 20.1s at 20, 8.2s at 8. Measured per address family against the
# live API: IPv4 0.1s every time; IPv6 0.1s, then 4.2s. The v6 path to api.telegram.org from this
# box intermittently swallows packets, so the request sits until the timer fires and a retransmit
# finally lands. DNS, TLS, the methods themselves and both bare TCP connects all measured clean —
# which is why nothing before this pointed at the network.
_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
    except socket.gaierror:
        return _getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _ipv4_only
# ---------------------------------------------------------------------------------------------


def say(**params) -> dict:
    """sendMessage that COMPLAINS. The first version ignored the API answer, so a refused send
    looked exactly like a delivered one — the owner pressed /start, the update was consumed, and
    nothing appeared anywhere, including the log."""
    r = call("sendMessage", **params)
    if not r.get("ok"):
        print(f"sendMessage FAILED chat={params.get('chat_id')}: {str(r.get('error'))[:300]}",
              file=sys.stderr, flush=True)
    return r


def call(method: str, _timeout: int = 8, _retry: bool = True, **params) -> dict:
    """One API call, with its own clock.

    The 70-second stalls of 14.09 were invisible because nothing timed the individual calls — the
    whole update just took 70s, which is exactly the old blanket socket timeout. Now every call over
    3s names itself in the log, and a stuck call costs 20s instead of 70.
    """
    data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
            for k, v in params.items() if v is not None}
    req = urllib.request.Request(f"{API}/{method}",
                                 data=urllib.parse.urlencode(data).encode("utf-8"))
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=_timeout) as r:
            out = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        out = {"ok": False, "error": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:                                   # noqa: BLE001
        out = {"ok": False, "error": str(e)[:300]}
    dt = time.monotonic() - t0
    if dt > 3 and method != "getUpdates":
        print(f"  ПОВІЛЬНО {method}: {dt:.1f}s ok={out.get('ok')} "
              f"{str(out.get('error') or '')[:120]}", file=sys.stderr, flush=True)
    # The stalls that prompted this were the IPv6 path (see the pin above) and are gone. The single
    # retry stays as cheap insurance against any other transient: it costs nothing when calls
    # succeed, and the ПОВТОР line is the evidence if something new starts failing.
    if _retry and not out.get("ok") and method != "getUpdates":
        print(f"  ПОВТОР {method} після {dt:.1f}s", file=sys.stderr, flush=True)
        return call(method, _timeout=_timeout, _retry=False, **params)
    return out


VENV = REPO / "bot" / ".venv" / "bin" / "python"
TRANSCRIBE = REPO / "tools" / "survey" / "transcribe.py"


def voice_to_text(file_id: str, seconds: int) -> str | None:
    """Download a voice note and hand it to the transcriber. None means "say so and keep waiting".

    Runs the OpenAI client in the main bot's venv as a SUBPROCESS: this bot stays stdlib-only, and a
    transcription that hangs or dies cannot take the questionnaire down with it.
    """
    f = call("getFile", file_id=file_id, _timeout=15)
    if not f.get("ok"):
        print(f"getFile: {str(f.get('error'))[:200]}", file=sys.stderr, flush=True)
        return None
    path = f["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{token()}/{path}"
    # Telegram hands voice notes back as `.oga`, which OpenAI rejects outright
    # ("Unsupported file format oga") even though the bytes are the same Ogg/Opus it accepts as
    # `.ogg`. The main bot never hit this because it always writes `.ogg`. Anything else — an mp3
    # sent as audio, an mp4 video note — keeps its real extension, since renaming THOSE would break
    # a format that actually is what it says.
    suffix = Path(path).suffix.lower()
    if suffix in ("", ".oga"):
        suffix = ".ogg"
    tmp = Path(tempfile.gettempdir()) / f"survey_voice_{file_id[:24]}{suffix}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            tmp.write_bytes(r.read())
        p = subprocess.run([str(VENV), str(TRANSCRIBE), str(tmp), "--seconds", str(seconds)],
                           capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            print(f"transcribe rc={p.returncode}: {p.stderr[-300:]}", file=sys.stderr, flush=True)
            return None
        return (p.stdout or "").strip() or None
    except Exception as e:                                   # noqa: BLE001
        print(f"voice_to_text: {e}", file=sys.stderr, flush=True)
        return None
    finally:
        tmp.unlink(missing_ok=True)


def load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return default


def save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)                                           # atomic: a crash cannot truncate answers


def surveys() -> list[dict]:
    out = []
    for p in sorted(DIR.glob("*.json")):
        if p.name == "state.json":
            continue
        d = load(p, None)
        if isinstance(d, dict) and d.get("items"):
            out.append({"name": p.stem, "title": d.get("title") or p.stem,
                        "total": len(d["items"])})
    return out


def answers_of(name: str) -> list[dict]:
    return load(ANSWERS / f"{name}.json", [])


def record(name: str, item_id: int, verdict: str, comment: str | None) -> None:
    rows = [r for r in answers_of(name) if r.get("id") != item_id]
    rows.append({"id": item_id, "verdict": verdict, "comment": comment,
                 "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    rows.sort(key=lambda r: r["id"])
    save(ANSWERS / f"{name}.json", rows)


def next_item(name: str) -> dict | None:
    d = load(DIR / f"{name}.json", {})
    done = {r["id"] for r in answers_of(name)}
    for it in d.get("items", []):
        if it["id"] not in done:
            return it
    return None


def menu(uid: int, note: str = "") -> None:
    rows = []
    for s in surveys():
        done = len(answers_of(s["name"]))
        mark = "✅ " if done >= s["total"] else ""
        rows.append([{"text": f"{mark}{s['title']}  ({done}/{s['total']})",
                      "callback_data": f"pick:{s['name']}"}])
    if not rows:
        say(chat_id=uid, text="Наборів поки немає.")
        return
    say(chat_id=uid, text=(note + "\n\n" if note else "") + "Обери набір:",
        reply_markup={"inline_keyboard": rows})


DEFAULT_OPTIONS = [{"key": "ok", "label": "✅ вірно"},
                   {"key": "wrong", "label": "✏️ не так", "comment": True},
                   {"key": "unclear", "label": "🤷 не видно"}]


def options_of(name: str) -> list[dict]:
    """Answer buttons for THIS set. A survey that asks «хто водить» cannot be answered with
    «вірно / не так» — the verdict vocabulary has to match the question, or the data is worthless."""
    d = load(DIR / f"{name}.json", {})
    opts = d.get("options")
    return opts if isinstance(opts, list) and opts else DEFAULT_OPTIONS


def kb(name: str, item_id: int, chosen: str | None = None) -> dict:
    """Answer keyboard. After an answer the buttons stay visible but go `disabled` (Bot API 10.3)
    with the chosen one ticked — so the message under the finger shows what was recorded."""
    keys = []
    for o in options_of(name):
        label = o["label"]
        if chosen is not None:
            label = ("▸ " if o["key"] == chosen else "") + label
        b = {"text": label, "callback_data": f"a:{name}:{item_id}:{o['key']}"}
        if chosen is not None:
            b["disabled"] = True
        keys.append(b)
    rows = [keys[i:i + 2] for i in range(0, len(keys), 2)]
    if chosen is None:
        rows.append([{"text": "⬅ до списку", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def show(uid: int, name: str, edit_id: int | None) -> None:
    """Put the next unanswered question into the LIVE message, editing it in place.

    v1 sent a new message per question, which is why a click looked like nothing happening: the
    message he tapped never changed. An inline flow has to edit what it is driving.

    `edit_id=None` means "start a fresh message at the bottom". That is what the comment path uses:
    once the owner has typed an explanation, the question he was working on is no longer the last
    thing in the chat, and editing it in place would leave the live question ABOVE the finished
    exchange — the working spot ends up higher than the history and has to be scrolled back to.
    """
    it = next_item(name)
    state = load(STATE, {})
    if it is None:
        by = {}
        for r in answers_of(name):
            by[r["verdict"]] = by.get(r["verdict"], 0) + 1
        tally = ", ".join(f"{k}: {v}" for k, v in sorted(by.items()))
        if edit_id:
            call("editMessageText", chat_id=uid, message_id=edit_id,
                 text=f"Набір «{name}» пройдено: {len(answers_of(name))} відповідей ({tally}).")
        state.pop(str(uid), None)
        save(STATE, state)
        menu(uid, "Скажи крабу, що завершив - він розбере.")
        return
    total = len(load(DIR / f"{name}.json", {}).get("items", []))
    text = f"[{len(answers_of(name)) + 1}/{total}]\n\n{it['text']}"[:CHUNK]
    if edit_id:
        r = call("editMessageText", chat_id=uid, message_id=edit_id, text=text,
                 reply_markup=kb(name, it["id"]))
        if r.get("ok"):
            state[str(uid)] = {"survey": name, "msg_id": edit_id}
            save(STATE, state)
            return
        print(f"editMessageText FAILED: {str(r.get('error'))[:200]}", file=sys.stderr, flush=True)
    r = say(chat_id=uid, text=text, reply_markup=kb(name, it["id"]))
    if r.get("ok"):
        state[str(uid)] = {"survey": name, "msg_id": r["result"]["message_id"]}
        save(STATE, state)


def handle_callback(cq: dict) -> None:
    uid = cq["from"]["id"]
    print(f"btn from {uid}: {(cq.get('data') or '')[:60]!r}", file=sys.stderr, flush=True)
    call("answerCallbackQuery", callback_query_id=cq["id"])
    if uid != OWNER:
        print(f"  ЗБІЙ: id {uid} не збігається з OWNER {OWNER}", file=sys.stderr, flush=True)
        return
    data = cq.get("data") or ""
    state = load(STATE, {})
    if data == "menu":
        mid = (cq.get("message") or {}).get("message_id")
        if mid:
            # Without this the question stayed on screen with its buttons alive, so "pause" looked
            # exactly like "the button is stuck" — the message under the finger never changed.
            call("editMessageText", chat_id=uid, message_id=mid,
                 text="Зупинено. Відповіді збережено - можна повернутись у будь-який момент.")
        state.pop(str(uid), None)
        save(STATE, state)
        menu(uid)
        return
    if data.startswith("pick:"):
        name = data.split(":", 1)[1]
        show(uid, name, (cq.get("message") or {}).get("message_id"))
        return
    if data.startswith("a:"):
        _, name, item_id, key = data.split(":", 3)
        # The item id is whatever the SET calls its question. It used to be coerced with int(),
        # which was fine while every set numbered its items — and silently swallowed every click
        # in the first set that named them (`g_НИНДЗЯ`, 23.09.2026): the handler raised, the button
        # spun, nothing was recorded. Keep the id as it is written in the file.
        item_id = int(item_id) if item_id.lstrip("-").isdigit() else item_id
        opt = next((o for o in options_of(name) if o["key"] == key), None)
        if opt is None:                         # a button from an older version of the set
            send_item(uid, name)
            return
        mid = (cq.get("message") or {}).get("message_id")
        if opt.get("comment"):
            state[str(uid)] = {"survey": name, "msg_id": mid,
                               "awaiting_comment": item_id, "verdict": key}
            save(STATE, state)
            call("editMessageReplyMarkup", chat_id=uid, message_id=mid,
                 reply_markup=kb(name, item_id, chosen=key))
            say(chat_id=uid, text="Напиши одним повідомленням, що саме не так "
                                  "(або /skip, щоб лишити без пояснення).")
            return
        record(name, item_id, key, None)
        show(uid, name, mid)


def handle_message(msg: dict) -> None:
    uid = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip()
    voice = msg.get("voice") or msg.get("audio") or msg.get("video_note")
    print(f"msg from {uid}: {text[:60]!r}", file=sys.stderr, flush=True)
    if uid != OWNER:
        print(f"  ЗБІЙ: id {uid} не збігається з OWNER {OWNER} - відповідь не надсилається",
              file=sys.stderr, flush=True)
        return
    state = load(STATE, {})
    cur = state.get(str(uid)) or {}
    # A voice note where a comment is expected: transcribe it and use it as if it had been typed.
    # The transcript is echoed back, because a wrong word in a comment that becomes a glossary line
    # is worse than a slow one — he has to be able to see what was heard.
    if voice and cur.get("awaiting_comment") is not None:
        say(chat_id=uid, text="🎧 розшифровую…")
        heard = voice_to_text(voice.get("file_id"), int(voice.get("duration") or 0))
        if not heard:
            say(chat_id=uid, text="Не вийшло розшифрувати. Спробуй ще раз або напиши текстом.")
            return
        record(cur["survey"], cur["awaiting_comment"], cur.get("verdict", "wrong"), heard)
        say(chat_id=uid, text=f"записано з голосу:\n«{heard[:600]}»")
        show(uid, cur["survey"], None)
        return
    if cur.get("awaiting_comment") is not None and not text.startswith("/"):
        record(cur["survey"], cur["awaiting_comment"], cur.get("verdict", "wrong"), text)
        show(uid, cur["survey"], None)     # new message at the BOTTOM — see the comment on show()
        return
    if text == "/skip" and cur.get("awaiting_comment") is not None:
        record(cur["survey"], cur["awaiting_comment"], cur.get("verdict", "wrong"), None)
        show(uid, cur["survey"], None)
        return
    if not text:
        # A stray sticker, photo or a voice sent outside the comment step used to land here (empty
        # text) and bounce him back to the list, losing his place in the flow for no reason.
        if voice:
            say(chat_id=uid, text="Голосове приймаю тільки там, де я прошу коментар.")
        return
    if text in ("/start", "/menu", "/list"):
        menu(uid)
        return
    if text == "/next" and cur.get("survey"):
        show(uid, cur["survey"], None)
        return
    menu(uid, "Команди: /start - список наборів, /next - наступне питання.")


def main() -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    ANSWERS.mkdir(parents=True, exist_ok=True)
    me = call("getMe")
    print(f"бот: {me.get('result', {}).get('username')}", file=sys.stderr, flush=True)
    offset = None
    while True:
        r = call("getUpdates", _timeout=35, _retry=False, offset=offset, timeout=25,
                 allowed_updates=["message", "callback_query"])
        if not r.get("ok"):
            # A long poll that simply ran out of time is the NORMAL case, not a failure: say
            # nothing and go straight back to waiting, or the log fills and every quiet minute
            # costs an extra 5 seconds of latency.
            err = str(r.get("error", ""))
            if "409" in err or "terminated by other" in err.lower():
                print("!!! 409: хтось іще опитує цей токен. Оновлення діляться навпіл - "
                      "саме так виглядає «іноді не реагує». Зупини другий процес.",
                      file=sys.stderr, flush=True)
                time.sleep(5)
                continue
            if "timed out" not in err.lower():
                print(f"getUpdates: {r.get('error')}", file=sys.stderr, flush=True)
                time.sleep(5)
            continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            t_start = time.monotonic()
            try:
                if "callback_query" in u:
                    handle_callback(u["callback_query"])
                elif "message" in u:
                    handle_message(u["message"])
            except Exception as e:                            # noqa: BLE001
                print(f"update {u['update_id']}: {e}", file=sys.stderr, flush=True)
            dt = time.monotonic() - t_start
            if dt > 1.5:
                print(f"update {u['update_id']}: оброблено за {dt:.1f}s", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
