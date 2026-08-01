"""Media helpers for matrix-bridge — mirrors lil_worker/bot/bot.py's transcription/TTS/marker logic
(same OpenAI engines), adapted to Matrix. Does NOT import bot.py."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import openai

TEMP = Path("/tmp")
# Repo root: CLAUDE_CWD when set, else derived from this file's location (matrix/bot/media.py →
# two levels up). A hardcoded absolute default only ever worked on the machine it was written on.
_LILWORKER = Path(os.environ.get("CLAUDE_CWD") or Path(__file__).resolve().parents[2])

# Marker regexes — identical to bot.py so the agent's output behaves the same on both channels.
_VOICE_RE = re.compile(
    r'(?m)^\s*\[VOICE\s+lang=["\'](\w+)["\'](?:\s+speed=["\']([0-9.]+)["\'])?\s*\](.*?)\[/VOICE\]',
    re.DOTALL,
)
_FILE_RE = re.compile(r'(?im)^\s*\[FILE[:\s]\s*(/[a-zA-Z0-9_./-]+)\s*\](?:\s*\[/FILE\])?')

_MEDIA = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
          "webp": "image/webp"}


def media_type(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return _MEDIA.get(ext, "application/octet-stream")


def extract_file_blocks(text: str) -> tuple[str, list[str]]:
    paths = [m.group(1).strip() for m in _FILE_RE.finditer(text)]
    return _FILE_RE.sub("", text).strip(), paths


def extract_voice_blocks(text: str) -> tuple[str, list[tuple[str, str, float]]]:
    blocks = []
    for m in _VOICE_RE.finditer(text):
        speech = m.group(3).strip()
        if speech:
            speed = float(m.group(2)) if m.group(2) else 1.0
            blocks.append((m.group(1), speech, max(0.25, min(4.0, speed))))
    return _VOICE_RE.sub("", text).strip(), blocks


def _client() -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])


async def transcribe(path: str) -> str:
    """Voice → text via OpenAI (same model as the Telegram bot, same language config)."""
    tcfg = {}
    try:
        import json
        tcfg = json.loads((_LILWORKER / "bot" / "transcribe_config.json").read_text())
    except Exception:
        pass
    kwargs = dict(
        model=os.environ.get("OPENAI_VOICE_MODEL", "gpt-4o-mini-transcribe"),
        prompt="The speaker uses Ukrainian, Russian, or English ONLY. Never output other languages.",
        temperature=tcfg.get("temperature", 0.2),
    )
    if tcfg.get("language"):
        kwargs["language"] = tcfg["language"]
    with open(path, "rb") as f:
        kwargs["file"] = f
        r = await _client().audio.transcriptions.create(**kwargs)
    return (r.text or "").strip()


async def synthesize(text: str, speed: float = 1.0) -> Path | None:
    """Text → OGG/Opus voice note via OpenAI TTS (same model/voice as the Telegram bot)."""
    out = TEMP / f"mb_tts_{int(time.time()*1000)}.ogg"
    try:
        async with _client().audio.speech.with_streaming_response.create(
            model=os.environ.get("TTS_MODEL", "gpt-4o-mini-tts"),
            voice=os.environ.get("TTS_VOICE", "marin"),
            input=text,
            response_format="opus",
            speed=speed,
        ) as resp:
            await resp.stream_to_file(out)
        return out
    except Exception:
        return None


def audio_duration_ms(path: str) -> int:
    try:
        from mutagen.oggopus import OggOpus
        return int(OggOpus(path).info.length * 1000)
    except Exception:
        return 0
