#!/usr/bin/env python3
"""Turn one audio file into text. Prints the transcript to stdout, nothing else.

    bot/.venv/bin/python tools/survey/transcribe.py <file> [--seconds N]

Called by `survey_bot.py` as a subprocess, because the bot itself is stdlib-only and the OpenAI
client lives in `bot/.venv`. Keeping it a separate process also means a transcription that hangs or
dies cannot take the bot down with it.

Same engine, same config and the same truncation guard as the main bot's voice path
(`bot/krevetka.py`), deliberately: one behaviour for voice in this project, not two that drift.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENV = REPO / "bot" / ".env"
CFG = REPO / "bot" / "transcribe_config.json"
PRIMARY = os.environ.get("OPENAI_VOICE_MODEL", "gpt-transcribe")
FALLBACK = os.environ.get("FALLBACK_VOICE_MODEL", "whisper-1")
MIN_CHARS_PER_SECOND = 4          # below this a long transcript is a silent early stop


def api_key() -> str:
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("немає OPENAI_API_KEY")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--seconds", type=int, default=0)
    a = ap.parse_args()

    try:
        cfg = json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        cfg = {}

    import openai
    client = openai.OpenAI(api_key=api_key())
    kw = dict(prompt="The speaker uses Ukrainian, Russian, or English ONLY. "
                     "Never output other languages.",
              temperature=cfg.get("temperature", 0.2))
    if cfg.get("language"):
        kw["language"] = cfg["language"]

    def run(model: str) -> str:
        with open(a.path, "rb") as f:
            return (client.audio.transcriptions.create(model=model, file=f, **kw).text or "").strip()

    text = run(PRIMARY)
    # The guard that matters is not "which model is faster" but the silent early stop: a long note
    # coming back as two sentences. Only then is the slower model worth its time.
    if a.seconds > 60 and len(text) < a.seconds * MIN_CHARS_PER_SECOND:
        try:
            alt = run(FALLBACK)
            if len(alt) > len(text):
                text = alt
        except Exception:                                    # noqa: BLE001
            pass
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
