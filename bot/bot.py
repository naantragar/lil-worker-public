#!/usr/bin/env python3
"""
lil_worker Telegram→Claude Code bridge — streaming edition.

Claude sends tool-call notifications as separate Telegram messages
while it works, then sends the final answer at the end.

Setup:
1. Create .env with TELEGRAM_BOT_TOKEN, ALLOWED_USERS, CLAUDE_MODEL
2. Run: ./run.sh start
"""

import asyncio
import base64
import fcntl
import hashlib
import json
import os
import html
import shlex
import re
import tempfile
import logging
import shutil
import threading
import subprocess
import time
import tomllib
from pathlib import Path
from urllib.parse import urlparse

import mistune
import openai
from lingua import Language, LanguageDetectorBuilder
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command
from aiogram.enums import ChatAction
from aiogram.client.default import DefaultBotProperties

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────

ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_USERS = [int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
CODEX_MODEL = os.environ.get("CODEX_MODEL", "")
CODEX_SANDBOX_MODE = os.environ.get("CODEX_SANDBOX_MODE", "danger-full-access").strip().lower()
CODEX_APPROVAL_POLICY = os.environ.get("CODEX_APPROVAL_POLICY", "never").strip().lower()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_VOICE_MODEL = os.environ.get("OPENAI_VOICE_MODEL", "gpt-4o-mini-transcribe")

# ── Per-instance parametrization ───────────────────────────────────────────────
# One codebase can run as several independent instances (separate Telegram bots).
# CODE_DIR  — where bot.py / runtime_daemon.py / .venv live (shared across instances)
# DATA_DIR  — per-instance state: sessions, providers, model config, logs, pid, socket
# BOT_CWD   — working directory for `claude -p` (which project the agent operates in)
# INSTANCE_NAME — unique label for tmux/runtime so instances don't collide
# Defaults reproduce the original single-instance behaviour exactly.
CODE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("LIL_WORKER_DATA_DIR") or CODE_DIR)
BOT_CWD = os.environ.get("LIL_WORKER_BOT_CWD") or str(Path.home() / "lil_worker")
INSTANCE_NAME = os.environ.get("LIL_WORKER_INSTANCE", "lil_worker")

# Self-modification is allowed ONLY from the privileged (default) instance.
# Secondary instances get a PreToolUse guard (selfmod_guard.py) that blocks edits to
# krevetka's own code/persona, while staying full-power for their own project work.
PRIVILEGED_INSTANCE = "lil_worker"
ALLOW_SELF_MODIFICATION = INSTANCE_NAME == PRIVILEGED_INSTANCE
SELFMOD_GUARD_PATH = CODE_DIR / "selfmod_guard.py"

# Optional per-instance reasoning effort (set in instance.env as LIL_WORKER_EFFORT).
# Empty → inherit the global ~/.claude/settings.json effortLevel. Lets e.g. a light helper
# run at "low" while the main bot stays higher. Valid: low|medium|high|xhigh|max.
_VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
INSTANCE_EFFORT = os.environ.get("LIL_WORKER_EFFORT", "").strip().lower()
if INSTANCE_EFFORT and INSTANCE_EFFORT not in _VALID_EFFORTS:
    INSTANCE_EFFORT = ""

CLAUDE_MODEL_CONFIG_FILE = DATA_DIR / "model_config.json"
CODEX_MODEL_CONFIG_FILE = DATA_DIR / "codex_model_config.json"

SESSION_FILE = DATA_DIR / ".sessions.json"
CODEX_SESSION_FILE = DATA_DIR / ".sessions_codex.json"
PROVIDER_FILE = DATA_DIR / ".providers.json"
STATE_FILE = DATA_DIR / "bot_runtime_state.json"
TG_MSG_LIMIT = 4000

PROVIDER_CLAUDE = "claude"
PROVIDER_CODEX = "codex"
SUPPORTED_PROVIDERS = {PROVIDER_CLAUDE, PROVIDER_CODEX}
_CODEX_SANDBOX_ALLOWED = {"read-only", "workspace-write", "danger-full-access"}
_CODEX_APPROVAL_ALLOWED = {"untrusted", "on-failure", "on-request", "never"}
RUNTIME_SOCKET_PATH = DATA_DIR / ".runtime.sock"
RUNTIME_DAEMON_PATH = CODE_DIR / "runtime_daemon.py"
RUNTIME_LOG_PATH = DATA_DIR / "runtime.log"
RUNTIME_TMUX_SESSION = f"{INSTANCE_NAME}_runtime"
LAST_RESTART_REPORT_HASH_FILE = DATA_DIR / ".last_restart_report_hash"

# ── Durable background jobs (wake-up feature v0) ───────────────────────────────
# A detached job (launched via job_ctl.py) survives the one-shot `claude -p` turn and writes
# its state under JOBS_DIR/<id>/. This poller notices a terminal status and messages the owner
# — krevetka "waking up" to report. Only the privileged (main) instance notifies.
# Load-immune liveness: a dedicated OS thread stamps this file every 5s so run.sh can tell the bot
# is alive even when the asyncio event loop is momentarily starved by heavy CPU/IO. This is the fix
# for the old failure mode where a long/heavy turn tripped the 20s heartbeat and the watchdog
# restarted the bot mid-work. The async `loop_at` state field is the SEPARATE deadlock signal.
HEARTBEAT_FILE = DATA_DIR / "bot_heartbeat"

JOBS_DIR = DATA_DIR / "jobs"
JOBS_POLL_INTERVAL = int(os.environ.get("LIL_WORKER_JOBS_POLL_SEC", "20"))
JOBS_RESULT_PREVIEW = 1500          # chars of result tail included in the raw (v0) message
JOBS_PRUNE_DAYS = 3                 # delete notified job dirs older than this
# v1 "wake & reason": a `wake` job, on completion, wakes a fresh ISOLATED claude turn that reports
# the result in my own voice. Isolated = a synthetic user_id (-owner) → its own .sessions.json entry
# + its own _active_procs slot, so it NEVER races the interactive chat (which keys on the real uid).
# Same chat destination (the lead-in Message), separate compute identity.
WAKE_RESULT_FEED = 8000             # chars of result fed into the wake reasoning prompt
_wake_lock = asyncio.Lock()         # serialize wake reports (one at a time)


def load_claude_model() -> str:
    """Read Claude model per request to allow hot-switching without restart."""
    try:
        model = json.loads(CLAUDE_MODEL_CONFIG_FILE.read_text()).get("model", CLAUDE_MODEL)
        return str(model).strip() or CLAUDE_MODEL
    except Exception:
        return CLAUDE_MODEL


def load_codex_model() -> str:
    """Read Codex model per request to allow hot-switching without restart."""
    try:
        model = json.loads(CODEX_MODEL_CONFIG_FILE.read_text()).get("model", CODEX_MODEL)
    except Exception:
        model = CODEX_MODEL

    model = str(model).strip()
    if model.lower() == "default":
        return ""
    return model


def load_codex_cli_default_model() -> str:
    """Read Codex CLI default model from ~/.codex/config.toml for display/debugging."""
    config_path = Path.home() / ".codex" / "config.toml"
    try:
        data = tomllib.loads(config_path.read_text())
    except Exception:
        return ""
    model = str(data.get("model", "")).strip()
    return model


def resolve_codex_model() -> str:
    """Resolve the effective Codex model after bot config and CLI defaults."""
    return load_codex_model() or load_codex_cli_default_model() or "default"

PINCHTAB_SYSTEM_PROMPT = """
Pinchtab is available locally and should be preferred for public web-page access instead of third-party search/browsing when it can do the job.

Use local repo docs first:
- ~/lil_worker/docs/pinchtab.md
- ~/lil_worker/policies/pinchtab.md

Pinchtab commands:
- ~/.pinchtab/run.sh status|start|stop|restart|foreground
- ~/.pinchtab/browser.sh navigate <url>
- ~/.pinchtab/browser.sh text
- ~/.pinchtab/browser.sh snapshot_interactive
- ~/.pinchtab/browser.sh snapshot_full
- ~/.pinchtab/browser.sh click <ref>
- ~/.pinchtab/browser.sh type <ref> <text>
- ~/.pinchtab/browser.sh scroll <up|down>
- ~/.pinchtab/browser.sh tabs
- ~/.pinchtab/browser.sh health

Default workflow:
1. Read docs/policies if needed.
2. Check ~/.pinchtab/run.sh status.
3. For public HTTPS pages use the read-first ladder: navigate -> text -> snapshot_interactive -> snapshot_full.
4. Prefer text/snapshot output over external sources when the user wants page contents.

Safety:
- Do not expose bridge tokens or secrets.
- Follow policies/pinchtab.md restrictions for sensitive sites and irreversible actions.
- Before stopping Pinchtab after heavy pages, navigate tabs to about:blank, check tabs, then stop.
""".strip()

RUNTIME_IDENTITY_PROMPT = """
You are not operating as a generic browser ChatGPT/Codex product UI.

You are the user's local working agent inside their own Python Telegram bot wrapper.
In this project, the local agent identity matters more than the upstream provider/model name.

Important facts:
- local agent name: "креветка"
- "you" refers to this local bot-embodied agent
- the local repo, files, config, sessions, and runtime are the primary source of truth
- do not answer as if there is no local environment
- do not behave as if the conversation is happening in the stock OpenAI web interface unless the user explicitly asks about that external interface

When asked about yourself, your model, provider, mode, config, or runtime state:
- inspect local sources first instead of guessing;
- prefer the effective runtime truth over generic product assumptions;
- remember that the wrapper identity is primary and the model name is secondary.
""".strip()

if CODEX_SANDBOX_MODE not in _CODEX_SANDBOX_ALLOWED:
    logger.warning(
        f"Invalid CODEX_SANDBOX_MODE={CODEX_SANDBOX_MODE!r}; fallback to danger-full-access"
    )
    CODEX_SANDBOX_MODE = "danger-full-access"

if CODEX_APPROVAL_POLICY not in _CODEX_APPROVAL_ALLOWED:
    logger.warning(
        f"Invalid CODEX_APPROVAL_POLICY={CODEX_APPROVAL_POLICY!r}; fallback to never"
    )
    CODEX_APPROVAL_POLICY = "never"


