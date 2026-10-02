#!/usr/bin/env python3
"""What shape are an instance's turns? — block types, stop reasons, closing-message length.

Built 2026-10-02, after doing this twice by hand in one sitting. The occasion: the `game` instance
moved to Opus 5.5 and the owner felt it had started "stopping mid-thought". Reading the transcript
settled it in a way no amount of discussion could - the turn had ended cleanly (`end_turn`), the
message he saw was a real `text` block and not thinking leaking through, and what had actually
changed was the LENGTH of the closing message: under 700 characters in 3 of 97 turns on Opus 5,
then 8 of 19 on Opus 5.5.

That question now recurs by construction: every door we move to a new model needs the same check,
and "it feels different" is not something you can act on. This turns it into one command.

    python3 tools/turn_shape.py game                 # the last turns, one line each
    python3 tools/turn_shape.py game --stats         # distribution, split by model
    python3 tools/turn_shape.py game --stats --since 2026-09-30T16:52
    python3 tools/turn_shape.py --session /path/to.jsonl --last 30

**What counts as a "turn" here:** one assistant message that ended with a stop reason. The bot
streams every text block to Telegram as its own message, so the LAST text block of a run is the
whole product as the owner receives it - which is why its length is the number worth watching.

Timestamps in a transcript are UTC; the bot's own log is local. A three-hour gap between them is
the timezone, not a bug.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = Path("/root/.claude/projects")
SHORT_DEFAULT = 700   # the threshold that made the Opus 5 -> 5.5 shift visible


def project_dir(cwd: str) -> Path:
    """Claude Code's folder name for a working directory: every / and _ becomes a dash."""
    return PROJECTS / cwd.replace("/", "-").replace("_", "-")


def instance_env(name: str) -> dict[str, str]:
    path = ROOT / "bot" / "instances" / name / "instance.env"
    if not path.exists():      # the main door keeps its config in bot/ itself
        return {"LIL_WORKER_BOT_CWD": str(ROOT), "LIL_WORKER_DATA_DIR": str(ROOT / "bot")}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def find_session(name: str) -> Path:
    """The instance's live transcript: the most recently written of the sessions it knows about.

    `.sessions.json` maps each chat to a session id, and an instance can hold several (the owner's
    private chat, a group). Newest-written is the one that just produced the behaviour being asked
    about; falling back to the newest file in the project folder keeps this working for a session
    the bot has not recorded yet.
    """
    env = instance_env(name)
    pdir = project_dir(env.get("LIL_WORKER_BOT_CWD", str(ROOT)))
    if not pdir.is_dir():
        sys.exit(f"no transcript folder for instance {name!r}: {pdir}")

    candidates: list[Path] = []
    sess = Path(env.get("LIL_WORKER_DATA_DIR", "")) / ".sessions.json"
    try:
        for sid in json.loads(sess.read_text(encoding="utf-8")).values():
            f = pdir / f"{sid}.jsonl"
            if f.exists():
                candidates.append(f)
    except Exception:
        pass
    if not candidates:
        candidates = list(pdir.glob("*.jsonl"))
    if not candidates:
        sys.exit(f"no .jsonl transcripts under {pdir}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def kinds_of(content) -> tuple[list[str], int]:
    """Human-readable block list, and the characters of visible text in it."""
    kinds, text_len = [], 0
    if not isinstance(content, list):
        return kinds, text_len
    for b in content:
        k = b.get("type")
        if k == "text":
            n = len(b.get("text") or "")
            text_len += n
            kinds.append(f"text({n})")
        elif k == "thinking":
            # length 0 is normal: display defaults to omitted, so the text is withheld, not missing
            kinds.append(f"thinking({len(b.get('thinking') or '')})")
        elif k == "tool_use":
            kinds.append(f"tool:{b.get('name')}")
        elif k == "tool_result":
            kinds.append("tool_result")
        elif k:
            kinds.append(k)
    return kinds, text_len


def walk(path: Path, since: str | None):
    """Yield (ts, model, stop_reason, kinds, text_len) for every assistant message that ENDED.

    Streamed line by line on purpose - these files reach tens of megabytes.
    """
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") != "assistant":
                continue
            m = r.get("message") or {}
            stop = m.get("stop_reason")
            if not stop:
                continue
            ts = str(r.get("timestamp") or "")
            if since and ts < since:
                continue
            kinds, text_len = kinds_of(m.get("content"))
            yield ts, m.get("model") or "?", stop, kinds, text_len


def cmd_list(path: Path, last: int, since: str | None, short: int) -> None:
    rows = deque(walk(path, since), maxlen=last)
    if not rows:
        print("ничего не найдено")
        return
    print(f"{path.name}  —  последние {len(rows)} завершённых сообщений (время UTC)\n")
    for ts, model, stop, kinds, text_len in rows:
        mark = "  ⚠ коротко" if (stop == "end_turn" and 0 < text_len < short) else ""
        print(f"  {ts[:19].replace('T',' ')}  {stop:10} {model:18} {' '.join(kinds)[:70]}{mark}")


def cmd_stats(path: Path, since: str | None, short: int) -> None:
    """Closing messages only, grouped by model — the comparison a model switch actually needs."""
    by_model: dict[str, list[int]] = defaultdict(list)
    for _ts, model, stop, _kinds, text_len in walk(path, since):
        if stop == "end_turn" and text_len:
            by_model[model].append(text_len)

    if not by_model:
        print("завершающих сообщений не найдено")
        return
    print(f"{path.name}  —  длина ЗАВЕРШАЮЩЕГО сообщения хода, по моделям\n")
    for model, lens in sorted(by_model.items()):
        n = len(lens)
        s = sum(1 for x in lens if x < short)
        print(f"  {model}")
        print(f"    ходов: {n} | медиана: {int(statistics.median(lens))} знаков | "
              f"мин {min(lens)} | макс {max(lens)}")
        print(f"    короче {short} знаков: {s} из {n}  ({100*s//n}%)")
    print("\n  Доля коротких — это и есть та метрика, по которой видно «он стал обрывать ход».")


def main() -> int:
    ap = argparse.ArgumentParser(description="Форма ходов инстанса: блоки, причины остановки, длина ответа")
    ap.add_argument("instance", nargs="?", help="имя инстанса (game, twin, helper, lil_worker…)")
    ap.add_argument("--session", help="путь к .jsonl напрямую, вместо имени инстанса")
    ap.add_argument("--last", type=int, default=15, help="сколько последних сообщений показать")
    ap.add_argument("--since", help="брать только записи с этого момента (UTC, напр. 2026-09-30T16:52)")
    ap.add_argument("--short-under", type=int, default=SHORT_DEFAULT, help="порог «короткого» ответа")
    ap.add_argument("--stats", action="store_true", help="распределение по моделям вместо списка")
    a = ap.parse_args()

    if not a.instance and not a.session:
        ap.error("нужно имя инстанса или --session")
    path = Path(a.session) if a.session else find_session(a.instance)
    if not path.exists():
        sys.exit(f"нет файла: {path}")

    if a.stats:
        cmd_stats(path, a.since, a.short_under)
    else:
        cmd_list(path, a.last, a.since, a.short_under)
    return 0


if __name__ == "__main__":
    sys.exit(main())
