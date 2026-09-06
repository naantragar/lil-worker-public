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


# OpenAI's audio endpoint refuses anything over 25 MB. Matrix accepts uploads up to 100 MiB, so
# this door lets through notes Telegram's 20 MB bot cap never could — the first 28 MB one would
# have died with a 413 and taken the audio with it (the caller unlinks the temp file either way).
# Fix in two steps, cheapest first: re-encode to 16 kHz mono Opus (speech loses nothing, a 28 MB
# note lands in single-digit MB), and only if that still does not fit, cut it into pieces and
# stitch the transcripts back together.
_API_LIMIT = 24 * 1024 * 1024
_SEGMENT_S = 900


def _ffmpeg(*args: str) -> bool:
    import subprocess
    try:
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                           capture_output=True, timeout=900)
        return r.returncode == 0
    except Exception:
        return False


def _fit_for_api(path: str) -> list[str]:
    """The chunk(s) to actually send: [path] if it already fits, else a shrunk copy, else segments."""
    try:
        if os.path.getsize(path) <= _API_LIMIT:
            return [path]
    except OSError:
        return [path]
    stem = path.rsplit(".", 1)[0]
    small = f"{stem}_16k.ogg"
    opus = ("-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k")
    if _ffmpeg("-i", path, *opus, small):
        try:
            if 0 < os.path.getsize(small) <= _API_LIMIT:
                return [small]
        except OSError:
            pass
    src = small if os.path.exists(small) and os.path.getsize(small) > 0 else path
    if _ffmpeg("-i", src, *opus, "-f", "segment", "-segment_time", str(_SEGMENT_S),
               f"{stem}_part_%03d.ogg"):
        import glob
        parts = sorted(glob.glob(f"{stem}_part_*.ogg"))
        if parts:
            return parts
    return [src]


async def transcribe(path: str) -> str:
    """Voice → text via OpenAI (same model as the Telegram bot, same language config)."""
    tcfg = {}
    try:
        import json
        tcfg = json.loads((_LILWORKER / "bot" / "transcribe_config.json").read_text())
    except Exception:
        pass
    _dur_s = audio_duration_ms(path) / 1000
    kwargs = dict(
        model=os.environ.get("OPENAI_VOICE_MODEL", "gpt-transcribe"),
        prompt="The speaker uses Ukrainian, Russian, or English ONLY. Never output other languages.",
        temperature=tcfg.get("temperature", 0.2),
    )
    if tcfg.get("language"):
        kwargs["language"] = tcfg["language"]
    async def _run_one(model_name: str, src: str) -> str:
        kw = dict(kwargs, model=model_name)
        with open(src, "rb") as f:
            kw["file"] = f
            return ((await _client().audio.transcriptions.create(**kw)).text or "").strip()

    chunks = _fit_for_api(path)

    async def _run(model_name: str) -> str:
        out = []
        for c in chunks:
            out.append(await _run_one(model_name, c))
        return " ".join(t for t in out if t).strip()

    try:
        text = await _run(kwargs["model"])
        # Silent-truncation guard, same as the Telegram door: these models stop early on long audio
        # and still return 200. Russian speech is ~10-14 chars/s, so under 5 c/s did not finish.
        min_cps = float(os.environ.get("MIN_CHARS_PER_SECOND", "5"))
        if _dur_s > 60 and len(text) < _dur_s * min_cps:
            try:
                alt = await _run(os.environ.get("FALLBACK_VOICE_MODEL", "whisper-1"))
                if len(alt) > len(text):
                    text = alt
            except Exception:
                pass
        return text
    finally:
        for c in chunks:
            if c != path:
                try:
                    os.unlink(c)
                except OSError:
                    pass


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
