#!/usr/bin/env python3
"""Model-free safety net over a daily alerts run (owner, 16.10.2026).

The daily report reads the whole day once and the model decides what is red. A single big sweep can
miss a grave event — the model misreads a line, or a capture is buried in a long window. This is the
backstop: a pure keyword scan, NO model, over every OUR-direction intercept the run did NOT already
mark red. What it catches, it surfaces to the red bot tagged «СТРАХОВКА», so a human looks.

The design choice that makes it useful instead of noisy: **few, high-precision triggers**, not an
exhaustive word list. The owner warned the word space is huge and declension-ridden; a loose net
would flag half the air. So this only fires on the two gravest, least-ambiguous classes:

  1. CAPTURE — полон / плен and their cases, surrender. The single worst event to drop.
  2. COORDINATES read out — reused verbatim from tools/upstream/coord_watch.py, where it was MEASURED
     at 11 frame+digit hits per 18 831 intercepts over a week, all 11 real.

Casualty codes (200/300) are deliberately NOT a trigger here: in this traffic they are also «приём»,
«двести метров», a button, a minute — the noise would drown the signal, and the model + the report's
own losses.py already carry them with the context to tell them apart. If we ever see the model drop a
clear casualty, add it then, scoped like coord_watch's frame rule — not as a bare number.

    python3 tools/alerts/safety_net.py <run.json> [--hours 24]
    python3 tools/alerts/safety_net.py <run.json> --out <net.json>   # write alerts for push_tg

It writes a tiny run-shaped json (`{"alerts": [...]}`) that push_tg.py delivers like any other, so
the message shape and the per-intercept copy-block are exactly the same as the model's own reds.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench import load_day, OURS_SRC  # noqa: E402 — one door for the window + the our-direction set

# CAPTURE. Ukrainian and Russian, with the cases the air actually uses: полон/полонили/у полоні,
# плен/пленный/в плену, сдал(ся)/здав(ся). Word-boundaried so «пленка/плёнка» (tape) and «полонина»
# do not fire. High precision on purpose.
CAPTURE = re.compile(
    r"\bполон(?:у|і|ом|или|ений|еного|еним)?\b"
    r"|\bу\s+полон(?:і|у)?\b"
    r"|\bплен(?:у|ный|ного|ным|ные|ных)?\b"
    r"|\bв\s+плен(?:у)?\b"
    r"|\bздав(?:ся|ались|ся\s+в\s+полон)\b"
    r"|\bсдал(?:ся|ись|ась)\b"
    r"|\bвзяли\s+(?:живьём|живим|живого|у\s+полон|в\s+плен)\b",
    re.I,
)

# COORDINATES — copied from tools/upstream/coord_watch.py so the two stay in step. A frame WORD plus a
# digit group; bare digits never fire (in this traffic they are codes/buttons/cubes/minutes).
FRAME = re.compile(r"запис(?:ыва|уй|ать|ати|ыв)|диктую|координат|прив['’я]?язк|харитон|ульян", re.I)
NUM_TRIPLE = re.compile(r"\b\d{2,3}(?:[ \-]\d{1,3}){2,4}\b")
NUM_PAIR = re.compile(r"\b\d{4,6}\b[ ,]+\b\d{4,6}\b")
NOT_COORD = re.compile(r"\b(?:200|300|400|450|202|303|412|415|55|44|54|33|22|11)\b")
# A Russian mobile number dictated on air («продиктую… 8 914 017 47 7») — 8 / +7, then 9xx, then the
# rest; 9-11 digits in total because the transcript drops one. Stripped before the coordinate test:
# on 21.10.2026 it fired the net as «координати» (both models had read it right as a phone number).
PHONE = re.compile(r"(?<!\d)(?:\+7|8)[ \-]?\(?9\d{2}\)?(?:[ \-]?\d){5,8}(?!\d)")


def _coord_hit(text: str) -> bool:
    if not FRAME.search(text):
        return False
    probe = NOT_COORD.sub(" ", PHONE.sub(" ", text))
    return bool(NUM_TRIPLE.search(probe) or NUM_PAIR.search(probe))


def scan(run_path: str, hours: int) -> list[dict]:
    run = json.loads(Path(run_path).read_text(encoding="utf-8"))
    red_ids = {str(a.get("id")) for a in run.get("alerts", []) if a.get("level") == "red"}

    rows = [r for r in load_day("", hours=hours) if r["src"] in OURS_SRC]
    out: list[dict] = []
    for r in rows:
        if str(r["id"]) in red_ids:
            continue  # the model already caught it — nothing to back up
        sp = r.get("speech", "") or ""
        why = None
        if CAPTURE.search(sp):
            why = "СТРАХОВКА: слово про полон/полонених, модель не позначила червоним"
        elif _coord_hit(sp):
            why = "СТРАХОВКА: координати в ефірі (рамка + цифри), модель не позначила червоним"
        if why:
            out.append({"id": r["id"], "level": "red", "class": "СТРАХОВКА", "why": why,
                        "hhmmss": r.get("hhmmss"), "freq": r.get("freq"), "src": r.get("src"),
                        "ts": r.get("ts")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    alerts = scan(a.run, a.hours)
    print(f"страховка: {len(alerts)} кандидатів (поза вже-червоними)", file=sys.stderr)
    payload = {"alerts": alerts, "source": "safety_net"}
    if a.out:
        Path(a.out).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(a.out)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
