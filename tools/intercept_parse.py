#!/usr/bin/env python3
"""Split a raw Patagonia message into structured intercepts.

Deterministic, stdlib only. This is the step that must NOT involve the model: timestamps,
frequencies, network headers and callsigns are regular, and every token the model does not have to
read is money and reliability saved.

Six shapes exist in the wild (owner's catalogue, 2026-08-22), and all of them are handled:

  1. `Коментар: …` above the block            4. an UNLABELLED comment above the block
  2. `Коментар: …` below the block            5. no comment at all
  3. `Мітки перехоплення: …` below the block  6. TWO intercepts inside ONE message

The anchor is the pair «timestamp line + frequency line», never the start of the text: only 1 of
1898 real messages actually began with the frequency — almost all carry a comment or a speaker
marker above it.

    python3 tools/intercept_parse.py --self-test
    python3 tools/intercept_parse.py < message.txt
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata

# WhatsApp wraps blocks in bidi isolates and sprinkles speaker emoji; they break regexes and would
# otherwise end up inside the analytic text.
_JUNK = dict.fromkeys(map(ord, "⁦⁧⁨⁩‪‫‬‎‏﻿"), None)

RE_TS = re.compile(r"^\W*?(\d{2}\.\d{2}\.\d{4})\s*,?\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*$")
RE_FREQ = re.compile(r"^\s*(\d{2,3}\.\d{3,4})\s*$")
RE_NET = re.compile(r"^\s*(УКХ|КХ)\b", re.I)
# Speech lines: -, –, —, ---, and the numbered forms "1 —", "2 -"
RE_SPEECH = re.compile(r"^\s*(?:\d{1,2}\s*)?[-–—]{1,3}\s")
RE_LABEL = re.compile(r"^\s*(Коментар|Мітки перехопл\w*|Коментарій)\s*[:\-–—]?\s*(.*)$", re.I)


def _clean(text: str) -> list[str]:
    text = (text or "").translate(_JUNK)
    text = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch)[0] != "C")
    return [ln.rstrip() for ln in text.split("\n")]


def _anchors(lines: list[str]) -> list[int]:
    """Indices of timestamp lines whose next non-empty line is a frequency."""
    out = []
    for i, ln in enumerate(lines):
        if not RE_TS.match(ln):
            continue
        for j in range(i + 1, min(i + 3, len(lines))):
            if not lines[j].strip():
                continue
            if RE_FREQ.match(lines[j]):
                out.append(i)
            break
    return out


def _parse_one(lines: list[str]) -> dict:
    """One intercept: anchor at index 0."""
    ts_m = RE_TS.match(lines[0])
    rec = {"date": ts_m.group(1), "time": ts_m.group(2), "freq": None, "network": None,
           "stations": [], "speech": [], "comment_below": None}
    i = 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and RE_FREQ.match(lines[i]):
        rec["freq"] = RE_FREQ.match(lines[i]).group(1)
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and RE_NET.match(lines[i]):
        rec["network"] = lines[i].strip()
        i += 1
    # callsign lines: everything up to the first blank line or the first speech line
    while i < len(lines) and lines[i].strip() and not RE_SPEECH.match(lines[i]):
        if RE_LABEL.match(lines[i]):
            break
        rec["stations"].append(lines[i].strip())
        i += 1
    tail = []
    for ln in lines[i:]:
        m = RE_LABEL.match(ln)
        if m:
            rec["comment_below"] = (rec["comment_below"] + " " if rec["comment_below"] else "") + m.group(2).strip()
            continue
        if RE_SPEECH.match(ln):
            rec["speech"].append(ln.strip())
        elif ln.strip() and rec["speech"]:
            # a wrapped continuation of the previous speech line
            rec["speech"][-1] += " " + ln.strip()
        elif ln.strip():
            tail.append(ln.strip())
    # non-speech text left over AFTER the dialogue and with no label = an unlabelled bottom comment
    if tail and not rec["comment_below"]:
        rec["comment_below"] = " ".join(tail)
    return rec


def parse_message(text: str) -> list[dict]:
    """Return every intercept in this message (usually one, occasionally two)."""
    lines = _clean(text)
    anchors = _anchors(lines)
    if not anchors:
        return []

    head = [ln for ln in lines[: anchors[0]] if ln.strip()]
    comment_above = None
    if head:
        m = RE_LABEL.match(head[0])
        comment_above = " ".join([m.group(2).strip()] + head[1:]).strip() if m else " ".join(head)

    out = []
    for n, start in enumerate(anchors):
        end = anchors[n + 1] if n + 1 < len(anchors) else len(lines)
        rec = _parse_one(lines[start:end])
        # The header comment belongs to the FIRST intercept of the message.
        rec["comment_above"] = comment_above if n == 0 else None
        rec["seq"] = n
        out.append(rec)
    return out


SELF_TEST = [
    ("labelled above", """Коментар: Переміщення о\\с противника під наглядом БПЛА РОВ