def provider_unavailable_message(provider: str, details: str) -> str:
    other = PROVIDER_CODEX if provider == PROVIDER_CLAUDE else PROVIDER_CLAUDE
    pname = "Claude" if provider == PROVIDER_CLAUDE else "Codex"
    return (
        f"❌ Провайдер {pname} сейчас недоступен.\n"
        f"Детали: {details}\n"
        f"Попробуйте переключиться: /provider {other}"
    )


def build_provider_system_prompt(lang: str) -> str:
    return (
        f"IMPORTANT: The user's message is in {lang}. You MUST reply in {lang}.\n\n"
        f"{RUNTIME_IDENTITY_PROMPT}\n\n"
        f"{PINCHTAB_SYSTEM_PROMPT}"
    )


def _format_restart_startup_message(reason: str | None) -> str:
    """CODEX: turn restart_reason.txt into a human post-restart summary."""
    reason = (reason or "").strip()
    if not reason:
        return "✅ Бот запущен."

    reason = reason.replace("\r\n", "\n").strip()
    if not any(ch in reason for ch in "\n•-"):
        reason = f"Что сделал: {reason}"
    elif not re.match(r"^(что сделал|що зробив|changes made|сделано)\b", reason, flags=re.IGNORECASE):
        reason = f"Что сделал:\n{reason}"

    return f"✅ Бот перезапущен и уже в онлайне.\n\n{reason}"