22.08.2026, 14:32:42
411.9630
УКХ р/м йм. 2 мсб 38 омсбр (р-н ЧАРІВНЕ - НОВОСЕЛІВКА) DMR
НВ
ЛИС

– Принял. Давай быстро, Лис, в укрытие, бегом в зеленку.""", 1),
    ("labelled below", """22.08.2026, 14:30:58
411.9650
УКХ р/м йм. 1 мсб 38 омсбр 35 А (МИРНЕ, ЧАРІВНЕ)
НВ
НВ

1 —  Принял, принял, начал движение.
2 —  Я не наблюдаю, чтоб ты двигался.
1 —  Двигаюсь.


Коментар: Йм. противник повідомляє про переміщення о/с РОВ в супроводі БПЛА""", 1),
    ("mitky below", """22.08.2026, 13:52:00
144.6650
УКХ р/м йм. 60 омсбр  (ГІРКЕ)
ЧЕРНЫЙ
ДЕМА, МОСКВА

— Дема, Дема, я Черный, прием
- На связи, на связи

Мітки перехоплення: доповідь - про стан справ""", 1),
    ("unlabelled above", """уточнення щодо переміщення о\\с

22.08.2026, 14:25:12
142.4000
УКХ р/м 1 мсб 60 омсбр (р-н ЗАЛІЗНИЧНЕ - ГУЛЯЙПІЛЬСЬКЕ)
БАТОН
МАЛИК

— Малик, Малик Батону
— Дошли до переходной точки, забираю бензин 9 6 2.""", 1),
    ("no comment", """22.08.2026, 14:31:18
437.0650
УКХ р/м 1198 мсп  ( ЧАРІВНЕ- МИРНЕ- ГУЛЯЙПІЛЬСЬКЕ) DMR
НВ
НВ

– По моей команде вылезете наверх, надо будет послушать воздух.
--- Да, да приняли.""", 1),
    ("two in one", """противник про паралельне переміщення суміжників

22.08.2026, 14:27:42
155.5000
УКХ р/м йм. 186 мсп (р-н ЗАЛІЗНИЧНЕ - ГІРКЕ - ГУЛЯЙПІЛЬСЬКЕ)
НВ
НВ

— ...что вы там прыгнули?
— Да вот здесь ребята докладывают с соседнего подразделения.


 22.08.2026, 14:29:05
155.5000
УКХ р/м йм. 186 мсп (р-н ЗАЛІЗНИЧНЕ - ГІРКЕ - ГУЛЯЙПІЛЬСЬКЕ)
НВ
НВ

— Принял, сейчас мы птичку поднимем.""", 2),
]


def self_test() -> int:
    bad = 0
    for name, text, expect in SELF_TEST:
        got = parse_message(text)
        ok = len(got) == expect and all(r["freq"] and r["network"] and r["speech"] for r in got)
        cmt = got[0].get("comment_above") or got[0].get("comment_below") or "—"
        print(f"{'OK  ' if ok else 'FAIL'} {name:18} перехватів={len(got)} "
              f"частота={got[0]['freq'] if got else '?'} реплік={len(got[0]['speech']) if got else 0} "
              f"коментар={cmt[:40]!r}")
        bad += not ok
    print(f"\n{len(SELF_TEST) - bad}/{len(SELF_TEST)} форм розібрано")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    print(json.dumps(parse_message(sys.stdin.read()), ensure_ascii=False, indent=2))