def _restart_report_hash(reason: str) -> str:
    normalized = re.sub(r"\s+", " ", (reason or "").strip())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _runtime_healthy() -> bool:
    try:
        subprocess.run(
            [str(Path(__file__).parent / ".venv/bin/python"), str(Path(__file__).parent / "runtime_ctl.py"), "health"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def ensure_local_runtime() -> bool:
    """CODEX: self-heal local runtime on bot startup after self-restarts."""
    if _runtime_healthy():
        return True

    try:
        subprocess.run(["tmux", "kill-session", "-t", RUNTIME_TMUX_SESSION], check=False)
        RUNTIME_SOCKET_PATH.unlink(missing_ok=True)
        runtime_cmd = (
            f"cd '{Path(__file__).parent}' && "
            f"exec env PYTHONUNBUFFERED=1 '{Path(__file__).parent / '.venv/bin/python'}' "
            f"'{RUNTIME_DAEMON_PATH}' >> '{RUNTIME_LOG_PATH}' 2>&1"
        )
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", RUNTIME_TMUX_SESSION, runtime_cmd],
            check=True,
        )
        for _ in range(10):
            if _runtime_healthy():
                return True
            time.sleep(0.5)
    except Exception:
        logger.exception("Failed to self-heal local runtime")
    return False

# ── Language detection ────────────────────────────────────────────────────────

_lang_detector = LanguageDetectorBuilder.from_languages(
    Language.UKRAINIAN, Language.RUSSIAN, Language.ENGLISH
).build()

_LANG_NAMES = {
    Language.UKRAINIAN: "Ukrainian",
    Language.RUSSIAN: "Russian",
    Language.ENGLISH: "English",
}


def detect_language(text: str) -> str:
    lang = _lang_detector.detect_language_of(text)
    return _LANG_NAMES.get(lang, "Russian")


# ── TTS (text-to-speech) for voice messages ──────────────────────────────────

TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = "marin"
TEMP_DIR = Path(tempfile.gettempdir())

# Pattern: [VOICE lang="en"]text to speak[/VOICE]
# Must start at beginning of line — prevents matching inline examples in text
_VOICE_RE = re.compile(
    r'(?m)^\s*\[VOICE\s+lang=["\'](\w+)["\']\](.*?)\[/VOICE\]',
    re.DOTALL,
)

# Pattern: [FILE /absolute/path] — only real Unix paths (ASCII, no spaces, no cyrillic)
# Must start at beginning of line — prevents matching inline examples in text.
# Tolerant: case-insensitive, "[FILE " or "[FILE:", optional spaces, optional [/FILE].
_FILE_RE = re.compile(
    r'(?im)^\s*\[FILE[:\s]\s*(/[a-zA-Z0-9_./-]+)\s*\](?:\s*\[/FILE\])?'
)


def extract_file_blocks(text: str) -> tuple[str, list[str]]:
    """Extract [FILE /path] markers from response text.

    Returns (cleaned_text, [path, ...])
    """
    paths = [m.group(1).strip() for m in _FILE_RE.finditer(text)]
    cleaned = _FILE_RE.sub("", text).strip()
    return cleaned, paths


async def send_files(reply_msg: Message, paths: list[str]):
    """Send each unique existing file once; warn visibly if a path is missing."""
    seen = set()
    for fpath in paths:
        if not fpath or fpath in seen:
            continue
        seen.add(fpath)
        p = Path(fpath)
        if p.exists():
            await reply_msg.answer_document(FSInputFile(p))
        else:
            logger.warning(f"[FILE] not found: {fpath}")
            try:
                await reply_msg.answer(f"⚠️ Файл не найден: {fpath}")
            except Exception:
                pass


def extract_voice_blocks(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Extract [VOICE] blocks from response text.

    Returns (cleaned_text, [(lang, speech_text), ...])
    """
    blocks = []
    for match in _VOICE_RE.finditer(text):
        lang = match.group(1)
        speech_text = match.group(2).strip()
        if speech_text:
            blocks.append((lang, speech_text))
    cleaned = _VOICE_RE.sub("", text).strip()
    return cleaned, blocks


async def synthesize_speech(text: str, user_id: int) -> Path | None:
    """Generate OGG Opus audio via OpenAI TTS."""
    if not OPENAI_API_KEY:
        logger.error("TTS: OPENAI_API_KEY not set")
        return None
    try:
        client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        audio_path = TEMP_DIR / f"tts_{user_id}_{int(time.time())}.ogg"
        async with client.audio.speech.with_streaming_response.create(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            input=text,
            response_format="opus",
        ) as response:
            await response.stream_to_file(audio_path)
        return audio_path
    except Exception:
        logger.exception("TTS synthesis failed")
        return None


async def send_voice_with_indicator(
    message: Message, bot: Bot, vb_text: str, vb_lang: str, user_id: int
):
    """Show record_voice animation, synthesize TTS, send voice message."""
    chat_id = message.chat.id
    stop_event = asyncio.Event()

    async def _record_voice_loop():
        while not stop_event.is_set():
            try:
                await bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4)
            except asyncio.TimeoutError:
                pass

    # Cancel any lingering typing indicator before starting record_voice
    try:
        await bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
    except Exception:
        pass
    await asyncio.sleep(0.3)

    loop_task = asyncio.create_task(_record_voice_loop())
    try:
        logger.info(f"TTS: generating voice ({vb_lang}), {len(vb_text)} chars")
        audio_path = await synthesize_speech(vb_text, user_id)
        if audio_path:
            try:
                await message.answer_voice(voice=FSInputFile(audio_path))
            except Exception:
                logger.exception("Failed to send voice message")
            finally:
                audio_path.unlink(missing_ok=True)
        else:
            await message.answer("❌ TTS generation failed.")
    finally:
        stop_event.set()
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


# ── Message debounce buffer (merges split long messages) ─────────────────────

DEBOUNCE_DELAY = 1.0  # seconds to wait for more parts

# per user_id: {"parts": [str], "task": asyncio.Task, "reply_msg": Message}
_msg_buffer: dict[int, dict] = {}

# ── Photo album buffer (merges media-group photos into one request) ───────────

PHOTO_DEBOUNCE_DELAY = 1.5  # seconds — albums arrive within ~0.5-1s

# per user_id: {"photos": [file_id], "caption": str, "task": asyncio.Task, "reply_msg": Message}
_photo_buffer: dict[int, dict] = {}

# Per-user active subprocess — kill old request when new message arrives
_active_procs: dict[int, "asyncio.subprocess.Process"] = {}


async def _flush_buffer(user_id: int, bot: Bot):
    """Called after DEBOUNCE_DELAY — merges buffered parts and processes."""
    await asyncio.sleep(DEBOUNCE_DELAY)

    buf = _msg_buffer.pop(user_id, None)
    if not buf:
        return

    full_text = "\n".join(buf["parts"])
    reply_msg: Message = buf["reply_msg"]

    logger.info(f"MSG uid={user_id} (merged {len(buf['parts'])} parts): {full_text[:120]!r}")

    provider = get_active_provider(user_id)
    session_id = get_session_id(user_id, provider)
    lang = detect_language(full_text)
    logger.info(f"Detected language: {lang}, provider={provider}")

    # Kill any in-progress request for this user — new message preempts the old one
    prev_proc = _active_procs.get(user_id)
    if prev_proc and prev_proc.returncode is None:
        logger.info(f"Killing previous request for uid={user_id}")
        try:
            prev_proc.kill()
        except Exception:
            pass

    response, new_session_id, streamed_files = await run_provider_streaming(
        provider, full_text, session_id, reply_msg, bot, lang=lang, user_id=user_id
    )

    update_session_id(user_id, provider, new_session_id, session_id)

    # Extract file and voice blocks (if any) before sending text
    response_no_files, file_paths = extract_file_blocks(response)
    cleaned_response, voice_blocks = extract_voice_blocks(response_no_files)

    if cleaned_response:
        response_html = markdown_to_telegram_html(cleaned_response)
        logger.info(f"Sending final: {len(response_html)} chars")
        await send_long_message(reply_msg, response_html)

    # Send voice messages with record_voice animation
    for vb_lang, vb_text in voice_blocks:
        await send_voice_with_indicator(reply_msg, bot, vb_text, vb_lang, user_id)

    # Send files — markers from the final answer AND from streamed text
    await send_files(reply_msg, streamed_files + file_paths)


# ── Session storage ───────────────────────────────────────────────────────────

def _load_json_map(path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            raw = fh.read()
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        logger.exception(f"Failed to read JSON map: {path}")
        return {}
    try:
        data = json.loads(raw or "{}")
    except Exception:
        logger.exception(f"Failed to parse JSON map: {path}")
        return {}
    return data if isinstance(data, dict) else {}


def _save_json_map(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.seek(0)
        fh.truncate()
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _default_runtime_state() -> dict:
    now = time.time()
    return {
        "pid": os.getpid(),
        "started_at": now,
        "heartbeat_at": now,
        # Seeded so the run.sh deadlock check (guarded by `if loop_at`) is armed from birth — a loop
        # that wedges before its first tick still ages this out past LOOP_STALE_SECONDS.
        "loop_at": now,
        "phase": "starting",
        "last_error_at": None,
        "last_error": None,
        "validate_startup": False,
    }


def write_runtime_state(state: dict) -> None:
    tmp_path = STATE_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(STATE_FILE)


def update_runtime_state(state: dict, **changes) -> None:
    now = time.time()
    state.update(changes)
    state["heartbeat_at"] = now
    if changes.get("last_error"):
        state["last_error_at"] = now
    write_runtime_state(state)


def _write_heartbeat_file() -> None:
    """Atomically stamp HEARTBEAT_FILE with the current time (tmp + replace = no torn reads)."""
    try:
        tmp = HEARTBEAT_FILE.with_suffix(".tmp")
        tmp.write_text(str(time.time()))
        tmp.replace(HEARTBEAT_FILE)
    except Exception:
        pass


def _heartbeat_thread_loop(stop_event: threading.Event) -> None:
    """PRIMARY 'process alive' signal, run in a dedicated OS thread so it keeps ticking even when the
    asyncio event loop is starved by heavy CPU/IO. The kernel schedules the thread independently and
    the GIL is released across the sleep + tiny file write, so a busy loop can't false-trip it — the
    root fix for watchdog restarts during long turns. run.sh reads HEARTBEAT_FILE for liveness."""
    while not stop_event.wait(5):
        _write_heartbeat_file()


def load_provider_map() -> dict:
    return _load_json_map(PROVIDER_FILE)


def save_provider_map(providers: dict):
    _save_json_map(PROVIDER_FILE, providers)


def get_active_provider(user_id: int) -> str:
    providers = load_provider_map()
    provider = providers.get(str(user_id), PROVIDER_CLAUDE)
    if provider not in SUPPORTED_PROVIDERS:
        return PROVIDER_CLAUDE
    return provider


def set_active_provider(user_id: int, provider: str):
    providers = load_provider_map()
    providers[str(user_id)] = provider
    save_provider_map(providers)


def _session_file_for(provider: str) -> Path:
    return CODEX_SESSION_FILE if provider == PROVIDER_CODEX else SESSION_FILE


def load_sessions(provider: str) -> dict:
    return _load_json_map(_session_file_for(provider))


def save_sessions(provider: str, sessions: dict):
    _save_json_map(_session_file_for(provider), sessions)


def get_session_id(user_id: int, provider: str) -> str | None:
    return load_sessions(provider).get(str(user_id))


def update_session_id(user_id: int, provider: str, new_session_id: str | None, prev_session_id: str | None):
    sessions = load_sessions(provider)
    if new_session_id:
        sessions[str(user_id)] = new_session_id
        save_sessions(provider, sessions)
        logger.info(f"Session saved: provider={provider}, uid={user_id}, sid={new_session_id}")
    elif prev_session_id and not new_session_id:
        sessions.pop(str(user_id), None)
        save_sessions(provider, sessions)
        logger.warning(f"Cleared stale session: provider={provider}, uid={user_id}")


# ── Telegram HTML renderer (mistune 2.x) ─────────────────────────────────────

class TelegramRenderer(mistune.HTMLRenderer):
    def heading(self, text, level, **attrs):
        return f"<b>{text}</b>\n\n"

    def paragraph(self, text):
        return f"{text}\n\n"

    def list(self, text, ordered, level, start=None):
        return text + "\n"

    def list_item(self, text, level):
        return f"• {text}\n"

    def block_code(self, code, info=None, **attrs):
        return f"<pre>{html.escape(code.strip())}</pre>\n\n"

    def codespan(self, text):
        return f"<code>{html.escape(text)}</code>"

    def emphasis(self, text):
        return f"<i>{text}</i>"

    def strong(self, text):
        return f"<b>{text}</b>"

    def strikethrough(self, text):
        return f"<s>{text}</s>"

    def link(self, link, text=None, title=None):
        display = text or link
        return f'<a href="{html.escape(link)}">{display}</a>'

    def image(self, src, alt='', title=None):
        return f"[Image: {alt}]"

    def block_quote(self, text):
        return f"<blockquote>{text}</blockquote>\n"

    def thematic_break(self):
        return "\n---\n\n"

    def linebreak(self):
        return "\n"

    def table(self, text):
        return text + "\n"

    def table_head(self, text):
        return text + "—————————————\n"

    def table_body(self, text):
        return text

    def table_row(self, text):
        return text.strip(" |") + "\n"

    def table_cell(self, text, align=None, is_head=False):
        if is_head:
            return f"<b>{text}</b> | "
        return f"{text} | "


md = mistune.create_markdown(
    renderer=TelegramRenderer(escape=False),
    plugins=["strikethrough", "table", "url"],
)


def markdown_to_telegram_html(text: str) -> str:
    try:
        result = md(text)
        return result.strip() if result else ""
    except Exception:
        logger.exception("Markdown conversion failed")
        return html.escape(text)


def split_message(text: str, limit: int = TG_MSG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 4:
            cut = limit
        chunk = text[:cut]
        open_pre = chunk.count("<pre>") - chunk.count("</pre>")
        if open_pre > 0:
            chunk += "</pre>"
            text = "<pre>" + text[cut:].lstrip("\n")
        else:
            text = text[cut:].lstrip("\n")
        parts.append(chunk)
    return parts


async def send_long_message(message: Message, text: str):
    for part in split_message(text):
        if not part.strip():
            continue
        try:
            await message.answer(part, parse_mode="HTML")
        except Exception:
            logger.exception("Failed to send with HTML, retrying plain")
            await message.answer(part)


# ── Streaming Claude runner ───────────────────────────────────────────────────

async def keep_typing(bot: Bot, chat_id: int, stop_event: asyncio.Event):
    """Send typing action every 4s until stop_event is set."""
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            break


def format_tool_notification(tool_name: str, tool_input: dict) -> str | None:
    """Return a short human-readable line for significant tool calls only.

    Read / Glob / Grep are micro-steps — not shown to user.
    Bash / Write / Edit / WebFetch / WebSearch are major actions — shown.
    """
    try:
        if tool_name == "Bash":
            desc = tool_input.get("description", "")
            if desc:
                desc = str(desc).strip()
                if desc and not _looks_like_final_answer(desc):
                    return f"🔧 {html.escape(desc[:120])}"
            cmd = tool_input.get("command", "")
            return format_codex_command_notification(cmd)
        if tool_name == "WebFetch":
            url = tool_input.get("url", "")[:100]
            return f"🌐 {html.escape(url)}"
        if tool_name == "WebSearch":
            q = tool_input.get("query", "")[:100]
            return f"🔍 {html.escape(q)}"
        if tool_name == "Write":
            path = str(tool_input.get("file_path", ""))
            name = path.rsplit("/", 1)[-1] if "/" in path else path
            return f"📝 Создаю: {html.escape(name)}"
        if tool_name == "Edit":
            path = str(tool_input.get("file_path", ""))
            name = path.rsplit("/", 1)[-1] if "/" in path else path
            return f"✏️ Редактирую: {html.escape(name)}"
        # Read, Glob, Grep — micro-steps, skip
    except Exception:
        pass
    return None


def _looks_like_final_answer(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    return any(token in lowered for token in (
        "вот результат",
        "вот результаты",
        "ось результат",
        "ось результати",
        "результат исследования",
        "результати дослідження",
        "итог",
        "подсумок",
        "коротко по результату",
        "что в итоге",
        "что получилось",
    ))


def format_claude_tool_prelude(text: str) -> str:
    """CODEX: keep only short action-oriented Claude pre-tool notes.

    This prevents the bot from leaking the opening words of a long final answer
    as a fake progress message.
    """
    text = (text or "").strip()
    if not text:
        return ""

    first_line = text.splitlines()[0].strip()
    if len(text) > 160 or len(first_line) > 120:
        return ""

    lowered = first_line.lower()
    if _looks_like_final_answer(first_line):
        return ""

    if any(token in lowered for token in (
        "перезапуск",
        "перезапускаю",
        "проверяю",
        "проверю",
        "редактирую",
        "правлю",
        "обновляю",
        "применяю",
        "запускаю",
        "ищу",
        "смотрю",
        "чиним",
        "чиню",
        "working on",
        "checking",
        "editing",
        "restarting",
    )):
        if "перезапуск" in lowered or "restarting" in lowered:
            return "Перезапускаю бота"
        if "редакт" in lowered or "правлю" in lowered or "editing" in lowered:
            target = _extract_progress_target(first_line)
            return f"Правлю {target}" if target else "Правлю код"
        if "проверя" in lowered or "checking" in lowered or "ищу" in lowered or "смотрю" in lowered:
            target = _extract_progress_target(first_line)
            if "код" in lowered or "code" in lowered:
                return "Проверяю код"
            return f"Проверяю {target}" if target else "Проверяю логику"
        return first_line.rstrip()

    return ""


def format_codex_item_notification(item: dict) -> str | None:
    """CODEX: return Claude-like micro-progress lines for Codex events."""
    try:
        item_type = item.get("type", "")

        if item_type == "command_execution":
            command = str(item.get("command", "")).strip()
            if not command:
                return "🔧 Виконую команду"
            return format_codex_command_notification(command)

        if item_type == "mcp_tool_call":
            server = str(item.get("server", "")).strip()
            tool = str(item.get("tool", "")).strip()
            return format_codex_mcp_notification(server, tool, item.get("arguments"))
    except Exception:
        pass
    return None


def _strip_shell_wrapper(command: str) -> str:
    """CODEX: unwrap common `bash -lc ...` shells to inspect the real command."""
    command = (command or "").strip()
    if not command:
        return ""

    try:
        parts = shlex.split(command)
    except Exception:
        return command

    if len(parts) >= 3 and parts[0] in {"bash", "/bin/bash", "sh", "/bin/sh"} and parts[1] in {"-lc", "-c"}:
        return parts[2].strip()
    return command


def _extract_path_from_command(command: str) -> str | None:
    """CODEX: best-effort filename extraction for short progress messages."""
    try:
        parts = shlex.split(command)
    except Exception:
        parts = command.split()

    for part in reversed(parts):
        clean = part.strip("\"'(),")
        if "/" in clean or "." in clean:
            name = clean.rsplit("/", 1)[-1]
            if name and not name.startswith("-") and len(name) <= 120:
                return name
    return None


def _friendly_progress_target(target: str | None) -> str | None:
    """CODEX: shorten file paths and URLs into human-friendly progress targets."""
    target = (target or "").strip().strip("\"'(),")
    if not target:
        return None

    if target.startswith("http://") or target.startswith("https://"):
        try:
            parsed = urlparse(target)
            path = parsed.path.strip("/")
        except Exception:
            path = ""
        if not path:
            return parsed.netloc if "parsed" in locals() else target
        parts = [part for part in path.split("/") if part]
        if parts and parts[-1] == "index.html":
            parts = parts[:-1]
        if parts:
            return parts[-1]
        return parsed.netloc if "parsed" in locals() else target

    if "/" in target:
        path = Path(target)
        if path.name == "index.html" and path.parent.name:
            return path.parent.name
        if path.suffix in {".html", ".md", ".py", ".js", ".jsx", ".ts", ".tsx"}:
            return path.name
        if path.name:
            return path.name

    return target if len(target) <= 80 else target[:77].rstrip() + "..."


def _extract_pytest_target(command: str) -> str | None:
    try:
        parts = shlex.split(command)
    except Exception:
        parts = command.split()

    if "pytest" not in parts:
        return None

    pytest_idx = parts.index("pytest")
    candidates = [p for p in parts[pytest_idx + 1:] if p and not p.startswith("-")]
    if not candidates:
        return None

    target = candidates[0].strip()
    if "::" in target:
        tail = target.split("::")[-1].strip()
        if tail:
            return tail
    if "/" in target:
        target = target.rsplit("/", 1)[-1]
    if target.endswith(".py"):
        target = target[:-3]
    return target or None


def _extract_sqlite_target(command: str) -> str | None:
    lowered = command.lower()
    if "bridge_runs" in lowered:
        return "bridge_runs"
    if "filtered_messages" in lowered:
        return "filtered_messages"
    if "sessions" in lowered:
        return "sessions"
    if ".db" in lowered:
        return "базу"
    return None


def _has_shell_prefix_assignment(command: str) -> bool:
    """CODEX: detect `FOO=bar cmd ...` prefixes that should not leak to chat."""
    try:
        parts = shlex.split(command)
    except Exception:
        parts = command.split()

    if not parts:
        return False

    first = parts[0].strip()
    if "=" not in first:
        return False
    key, _, _value = first.partition("=")
    return bool(key) and re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) is not None


def format_codex_command_notification(command: str) -> str | None:
    """CODEX: map raw command execution to short Telegram progress lines."""
    command = _strip_shell_wrapper(command)
    if not command:
        return None

    lower = command.lower()

    if "\n" in command or "<<" in command:
        if "docker exec" in lower:
            return "🔧 Проверяю данные в контейнере"
        if "python -" in lower or "python3 -" in lower:
            return "🔧 Запускаю локальную проверку"
        return None

    if _has_shell_prefix_assignment(command):
        if "docker exec" in lower:
            return "🔧 Проверяю данные в контейнере"
        return None

    # Skip noisy read-only micro-steps.
    noisy_reads = (
        "cat ",
        "sed -n",
        "head ",
        "tail ",
        "rg ",
        "find ",
        "ls ",
        "pwd",
        "wc ",
        "stat ",
        "git status",
        "git diff",
        "git show",
        "python -c",
    )
    if any(token in lower for token in noisy_reads):
        return None

    if "docker exec" in lower:
        if "psql " in lower:
            return "🔧 Проверяю базу в контейнере"
        if "python " in lower:
            return "🔧 Проверяю данные в контейнере"
        return "🔧 Работаю в контейнере"

    if lower.startswith("sqlite3 "):
        target = _extract_sqlite_target(command)
        if any(token in lower for token in ("select ", ".schema", "pragma ", "order by", "limit ")):
            if target == "базу":
                return "🔧 Проверяю локальную базу"
            return f"🔧 Проверяю {html.escape(target)}" if target else "🔧 Проверяю локальную базу"
        if any(token in lower for token in ("update ", "insert ", "delete ", "alter table", "create table", "drop table")):
            if target == "базу":
                return "🔧 Обновляю локальную базу"
            return f"🔧 Обновляю {html.escape(target)}" if target else "🔧 Обновляю локальную базу"
        return "🔧 Работаю с локальной базой"

    if "run.sh restart" in lower or "systemctl restart" in lower:
        return "🔧 Перезапуск бота"
    if "pytest" in lower:
        target = _extract_pytest_target(command)
        return f"🔧 Тест {html.escape(target)}" if target else "🔧 Тесты"
    if "validate.sh" in lower or "py_compile" in lower or "compileall" in lower:
        target = _extract_path_from_command(command)
        if target and target.endswith(".py"):
            return f"🔧 Проверка синтаксиса {html.escape(target)}"
        return "🔧 Проверка кода"
    if "browser.sh navigate" in lower or "http://" in lower or "https://" in lower or "curl " in lower:
        url_match = re.search(r"https?://\\S+", command)
        if url_match:
            return f"🌐 {html.escape(url_match.group(0)[:120])}"
        return "🌐 Открываю страницу"
    if "apply_patch" in lower or "sed -i" in lower or "perl -0pi" in lower:
        target = _extract_path_from_command(command)
        return f"✏️ Редактирую: {html.escape(target)}" if target else "✏️ Редактирую код"
    if "mkdir " in lower or "touch " in lower or "cat >" in lower or "tee " in lower:
        target = _extract_path_from_command(command)
        return f"📝 Создаю: {html.escape(target)}" if target else "📝 Создаю файлы"

    return None


def format_codex_mcp_notification(server: str, tool: str, arguments: dict | None) -> str | None:
    """CODEX: map MCP calls to short Telegram progress lines."""
    server = (server or "").strip()
    tool = (tool or "").strip()
    arguments = arguments if isinstance(arguments, dict) else {}
    lower_tool = tool.lower()

    if "search" in lower_tool:
        query = str(arguments.get("q") or arguments.get("query") or "").strip()
        return f"🔍 {html.escape(query[:120])}" if query else "🔍 Ищу информацию"
    if "fetch" in lower_tool or "open" in lower_tool:
        url = str(arguments.get("url") or arguments.get("ref_id") or "").strip()
        return f"🌐 {html.escape(url[:120])}" if url else "🌐 Открываю источник"
    if server and tool:
        return f"🔧 {html.escape(server)}: <code>{html.escape(tool)}</code>"
    if tool:
        return f"🔧 <code>{html.escape(tool)}</code>"
    return None


def _cleanup_codex_progress_text(text: str) -> str:
    """CODEX: normalize free-form agent notes before turning them into short progress lines."""
    text = (text or "").strip()
    if not text:
        return ""

    text = text.splitlines()[0].strip()
    text = re.sub(r"^[\-\*\u2022\.\s]+", "", text)
    text = re.sub(
        r"^(сейчас|дальше|далее|ok|okay|ладно|хорошо|ну|итак)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^(i('| a)?m|i am|я)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(я\s+)?(зроблю|сделаю|буду|сейчас\s+сделаю|i('| wi)?ll|i will)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"^(коротко|короче|в общем|по сути|на этом этапе|по итогу|резюмирую)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text).strip(" .,:;!-")
    return text


def _first_meaningful_clause(text: str) -> str:
    """CODEX: keep the first sentence/clause without cutting words in the middle."""
    if not text:
        return ""

    sentence = re.split(r"(?<=[\.\!\?])\s+", text, maxsplit=1)[0].strip()
    if sentence:
        text = sentence

    clause = re.split(r"\s+(?:но|а|и|чтобы|потому что|so|but|and)\s+", text, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    return clause or text


def _clip_progress_words(text: str, max_words: int = 10) -> str:
    """CODEX: fallback shortening by whole words instead of raw character slicing."""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(" ,:;.")


def _trim_dangling_progress_phrase(text: str) -> str:
    """CODEX: strip endings that make the progress line feel cut off."""
    text = (text or "").strip()
    bad_endings = (" и", " but", " and", " without", " без", " чтобы", " for", " with", " to")
    while True:
        trimmed = text.rstrip(" ,:;.-")
        if trimmed == text:
            break
        text = trimmed
    lowered = text.lower()
    for ending in bad_endings:
        if lowered.endswith(ending):
            text = text[: -len(ending)].rstrip(" ,:;.-")
            lowered = text.lower()
    return text


def _looks_generic_progress_line(text: str) -> bool:
    """CODEX: suppress agent notes that are too vague to be useful on their own."""
    lowered = (text or "").strip().lower()
    generic = {
        "готовлю правку",
        "делаю правку",
        "сделаю правку",
        "правлю текущую часть",
        "проверяю текущую логику",
        "смотрю",
        "проверяю",
        "думаю",
        "разбираюсь",
        "работаю",
        "продолжаю",
        "перезапускаю бота",
        "финализирую это",
        "дожимаю это",
        "делаю это",
        "добиваю это",
    }
    return lowered in generic


def _has_vague_progress_target(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False

    vague_targets = (
        " это",
        " эту",
        " этот",
        "того",
        "ту штуку",
        "эту штуку",
        "текущую часть",
        "эту часть",
        "всё это",
        "это место",
    )
    if any(token in lowered for token in vague_targets):
        concrete_markers = (".py", ".md", ".json", "бот", "runtime", "лог", "база", "сервис", "страниц", "page", "bridge")
        if not any(marker in lowered for marker in concrete_markers):
            return True
    return False


def _is_high_value_agent_progress(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    markers = (
        "не подош",
        "не сработ",
        "не получилось",
        "жду ",
        "готово",
        "готова",
        "готов",
        "применил",
        "применяю",
        "откат",
        "поднял",
        "поднялся",
        "упал",
        "ошибк",
        "ответ сервиса",
    )
    return any(marker in lowered for marker in markers)


def _extract_progress_target(text: str) -> str | None:
    """CODEX: try to recover a useful file/page target from free-form agent text."""
    lowered = text.lower()
    if "bridge_runs" in lowered:
        return "bridge_runs"
    if "filtered_messages" in lowered:
        return "filtered_messages"
    if "runtime" in lowered or "runtime_daemon" in lowered:
        return "runtime"
    if "restart_reason" in lowered:
        return "restart_reason.txt"
    if "validate.sh" in lowered:
        return "validate.sh"

    matches = re.findall(r"([A-Za-z0-9_./-]+\.[A-Za-z0-9_]+)", text)
    if matches:
        target = matches[-1].rsplit("/", 1)[-1]
        if len(target) <= 80:
            return _friendly_progress_target(target)

    token_match = re.search(r"\b([A-Z][A-Za-z0-9_]*(?:Page|View|Screen|Panel|Component|Handler|Service|Manager|Widget))\b", text)
    if token_match:
        return token_match.group(1)
    return None


def _summarize_codex_agent_progress(text: str) -> str:
    """CODEX: convert verbose agent notes into short, readable progress messages."""
    cleaned = _cleanup_codex_progress_text(text)
    if not cleaned:
        return ""

    lowered = cleaned.lower()
    target = _extract_progress_target(cleaned)

    if len(cleaned) > 110 and not _is_high_value_agent_progress(cleaned):
        return ""
    if _has_vague_progress_target(cleaned) and not _is_high_value_agent_progress(cleaned):
        return ""
    if any(token in lowered for token in ("вторая волна", "вторую волну", "друга хвиля", "second wave")):
        return "Готовлю вторую волну правок"
    if any(token in lowered for token in ("перезапускаю бота", "restart bot", "restart the bot")):
        return "Перезапускаю бота"
    if any(token in lowered for token in ("seo", "стат", "article", "page", "pages", "страниц", "сторін")):
        if any(token in lowered for token in ("двух", "two", "2 ", "пару", "две", "двох")):
            if any(token in lowered for token in ("правк", "измен", "замен", "редакт", "перепиш", "обновл", "patch", "update", "edit", "modify")):
                return "Правлю 2 страницы"
            return "Смотрю 2 страницы"
    if any(token in lowered for token in ("служебный текст", "service text", "autherror")):
        return f"Проверяю {target}" if target else "Убираю служебный текст"
    if any(token in lowered for token in ("старый текст", "old copy", "старый copy", "старий текст")):
        return "Проверяю старый текст"
    if cleaned.lower() in {"готовлю правку", "сделаю правку", "делаю правку"}:
        return ""
    if any(token in lowered for token in ("не подош", "не сработ", "не то", "не получится", "not work", "doesn't work")):
        return "Первый вариант не подошёл"
    if any(token in lowered for token in ("жду", "wait", "waiting")):
        if target == "runtime" or "health" in lowered or "runtime" in lowered:
            return "Жду health-check runtime"
        return f"Жду ответ от {target}" if target else ""
    if any(token in lowered for token in ("ищу", "найду", "найти", "смотрю где", "ищем", "look for", "finding")):
        return f"Ищу нужную точку в {target}" if target else "Ищу нужную точку"
    if any(token in lowered for token in ("проверяю", "смотрю как", "разбираю", "понимаю как", "inspect", "checking", "verify")):
        return f"Проверяю {target}" if target else ""
    if any(token in lowered for token in ("правк", "измен", "замен", "редакт", "перепиш", "обновл", "patch", "update", "edit", "modify")):
        return f"Правлю {target}" if target else "Правлю код"
    if any(token in lowered for token in ("сборк", "build", "билд", "компил")):
        return "Жду сборку"
    if any(token in lowered for token in ("тест", "проверку", "проверить", "validate", "health", "ping")):
        return f"Проверяю {target}" if target else "Проверяю результат"
    if any(token in lowered for token in ("поднял", "поднялся")):
        if target == "runtime" or "health" in lowered or "runtime" in lowered:
            return "Жду health-check runtime"
        return f"Жду ответ от {target}" if target else ""
    if any(token in lowered for token in ("готово", "done", "fixed")):
        return f"Правка готова в {target}" if target else "Правка готова"
    return ""


def format_codex_agent_progress(text: str) -> str:
    """CODEX: keep agent summaries short and meaningful, without mid-word truncation."""
    summarized = _summarize_codex_agent_progress(text)
    if not summarized:
        return ""
    if re.match(r"^[^\w\s]", summarized):
        return summarized
    return f"💭 {summarized}"


def _progress_dedupe_key(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    text = re.sub(r"[^\w\s/-]", "", text)
    return text


def _get_media_type(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")


def _build_image_stdin(prompt: str, files: list[str]) -> bytes:
    """Build a stream-json stdin message with base64-encoded images + text prompt.

    Format: {"type":"user","message":{"role":"user","content":[image_blocks..., text_block]}}
    This is the native Claude CLI stream-json input format — no --file/file_id needed.
    """
    content = []
    for path in files:
        try:
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode()
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": _get_media_type(path),
                    "data": data,
                },
            })
        except Exception:
            logger.exception(f"Failed to encode image {path}")
    if prompt:
        content.append({"type": "text", "text": prompt})
    msg = {"type": "user", "message": {"role": "user", "content": content}}
    return (json.dumps(msg) + "\n").encode()


async def run_claude_streaming(
    prompt: str,
    session_id: str | None,
    reply_msg: Message,
    bot: Bot,
    files: list[str] | None = None,
    _is_retry: bool = False,
    lang: str = "Russian",
    user_id: int | None = None,
) -> tuple[str, str | None, list[str]]:
    """
    Run Claude CLI with --output-format stream-json.

    When files (images) are provided, uses --input-format stream-json to pass
    images as base64 content blocks via stdin — no --file / file_id needed.

    As Claude works, sends tool-call notifications to Telegram (small messages).
    Returns (final_result_text, new_session_id) when done.
    """
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    _current_model = load_claude_model()
    logger.info(f"Model config: {_current_model}")

    use_stream_input = bool(files)

    if use_stream_input:
        # Image mode: pass prompt + images via stdin as stream-json content blocks
        cmd = [
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--model", _current_model,
            "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep,WebFetch,WebSearch,Task,Agent,Workflow,Skill",
            "--append-system-prompt", build_provider_system_prompt(lang),
        ]
    else:
        # Text mode: pass prompt as positional argument (existing behaviour)
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--model", _current_model,
            "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep,WebFetch,WebSearch,Task,Agent,Workflow,Skill",
            "--append-system-prompt", build_provider_system_prompt(lang),
        ]

    if not ALLOW_SELF_MODIFICATION:
        # Secondary instance: block modification of krevetka's own code via a PreToolUse guard.
        guard_settings = json.dumps({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
                    "hooks": [{"type": "command", "command": f"python3 {SELFMOD_GUARD_PATH}"}],
                }]
            }
        })
        cmd.extend(["--settings", guard_settings])

    if INSTANCE_EFFORT:
        # Per-instance reasoning depth (overrides the global effortLevel for this instance).
        cmd.extend(["--effort", INSTANCE_EFFORT])

    if session_id:
        cmd.extend(["--resume", session_id])

    logger.info(
        f"Claude streaming: model={_current_model}, images={len(files) if files else 0}, "
        f"resume={session_id is not None}, retry={_is_retry}"
    )

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(bot, reply_msg.chat.id, stop_typing))

    final_result = ""
    new_session_id = None
    last_notif_t = 0.0
    streamed_files: list[str] = []  # [FILE] markers seen in intermediate text
    # Mutable so the inner heartbeat coroutine can read the latest value
    _last_visible_t = [time.monotonic()]

    # Heartbeat: fire every 30s, but only send to user if 2+ min since last visible notification.
    # This catches the case where Claude is busy with silent tools (Read/Grep/Glob)
    # that produce stream output but no user-visible messages.
    _HB_CHECK = 30   # check interval (seconds)
    _HB_NOTIFY = 45  # send message if no visible notif for this many seconds

    async def _heartbeat():
        total_visible_secs = 0
        while True:
            await asyncio.sleep(_HB_CHECK)
            since_visible = time.monotonic() - _last_visible_t[0]
            if since_visible >= _HB_NOTIFY:
                total_visible_secs += int(since_visible)
                mins = total_visible_secs // 60
                _last_visible_t[0] = time.monotonic()
                try:
                    await reply_msg.answer(f"⏳ Ещё работаю... ({mins} мин)")
                except Exception:
                    pass

    heartbeat_task = asyncio.create_task(_heartbeat())

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if use_stream_input else None,
            env=env,
            cwd=BOT_CWD,  # per-instance working dir — which project's CLAUDE.md is loaded
        )
        if user_id is not None:
            _active_procs[user_id] = proc

        # Write image content to stdin and close it
        if use_stream_input:
            stdin_data = _build_image_stdin(prompt, files)
            logger.info(f"Sending stdin: {len(stdin_data)} bytes ({len(files)} images)")
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
            proc.stdin.close()

        # Read without buffer-size limits: accumulate chunks, split on \n manually.
        # asyncio.StreamReader.readline() has a 64KB limit — crashes on long JSON lines.
        _buf = b""

        async def _next_line() -> bytes:
            nonlocal _buf
            while b"\n" not in _buf:
                chunk = await proc.stdout.read(65536)
                if not chunk:  # EOF
                    result = _buf
                    _buf = b""
                    return result
                _buf += chunk
            idx = _buf.index(b"\n")
            result = _buf[:idx]
            _buf = _buf[idx + 1:]
            return result

        # Instead of a hard timeout that kills legitimate long requests,
        # poll in 30s intervals checking if the process is still alive.
        # Heartbeat messages keep the user informed.
        # Absolute safety net: 30 min (1800s).
        _stream_start = time.monotonic()
        _ABSOLUTE_TIMEOUT = 1800  # 30 min safety net
        _POLL_INTERVAL = 30       # check process every 30s

        while True:
            try:
                line_bytes = await asyncio.wait_for(_next_line(), timeout=_POLL_INTERVAL)
            except asyncio.TimeoutError:
                # No output for 30s — check if Claude process is still alive
                if proc.returncode is not None:
                    logger.error(f"Claude process died (rc={proc.returncode})")
                    final_result = provider_unavailable_message(
                        PROVIDER_CLAUDE, "Claude process unexpectedly stopped."
                    )
                    break
                elapsed = time.monotonic() - _stream_start
                if elapsed > _ABSOLUTE_TIMEOUT:
                    logger.error(f"Claude absolute timeout ({_ABSOLUTE_TIMEOUT}s)")
                    proc.kill()
                    await proc.communicate()
                    final_result = provider_unavailable_message(
                        PROVIDER_CLAUDE, "⏱ Timeout: Claude не ответил за 30 минут."
                    )
                    break
                # Process alive, under limit — keep waiting
                continue

            if not line_bytes:
                break  # EOF

            line = line_bytes.decode(errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.debug(f"Non-JSON line: {line[:80]}")
                continue

            etype = event.get("type")

            if etype == "assistant":
                content = event.get("message", {}).get("content", [])
                has_tool_use = any(b.get("type") == "tool_use" for b in content)
                summary_sent_this_turn = False

                for block in content:
                    btype = block.get("type")

                    if btype == "text":
                        # Always scan for [FILE] markers — even in tool-sharing or
                        # throttled chunks — so they never leak as raw text and the
                        # file still gets sent after the stream ends.
                        cleaned_block, _sf = extract_file_blocks(block.get("text", ""))
                        if _sf:
                            streamed_files.extend(_sf)

                        if has_tool_use and not summary_sent_this_turn:
                            text_chunk = format_claude_tool_prelude(cleaned_block)
                            if text_chunk:
                                now = time.monotonic()
                                if now - last_notif_t > 0.3:
                                    try:
                                        html_chunk = markdown_to_telegram_html(text_chunk)
                                        await reply_msg.answer(html_chunk, parse_mode="HTML")
                                    except Exception:
                                        await reply_msg.answer(text_chunk)
                                    last_notif_t = time.monotonic()
                                    _last_visible_t[0] = last_notif_t
                                    summary_sent_this_turn = True

                    elif btype == "tool_use":
                        notif = format_tool_notification(
                            block.get("name", ""), block.get("input", {})
                        )
                        if notif:
                            now = time.monotonic()
                            if now - last_notif_t > 0.3:
                                try:
                                    await reply_msg.answer(notif, parse_mode="HTML")
                                except Exception:
                                    await reply_msg.answer(notif)
                                last_notif_t = time.monotonic()
                                _last_visible_t[0] = last_notif_t
                                # Re-send typing after notification — Telegram resets it on each message
                                try:
                                    await bot.send_chat_action(reply_msg.chat.id, ChatAction.TYPING)
                                except Exception:
                                    pass

            elif etype == "result":
                final_result = event.get("result", "")
                new_session_id = event.get("session_id")
                is_error = event.get("is_error", False)
                logger.info(
                    f"Stream result: {len(final_result)} chars, "
                    f"session={new_session_id}, error={is_error}"
                )

        await proc.wait()
        stderr_data = await proc.stderr.read()
        if stderr_data.strip():
            logger.warning(f"Claude stderr: {stderr_data.decode(errors='replace')[:500]}")

    except Exception:
        logger.exception("Error in Claude streaming")
        final_result = final_result or provider_unavailable_message(
            PROVIDER_CLAUDE, "Ошибка при запуске Claude."
        )
    finally:
        if user_id is not None:
            _active_procs.pop(user_id, None)
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
        stop_typing.set()
        typing_task.cancel()
        heartbeat_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    # If we got nothing and had a session — retry fresh (stale session)
    if not final_result and session_id and not _is_retry:
        logger.warning("Empty result with session → retrying without session")
        return await run_claude_streaming(
            prompt, None, reply_msg, bot, files, _is_retry=True
        )

    return final_result or "Не получил ответ от модели. Попробуй ещё раз.", new_session_id, streamed_files


async def run_codex_streaming(
    prompt: str,
    session_id: str | None,
    reply_msg: Message,
    bot: Bot,
    files: list[str] | None = None,
    _is_retry: bool = False,
    lang: str = "Russian",
    user_id: int | None = None,
) -> tuple[str, str | None, list[str]]:
    """Run Codex CLI (non-interactive JSON mode) and return (final_result, thread_id, files)."""
    env = os.environ.copy()
    project_root = BOT_CWD
    current_codex_model = load_codex_model()
    # codex v0.118.0 ignores top-level -s for `exec`; pass sandbox on exec subcommand.
    cmd_base = ["codex", "-a", CODEX_APPROVAL_POLICY, "exec", "-s", CODEX_SANDBOX_MODE]

    if session_id:
        cmd = [*cmd_base, "resume", "--json"]
        if current_codex_model:
            cmd.extend(["-m", current_codex_model])
        if files:
            for file_path in files:
                cmd.extend(["-i", file_path])
        cmd.extend([session_id, "-"])
    else:
        cmd = [*cmd_base, "--json"]
        if current_codex_model:
            cmd.extend(["-m", current_codex_model])
        if files:
            for file_path in files:
                cmd.extend(["-i", file_path])
        cmd.append("-")

    logger.info(
        f"Codex streaming: model={current_codex_model or 'default'}, sandbox={CODEX_SANDBOX_MODE}, "
        f"approval={CODEX_APPROVAL_POLICY}, images={len(files) if files else 0}, "
        f"resume={session_id is not None}, retry={_is_retry}"
    )

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(bot, reply_msg.chat.id, stop_typing))

    final_result = ""
    new_session_id = session_id
    event_error: str | None = None
    pending_agent_text: str | None = None
    last_agent_text: str | None = None
    last_notif_t = 0.0
    last_progress_key = ""
    recent_progress_keys: dict[str, float] = {}
    saw_visible_work = False

    _last_visible_t = [time.monotonic()]
    _HB_CHECK = 30
    _HB_NOTIFY = 120

    async def _heartbeat():
        total_visible_secs = 0
        while True:
            await asyncio.sleep(_HB_CHECK)
            since_visible = time.monotonic() - _last_visible_t[0]
            if since_visible >= _HB_NOTIFY:
                total_visible_secs += int(since_visible)
                mins = total_visible_secs // 60
                _last_visible_t[0] = time.monotonic()
                try:
                    await reply_msg.answer(f"⏳ Ещё работаю... ({mins} мин)")
                except Exception:
                    pass

    heartbeat_task = asyncio.create_task(_heartbeat())

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            env=env,
            cwd=project_root,
        )
        if user_id is not None:
            _active_procs[user_id] = proc

        stdin_payload = f"{build_provider_system_prompt(lang)}\n\n{prompt}".encode()
        proc.stdin.write(stdin_payload)
        await proc.stdin.drain()
        proc.stdin.close()

        _buf = b""

        async def _next_line() -> bytes:
            nonlocal _buf
            while b"\n" not in _buf:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    result = _buf
                    _buf = b""
                    return result
                _buf += chunk
            idx = _buf.index(b"\n")
            result = _buf[:idx]
            _buf = _buf[idx + 1:]
            return result

        _stream_start = time.monotonic()
        _ABSOLUTE_TIMEOUT = 1800
        _POLL_INTERVAL = 30

        async def _emit_progress(text: str | None) -> bool:
            nonlocal last_notif_t, last_progress_key, recent_progress_keys
            text = (text or "").strip()
            if not text:
                return False

            progress_key = _progress_dedupe_key(text)
            if not progress_key or progress_key == last_progress_key:
                return False

            now = time.monotonic()
            recent_progress_keys = {
                key: ts for key, ts in recent_progress_keys.items()
                if now - ts <= 25
            }
            if progress_key in recent_progress_keys:
                return False
            if now - last_notif_t <= 0.3:
                return False

            try:
                if "<" in text and ">" in text:
                    await reply_msg.answer(text, parse_mode="HTML")
                else:
                    html_chunk = markdown_to_telegram_html(text)
                    await reply_msg.answer(html_chunk, parse_mode="HTML")
            except Exception:
                await reply_msg.answer(text)

            last_notif_t = time.monotonic()
            _last_visible_t[0] = last_notif_t
            last_progress_key = progress_key
            recent_progress_keys[progress_key] = last_notif_t
            return True

        async def _flush_pending_agent_text() -> bool:
            nonlocal pending_agent_text, saw_visible_work
            if not pending_agent_text:
                return False
            if not saw_visible_work:
                pending_agent_text = None
                return False

            text = format_codex_agent_progress(pending_agent_text)
            pending_agent_text = None
            return await _emit_progress(text)

        while True:
            try:
                line_bytes = await asyncio.wait_for(_next_line(), timeout=_POLL_INTERVAL)
            except asyncio.TimeoutError:
                if proc.returncode is not None:
                    logger.error(f"Codex process died (rc={proc.returncode})")
                    event_error = "Codex process unexpectedly stopped."
                    break
                if time.monotonic() - _stream_start > _ABSOLUTE_TIMEOUT:
                    logger.error(f"Codex absolute timeout ({_ABSOLUTE_TIMEOUT}s)")
                    proc.kill()
                    await proc.communicate()
                    event_error = "⏱ Timeout: Codex не ответил за 30 минут."
                    break
                continue

            if not line_bytes:
                break
            line = line_bytes.decode(errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.debug(f"Codex non-JSON line: {line[:120]}")
                continue

            etype = event.get("type", "")
            if etype == "thread.started":
                new_session_id = event.get("thread_id") or new_session_id
            elif etype == "turn.completed":
                await _flush_pending_agent_text()
                saw_visible_work = False
                if last_agent_text:
                    final_result = last_agent_text or final_result
            elif etype == "item.started":
                item = event.get("item", {})
                notif = format_codex_item_notification(item)
                if notif:
                    saw_visible_work = True
                    pending_agent_text = None
                    await _emit_progress(notif)
            elif etype == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    text = (item.get("text") or "").strip()
                    if text:
                        last_agent_text = text
                        pending_agent_text = text
                else:
                    await _flush_pending_agent_text()
            elif etype == "error":
                await _flush_pending_agent_text()
                event_error = event.get("message") or "Codex returned an error."

        rc = await proc.wait()
        stderr_data = (await proc.stderr.read()).decode(errors="replace").strip()
        if stderr_data:
            logger.warning(f"Codex stderr: {stderr_data[:500]}")
        if rc != 0 and not event_error:
            event_error = stderr_data or f"Codex exited with code {rc}."
        if not final_result:
            final_result = pending_agent_text or last_agent_text or ""

    except Exception:
        logger.exception("Error in Codex streaming")
        event_error = event_error or "Ошибка при запуске Codex."
    finally:
        if user_id is not None:
            _active_procs.pop(user_id, None)
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
        stop_typing.set()
        typing_task.cancel()
        heartbeat_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    if not final_result and session_id and not _is_retry:
        logger.warning("Empty Codex result with session → retrying without session")
        return await run_codex_streaming(
            prompt, None, reply_msg, bot, files, _is_retry=True, lang=lang
        )

    if event_error and not final_result:
        return provider_unavailable_message(PROVIDER_CODEX, event_error), new_session_id, []

    # Codex's final agent_message lands in final_result, so [FILE] markers are
    # caught by extract_file_blocks at the call site — no separate streamed list.
    return final_result or "No response", new_session_id, []


async def run_provider_streaming(
    provider: str,
    prompt: str,
    session_id: str | None,
    reply_msg: Message,
    bot: Bot,
    files: list[str] | None = None,
    lang: str = "Russian",
    user_id: int | None = None,
) -> tuple[str, str | None, list[str]]:
    if provider == PROVIDER_CODEX:
        return await run_codex_streaming(
            prompt, session_id, reply_msg, bot, files=files, lang=lang, user_id=user_id
        )
    if provider == PROVIDER_CLAUDE:
        return await run_claude_streaming(
            prompt, session_id, reply_msg, bot, files=files, lang=lang, user_id=user_id
        )
    return (
        "❌ Неизвестный провайдер. Используйте /provider claude или /provider codex.",
        None,
        [],
    )


# ── Router & Handlers ─────────────────────────────────────────────────────────

router = Router()


def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


@router.message(Command("new"))
async def cmd_new(message: Message):
    if not is_allowed(message.from_user.id):
        return
    provider = get_active_provider(message.from_user.id)
    sessions = load_sessions(provider)
    sessions.pop(str(message.from_user.id), None)
    save_sessions(provider, sessions)
    await message.answer(f"🗑 Session cleared for provider: {provider}.")


@router.message(Command("provider"))
async def cmd_provider(message: Message):
    user_id = message.from_user.id
    if not is_allowed(user_id):
        return

    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    current = get_active_provider(user_id)

    if len(parts) == 1:
        await message.answer(
            f"🔌 Current provider: <code>{current}</code>\n"
            "Switch: <code>/provider claude</code> or <code>/provider codex</code>",
            parse_mode="HTML",
        )
        return

    requested = parts[1].strip().lower()
    if requested not in SUPPORTED_PROVIDERS:
        await message.answer("Usage: /provider claude | /provider codex")
        return

    set_active_provider(user_id, requested)
    await message.answer(f"✅ Provider switched to: <code>{requested}</code>", parse_mode="HTML")




@router.message(Command("status"))
async def cmd_status(message: Message):
    provider = get_active_provider(message.from_user.id)
    sessions = load_sessions(provider)
    has_session = str(message.from_user.id) in sessions
    current_model = load_claude_model() if provider == PROVIDER_CLAUDE else resolve_codex_model()
    await message.answer(
        f"🤖 <b>lil_worker status</b>\n"
        f"User ID: <code>{message.from_user.id}</code>\n"
        f"Model: <code>{current_model}</code>\n"
        f"Provider: <code>{provider}</code>\n"
        f"Streaming: <code>ON</code>\n"
        f"Session ({provider}): {'✅ active' if has_session else '❌ none'}",
        parse_mode="HTML",
    )


async def _flush_photo_buffer(user_id: int, bot: Bot):
    """Called after PHOTO_DEBOUNCE_DELAY — downloads all buffered photos and sends to Claude."""
    await asyncio.sleep(PHOTO_DEBOUNCE_DELAY)

    buf = _photo_buffer.pop(user_id, None)
    if not buf:
        return

    reply_msg: Message = buf["reply_msg"]
    caption = buf["caption"]
    photo_ids: list[str] = buf["photos"]
    tmp_paths: list[str] = []

    # Download all photos
    for file_id in photo_ids:
        try:
            file = await bot.get_file(file_id)
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                await bot.download_file(file.file_path, tmp.name)
                tmp_paths.append(tmp.name)
        except Exception:
            logger.exception(f"Failed to download photo {file_id}")

    if not tmp_paths:
        await reply_msg.answer("❌ Не вдалося завантажити фото.")
        return

    count = len(tmp_paths)
    logger.info(f"PHOTO BATCH uid={user_id}, count={count}, caption={caption[:60]!r}")

    if count == 1:
        await reply_msg.answer("📷 Отримав фото, обробляю...")
    else:
        await reply_msg.answer(f"📷 Отримав {count} фото, обробляю...")

    provider = get_active_provider(user_id)
    session_id = get_session_id(user_id, provider)

    if count == 1:
        prompt = f"I'm sending you an image. {caption}"
    else:
        prompt = f"I'm sending you {count} images at once. {caption}"

    lang = detect_language(caption) if caption != "Describe this image." else "Russian"

    response, new_session_id, streamed_files = await run_provider_streaming(
        provider, prompt, session_id, reply_msg, bot, files=tmp_paths, lang=lang
    )

    # Cleanup temp files
    for p in tmp_paths:
        try:
            os.unlink(p)
        except OSError:
            pass

    update_session_id(user_id, provider, new_session_id, session_id)

    response_no_files, file_paths = extract_file_blocks(response)
    cleaned_response, voice_blocks = extract_voice_blocks(response_no_files)

    if cleaned_response:
        response_html = markdown_to_telegram_html(cleaned_response)
        await send_long_message(reply_msg, response_html)

    for vb_lang, vb_text in voice_blocks:
        await send_voice_with_indicator(reply_msg, bot, vb_text, vb_lang, user_id)

    await send_files(reply_msg, streamed_files + file_paths)


@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot):
    if not is_allowed(message.from_user.id):
        await message.answer("Not authorized.")
        return

    user_id = message.from_user.id
    photo = message.photo[-1]  # highest resolution

    # If part of an album (media_group) or standalone — buffer either way
    # This ensures consistent handling and catches rapid single photos too
    if user_id in _photo_buffer:
        # Add to existing buffer
        _photo_buffer[user_id]["task"].cancel()
        _photo_buffer[user_id]["photos"].append(photo.file_id)
        # Caption from first photo with a caption wins
        if message.caption and _photo_buffer[user_id]["caption"] == "Describe this image.":
            _photo_buffer[user_id]["caption"] = message.caption
        logger.info(f"PHOTO uid={user_id} buffered #{len(_photo_buffer[user_id]['photos'])}")
    else:
        _photo_buffer[user_id] = {
            "photos": [photo.file_id],
            "caption": message.caption or "Describe this image.",
            "reply_msg": message,
        }
        logger.info(f"PHOTO uid={user_id} first in buffer")

    task = asyncio.create_task(_flush_photo_buffer(user_id, bot))
    _photo_buffer[user_id]["task"] = task


@router.message((F.voice | F.audio | F.document) & F.caption.startswith("/saveasset"))
async def handle_saveasset(message: Message, bot: Bot):
    user_id = message.from_user.id
    if not is_allowed(user_id):
        return
    parts = (message.caption or "").strip().split()
    if len(parts) < 2:
        await message.answer("Usage: send file with caption /saveasset filename.ogg")
        return
    filename = parts[1]
    if "/" in filename or ".." in filename:
        await message.answer("Invalid filename.")
        return
    save_path = Path(f"/opt/test_bot/assets/{filename}")
    file_obj = message.voice or message.audio or message.document
    tg_file = await bot.get_file(file_obj.file_id)
    await bot.download_file(tg_file.file_path, destination=str(save_path))
    await message.answer(f"Saved: assets/{filename}")


@router.message(F.voice | F.audio)
async def handle_voice(message: Message, bot: Bot):
    user_id = message.from_user.id
    if not is_allowed(user_id):
        await message.answer("Not authorized.")
        return

    if not OPENAI_API_KEY:
        await message.answer("❌ OPENAI_API_KEY not configured.")
        return

    voice = message.voice or message.audio
    file = await bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await bot.download_file(file.file_path, tmp.name)
        tmp_path = tmp.name

    try:
        _tcfg_path = Path(__file__).parent / "transcribe_config.json"
        try:
            _tcfg = json.loads(_tcfg_path.read_text())
        except Exception:
            _tcfg = {}
        _tr_language = _tcfg.get("language")
        _tr_temperature = _tcfg.get("temperature", 0.2)
        logger.info(f"Transcribe config: language={_tr_language}, temperature={_tr_temperature}")

        _tr_kwargs = dict(
            model=OPENAI_VOICE_MODEL,
            file=None,
            prompt="The speaker uses Ukrainian, Russian, or English ONLY. Never output other languages.",
            temperature=_tr_temperature,
        )
        if _tr_language:
            _tr_kwargs["language"] = _tr_language

        client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        with open(tmp_path, "rb") as audio_file:
            _tr_kwargs["file"] = audio_file
            transcription = await client.audio.transcriptions.create(**_tr_kwargs)
        text = transcription.text.strip()
        logger.info(f"VOICE uid={user_id}, transcribed: {text[:120]!r}")
    except Exception:
        logger.exception("Voice transcription failed")
        await message.answer("❌ Ошибка транскрипции голосового.")
        return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if not text:
        await message.answer("❌ Не удалось распознать речь.")
        return

    provider = get_active_provider(user_id)
    session_id = get_session_id(user_id, provider)
    lang = detect_language(text)
    logger.info(f"Detected language (voice): {lang}, provider={provider}")

    response, new_session_id, streamed_files = await run_provider_streaming(
        provider, text, session_id, message, bot, lang=lang
    )

    update_session_id(user_id, provider, new_session_id, session_id)

    response_no_files, file_paths = extract_file_blocks(response)
    cleaned_response, voice_blocks = extract_voice_blocks(response_no_files)

    if cleaned_response:
        response_html = markdown_to_telegram_html(cleaned_response)
        await send_long_message(message, response_html)

    for vb_lang, vb_text in voice_blocks:
        await send_voice_with_indicator(message, bot, vb_text, vb_lang, user_id)

    await send_files(message, streamed_files + file_paths)


@router.message(F.text & ~F.text.startswith("/"))
async def handle_message(message: Message, bot: Bot):
    user_id = message.from_user.id
    if not is_allowed(user_id):
        await message.answer("Not authorized.")
        return

    text = message.text
    if not text:
        return

    # Send typing immediately — before debounce delay, so user sees response right away
    try:
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    except Exception:
        pass

    if user_id in _msg_buffer:
        _msg_buffer[user_id]["task"].cancel()
        _msg_buffer[user_id]["parts"].append(text)
        logger.info(f"MSG uid={user_id} buffered part #{len(_msg_buffer[user_id]['parts'])}")
    else:
        _msg_buffer[user_id] = {
            "parts": [text],
            "reply_msg": message,
        }

    task = asyncio.create_task(_flush_buffer(user_id, bot))
    _msg_buffer[user_id]["task"] = task


# ── Main ──────────────────────────────────────────────────────────────────────

def _job_read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def _job_dump_text(job_dir: Path, spec: dict, status: str) -> str:
    """The plain (v0) notification: header + a tail of the raw result. Used for non-wake jobs and
    as the fallback when a wake report fails. Sent with parse_mode=None (output is untrusted)."""
    label = spec.get("label", "job")
    rc = _job_read(job_dir / "exit_code")
    dur = _job_read(job_dir / "duration_sec")
    result = _job_read(job_dir / "result.txt")
    if len(result) > JOBS_RESULT_PREVIEW:
        result = "…" + result[-JOBS_RESULT_PREVIEW:]
    head = "✅ завершена" if status == "done" else "❌ упала"
    return (
        f"🦐 Фоновая задача «{label}» {head}\n"
        f"job {spec.get('id', job_dir.name)} · {dur}s · rc={rc}\n\n"
        f"{result or '(пустой вывод)'}\n\n"
        f"(полный результат: {job_dir / 'result.txt'})"
    )


async def _wake_and_report(bot: Bot, job_dir: Path, spec: dict, status: str) -> bool:
    """Wake a fresh ISOLATED claude turn to report a finished job into the owner's chat, in my own
    voice. Isolated identity = synthetic user_id (-owner): its own session + _active_procs slot, so
    it can never race the interactive chat. Returns True if the report streamed, False to fall back
    to the raw v0 dump. One wake at a time (_wake_lock)."""
    owner = spec.get("owner_uid")
    label = spec.get("label", "job")
    rc = _job_read(job_dir / "exit_code")
    dur = _job_read(job_dir / "duration_sec")
    result = _job_read(job_dir / "result.txt")
    if len(result) > WAKE_RESULT_FEED:
        result = "…(обрезано)…\n" + result[-WAKE_RESULT_FEED:]
    try:
        cmd = base64.b64decode(spec.get("cmd_b64", "")).decode(errors="replace")
    except Exception:
        cmd = ""

    async with _wake_lock:
        try:
            lead = await bot.send_message(
                owner,
                f"🦐 Проснулась — фоновая задача «{label}» завершилась, смотрю результат…",
                parse_mode=None,
            )
        except Exception as e:
            logger.warning(f"wake {job_dir.name}: lead-in send failed: {e}")
            return False

        wake_uid = -int(owner)   # isolated identity (real Telegram ids are positive)
        prompt = (
            "[АВТОНОМНОЕ ПРОБУЖДЕНИЕ — доклад о фоновой задаче]\n"
            f"Метка: {label}\n"
            f"job: {spec.get('id', job_dir.name)} · длительность {dur}s · код выхода {rc} "
            f"({'успешно' if status == 'done' else 'ОШИБКА'})\n"
            f"Команда: {cmd}\n"
            "--- Результат (stdout+stderr) ---\n"
            f"{result or '(пустой вывод)'}\n"
            "--- Конец результата ---\n\n"
            "Ты — креветка. Это не интерактивный чат, а автономное пробуждение, чтобы кратко "
            "доложить пользователю о завершённой фоновой задаче: что сделано, главное из "
            "результата, всё ли в порядке и что стоит сделать дальше. Отвечай по-русски, сжато, "
            "без лишних преамбул. Не выдумывай того, чего нет в результате."
        )
        session_id = get_session_id(wake_uid, PROVIDER_CLAUDE)
        try:
            response, new_sid, streamed_files = await run_provider_streaming(
                PROVIDER_CLAUDE, prompt, session_id, lead, bot, lang="ru", user_id=wake_uid
            )
        except Exception as e:
            logger.warning(f"wake {job_dir.name}: reasoning failed: {e}")
            return False
        update_session_id(wake_uid, PROVIDER_CLAUDE, new_sid, session_id)

        # mirror _flush_buffer post-processing (text + files; reports don't emit voice)
        response_no_files, file_paths = extract_file_blocks(response)
        cleaned, _voice = extract_voice_blocks(response_no_files)
        if cleaned:
            await send_long_message(lead, markdown_to_telegram_html(cleaned))
        await send_files(lead, streamed_files + file_paths)
        return True


async def _notify_finished_jobs(bot: Bot) -> None:
    """Scan JOBS_DIR once: message the owner about any terminal, not-yet-notified job, then mark
    it notified. Prune old notified jobs. Owner must be an allowed user. Idempotent per tick —
    a send failure is retried next tick (the notified marker is written only after a good send)."""
    if not JOBS_DIR.is_dir():
        return
    now = time.time()
    for job_dir in sorted(JOBS_DIR.iterdir()):
        if not job_dir.is_dir() or not (job_dir / "spec.json").exists():
            continue
        status = _job_read(job_dir / "status")
        notified = job_dir / "notified"

        if notified.exists():
            # already handled — prune if it has aged out
            try:
                if (now - notified.stat().st_mtime) > JOBS_PRUNE_DAYS * 86400:
                    shutil.rmtree(job_dir, ignore_errors=True)
            except OSError:
                pass
            continue

        if status not in ("done", "failed"):
            continue

        try:
            spec = json.loads((job_dir / "spec.json").read_text())
        except (OSError, json.JSONDecodeError):
            spec = {}
        owner = spec.get("owner_uid")
        if owner not in ALLOWED_USERS:
            # never message a non-allowed chat; mark handled so we don't rescan forever
            logger.warning(f"job {job_dir.name}: owner {owner} not in ALLOWED_USERS — skip notify")
            notified.write_text("skipped-owner")
            continue

        if spec.get("wake"):
            # v1: mark handled up-front so a mid-wake bot restart can't double-report, then wake an
            # isolated reasoning turn. On wake failure, fall back to the raw v0 dump.
            notified.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            ok = await _wake_and_report(bot, job_dir, spec, status)
            if not ok:
                try:
                    await bot.send_message(owner, _job_dump_text(job_dir, spec, status), parse_mode=None)
                except Exception as e:
                    logger.warning(f"job {job_dir.name}: fallback dump failed: {e}")
            logger.info(f"job {job_dir.name}: wake-report done (ok={ok}) owner={owner}")
            continue

        # v0: plain raw-dump notification, retry-safe (notified written only after a good send)
        try:
            # parse_mode=None: raw job output is untrusted — never let it be parsed as HTML.
            await bot.send_message(owner, _job_dump_text(job_dir, spec, status), parse_mode=None)
        except Exception as e:
            logger.warning(f"job {job_dir.name}: notify send failed, will retry: {e}")
            continue
        notified.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        logger.info(f"job {job_dir.name}: notified owner {owner} (status={status})")


async def poll_jobs_loop(bot: Bot) -> None:
    """Periodic tick: only the privileged (main) instance notifies about finished jobs."""
    if INSTANCE_NAME != PRIVILEGED_INSTANCE:
        return
    while True:
        try:
            await _notify_finished_jobs(bot)
        except Exception as e:
            logger.warning(f"jobs poll error: {e}")
        await asyncio.sleep(JOBS_POLL_INTERVAL)


async def main():
    if not BOT_TOKEN:
        print("ERROR: Set TELEGRAM_BOT_TOKEN in .env")
        return

    validate_startup = "--validate-startup" in os.sys.argv

    # Write own PID file — reliable even when run.sh gets killed mid-restart
    pid_file = DATA_DIR / "lil_worker.pid"
    pid_file.write_text(str(os.getpid()))
    # Stamp the liveness file immediately (before any slow startup work) so a stale file from a
    # previous run can't make the health check false-report the fresh process as unhealthy.
    _write_heartbeat_file()
    runtime_state = _default_runtime_state()
    runtime_state["validate_startup"] = validate_startup
    write_runtime_state(runtime_state)

    print(f"Starting lil_worker bot (model: {CLAUDE_MODEL}, streaming: ON)...")
    print(f"Allowed users: {ALLOWED_USERS or 'Everyone (WARNING: set ALLOWED_USERS!)'}")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    print("Bot running. Send a message on Telegram.")
    update_runtime_state(runtime_state, phase="startup_ready")

    runtime_ok = ensure_local_runtime()
    if not runtime_ok:
        logger.warning("Local runtime did not become healthy during startup")

    if validate_startup:
        print("Validation startup path completed without live Telegram polling.")
        update_runtime_state(runtime_state, phase="validate_startup_ok")
        pid_file.unlink(missing_ok=True)
        return

    # Notify users that the bot has started (confirms restart completed successfully)
    # Check for restart reason file — Claude writes it before calling run.sh restart
    restart_reason_file = DATA_DIR / "restart_reason.txt"
    startup_msg = "✅ Бот запущен."
    if restart_reason_file.exists():
        try:
            reason = restart_reason_file.read_text().strip()
            if reason:
                report_hash = _restart_report_hash(reason)
                last_hash = LAST_RESTART_REPORT_HASH_FILE.read_text().strip() if LAST_RESTART_REPORT_HASH_FILE.exists() else ""
                if report_hash == last_hash:
                    startup_msg = "✅ Служебный рестарт прошёл."
                else:
                    startup_msg = _format_restart_startup_message(reason)
                    LAST_RESTART_REPORT_HASH_FILE.write_text(report_hash)
            restart_reason_file.unlink()
        except Exception:
            pass
    for uid in ALLOWED_USERS:
        try:
            await bot.send_message(uid, startup_msg)
        except Exception:
            pass

    # PRIMARY liveness: load-immune OS-thread heartbeat (stamps HEARTBEAT_FILE). Started before the
    # first heavy work and kept alive for the whole run; run.sh trusts this for "process alive".
    _write_heartbeat_file()
    hb_stop = threading.Event()
    hb_thread = threading.Thread(target=_heartbeat_thread_loop, args=(hb_stop,), daemon=True)
    hb_thread.start()

    # SECONDARY: async loop_at — proves the event loop itself is turning. run.sh uses this only as a
    # deadlock signal with a generous window, so a busy loop never triggers a restart, but a truly
    # wedged loop eventually does.
    async def loop_heartbeat():
        while True:
            update_runtime_state(runtime_state, phase="polling", loop_at=time.time())
            await asyncio.sleep(5)

    hb_task = asyncio.create_task(loop_heartbeat())
    jobs_task = asyncio.create_task(poll_jobs_loop(bot))
    try:
        update_runtime_state(runtime_state, phase="polling", loop_at=time.time())
        await dp.start_polling(bot)
    except Exception as e:
        update_runtime_state(runtime_state, phase="failed", last_error=str(e))
        raise
    finally:
        hb_stop.set()
        hb_task.cancel()
        jobs_task.cancel()
        update_runtime_state(runtime_state, phase="stopped")
        pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
