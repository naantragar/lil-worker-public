#!/usr/bin/env python3
"""report_cleanup — the third pass over a built report: cut what does not earn its place.

Runs AFTER the audit fixes have landed, deliberately: it cleans the text that survived, not the
text the audit is about to change.

Two different problems live here, and only one of them is a judgement call.

**Slang in brackets is mechanical.** Every «term» a line puts in quotes has to appear in the speech
of that line's own intercepts. On 14.10.2026 three did not: «Колючий» (their own handler's callsign
pressed into service as the name of OUR aircraft), «синя» (an invented gloss for an unstable link)
and the «пятерка/бабка/пятнашка» exchange. No model is needed to catch that class, and I miss it
silently when reading - which is exactly why it belongs in code.

**Excess detail is a judgement, so the model only FLAGS it.** The owner's standard, in his words:
the report exists for confirmed movement, 200/300, enemy equipment, plans of assault actions, and
confirmed bad morale. It does not exist for who said what to whom, for first aid and tourniquets,
for pleading for help ("they always ask, if they are wounded"), or for a man's condition written
out at length. "Cannot walk" stays only when something precise sits under it.

**It only reports. It never edits the report.** Same discipline as `report_audit.py`: the verdicts
come back to me and I decide, line by line.

    python3 tools/upstream/report_cleanup.py 14.10 [--events <path>] [--model claude-opus-5-5]
                                                [--workers 6] [--limit N] [--no-model]

Writes `CLEANUP_<day>.md` next to the report. Stdlib only.

**Switching it off** is one file: `tools/upstream/cleanup_config.json` -> `{"enabled": false}`. The
script then does nothing and says so, and the cycle is exactly what it was before this pass
existed. Nothing else in the pipeline depends on it.

The model call here is line-only on purpose - no intercepts. The excess-detail question is about
what the LINE says, not about what the speech holds, so the prompt stays small and a full report
costs minutes rather than the audit's eight. Grounding is the mechanical pass's job.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "knowledge" / "upstream" / "reports_out"
CONFIG = Path(__file__).with_name("cleanup_config.json")

# A quoted term in a line must be heard in that line's own intercepts. These are the exceptions,
# and every one of them is a rule we chose on purpose, not a loophole:
# `rules.md` orders their «бабка»/«баба яга» to be written «Вампір», so the report's word will
# never equal the speech's word. Grow this map only for another rule of that kind.
# Keys are FOLDED (see _fold): «Вампір» folds to "вампир", and keying it any other way silently
# disables the exception — which it did on the first run, flagging every Vampire line in the report.
RENDERED = {
    "вампир": ["вампир", "бабк", "баба", "яга"],
}
# Codes like «55» or «11» are legend entries of the net block, not quotations of speech. The comma
# belongs in this class: a quoted coordinate PAIR («52 73 121, 67 20 519») is the one thing the
# report must never be made to doubt, and without the comma it fell through to the stem match and
# was reported as an invented gloss — a false alarm on 16.10.2026, the first day a real fire-mission
# coordinate stood in a line.
NUMERIC = re.compile(r"^[\d\s/,.-]+$")

# Colour the owner has asked to be gone. STRONG is what he named outright; WEAK is only counted,
# so a long tail of "somebody reported something" never drowns the real findings.
STRONG = [
    (r"надано\s+1\s+допомог|перш\w*\s+допомог|перев'яз\w+|джгут|жгут", "перша допомога / перев'язка"),
    (r"стан\s+(стабільн|задовільн)\w*", "оцінка стану без конкретики"),
    (r"прос\w+\s+(допомог\w+|евакуац\w+)", "прохання про допомогу"),
    (r"не\s+може\s+(йти|рухатись|рухатися|пересуват\w+|стати|піднятися|встати)", "«не може йти» - лише з конкретикою"),
    (r"кричав|панік\w+|вичитан\w+|лаяв|замовк", "емоційний колорит"),
    (r"розмову\s+відклад\w+|обіцяв\s+поговорити", "порожній обмін"),
]
WEAK = [
    (r"\bдоповів\b|\bдоповіли\b|\bпередав\s+доповідь", "хто кому доповів"),
    (r"уточнюва\w+|уточнюють|перепита\w+", "уточнення без факту"),
]
LONG_LINE = 230

PROMPT = """Ти чистиш ОДИН рядок розвідзвіту від зайвого. Не перевіряєш правду - тільки вагу.

РЯДОК ({time}):
{line}

Звіт існує заради: підтверджених переміщень (хто, куди, хто веде), 200/300 і самого факту
поранення, їхньої техніки та її руху, планів штурмових дій і наказів, підтвердженого поганого
психоемоційного стану, позицій, координат, орієнтирів, підрозділів і союзників, МТЗ.

Зайве - це:
- перша допомога, джгут, перев'язка, «надано допомогу»
- прохання про допомогу (поранений завжди просить - це не інформація)
- опис стану замість факту: «стан стабільний», «не може йти» без конкретики, «дуже погано»
- хто кому доповів чи передав, коли сам факт уже в рядку
- емоції: кричав, панікував, вичитаний за зв'язок
- повтор того, що в рядку вже сказано іншими словами

Якщо під «не може йти» стоїть конкретика (ампутація, уражена кінцівка, ноші) - це НЕ зайве.

Усе це стосується ЛИШЕ їхніх людей (РОВ). Якщо рядок про наших - в\с СОУ, полонений, наш
поранений - не ріж у ньому НІЧОГО: там кожна деталь стану має вагу, бо це доля нашої людини.

Відповідь рівно в три рядки, без вступу:
ВЕРДИКТ: чисто | зайве
ЗАЙВЕ: <дослівні фрагменти рядка через кому; якщо чисто - прочерк>
ЧОМУ: <один короткий рядок>"""


def enabled() -> bool:
    try:
        return bool(json.loads(CONFIG.read_text(encoding="utf-8")).get("enabled", True))
    except FileNotFoundError:
        return True
    except Exception as exc:                        # noqa: BLE001 — a broken config must not silently disable the pass
        print(f"cleanup_config.json не читається ({exc!r}) — вважаю увімкненим", file=sys.stderr)
        return True


def _fold(s: str) -> str:
    """One spelling for a word written in Russian and in Ukrainian. Python's lower() handles
    Cyrillic (SQLite's does not — that is the trap this deliberately avoids by matching here)."""
    t = (s or "").lower()
    for a, b in (("і", "и"), ("ї", "и"), ("ы", "и"), ("й", "и"), ("є", "е"),
                 ("э", "е"), ("ё", "е"), ("ґ", "г"), ("'", ""), ("ь", "")):
        t = t.replace(a, b)
    return t


def speech_of(src: dict, refs: list) -> str:
    out = []
    for r in refs or []:
        s = src.get(r) or {}
        out += (s.get("speech") or []) + (s.get("marks") or []) + (s.get("stations") or [])
    return _fold(" ".join(out))


def ungrounded(text: str, speech: str) -> list:
    """Quoted terms of the line that are nowhere in its own intercepts."""
    bad = []
    for term in re.findall(r"«([^»]{1,40})»", text or ""):
        if NUMERIC.match(term):
            continue
        folded = _fold(term)
        stems = RENDERED.get(folded)
        if stems is None:
            # The anchor of a multi-word term is its longest WORD, not its first one: «9 кнопки»
            # anchored on "9" matches nothing and flags a line whose speech plainly says кнопка.
            words = [w for w in re.findall(r"[^\W\d_]+", folded, re.U) if len(w) > 2] or [folded]
            word = max(words, key=len)
            # Two letters off the end, because the speech declines what the report quotes in the
            # nominative: «ямка» against «на ямке» was a false alarm on the first live run.
            stems = [word[: max(3, len(word) - 2)]]
        if not any(st and st in speech for st in stems):
            bad.append(term)
    return bad


def ask(model: str, prompt: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    try:
        p = subprocess.run(["claude", "-p", "--model", model, "--effort", "medium", prompt],
                           capture_output=True, text=True, timeout=120, env=env)
        return (p.stdout or p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return "ВЕРДИКТ: чисто\nЗАЙВЕ: -\nЧОМУ: (таймаут)"


def verdict_of(answer: str) -> str:
    m = re.search(r"ВЕРДИКТ:\s*(\S+)", answer)
    return (m.group(1) if m else "?").strip().lower()


def main() -> int:
    ap = argparse.ArgumentParser(description="third pass: cut excess detail and ungrounded glosses")
    ap.add_argument("day")
    ap.add_argument("--events")
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-model", action="store_true", help="mechanical pass only, no model calls")
    a = ap.parse_args()

    if not enabled():
        print(f"чистка вимкнена у {CONFIG.relative_to(REPO)} — нічого не роблю", file=sys.stderr)
        print("чистка вимкнена (cleanup_config.json: enabled=false)")
        return 0

    ev_path = Path(a.events) if a.events else OUT / f"ZVIT_{a.day}_events.json"
    events = json.loads(ev_path.read_text(encoding="utf-8"))
    src = json.loads((OUT / f"ZVIT_{a.day}_sources.json").read_text(encoding="utf-8"))
    todo = [e for e in events if e.get("_src_ref")]
    if a.limit:
        todo = todo[: a.limit]

    # --- step 1: mechanical
    gloss_hits, colour_hits, weak_hits, long_hits = [], [], [], []
    for e in todo:
        text = e.get("text") or ""
        bad = ungrounded(text, speech_of(src, e.get("_src_ref")))
        if bad:
            gloss_hits.append((e, bad))
        for pat, why in STRONG:
            if re.search(pat, text, re.I):
                colour_hits.append((e, why))
                break
        for pat, why in WEAK:
            if re.search(pat, text, re.I):
                weak_hits.append((e, why))
                break
        if len(text) > LONG_LINE:
            long_hits.append((e, len(text)))

    lines = [f"# крок 1. механічна чистка ZVIT_{a.day} (без моделі)", "",
             f"Рядків під перевіркою: {len(todo)}", ""]
    lines += ["## розшифровки в «лапках», яких немає в мові цього рядка", ""]
    if gloss_hits:
        lines += ["Це найдорожчий клас: вигадана розшифровка читається як факт.", ""]
        for e, bad in gloss_hits:
            lines += [f"- {e.get('time')} — {', '.join('«'+b+'»' for b in bad)}",
                      f"    {e.get('text','')[:160]}",
                      f"    перехоплення: {', '.join((e.get('_src_ref') or [])[:4])}"]
    else:
        lines += ["- чисто"]
    lines += ["", "## колорит стану та порожній обмін", ""]
    lines += [f"- {e.get('time')} — {why}\n    {e.get('text','')[:160]}" for e, why in colour_hits] or ["- чисто"]
    lines += ["", f"## слабкі прикмети (лише перелік, {len(weak_hits)})", ""]
    lines += [f"- {e.get('time')} — {why}" for e, why in weak_hits] or ["- немає"]
    lines += ["", f"## задовгі рядки (> {LONG_LINE} знаків)", ""]
    lines += [f"- {e.get('time')} — {n} знаків\n    {e.get('text','')[:160]}" for e, n in long_hits] or ["- немає"]
    lines += [""]

    # --- step 2: model, line only
    counts: dict = {}
    if not a.no_model:
        print(f"чистка {len(todo)} рядків, модель {a.model}, потоків {a.workers}", file=sys.stderr)

        def one(e: dict) -> tuple:
            return e, ask(a.model, PROMPT.format(time=e.get("time", ""), line=e.get("text", "")))

        results = []
        with cf.ThreadPoolExecutor(max_workers=a.workers) as pool:
            for e, answer in pool.map(one, todo):
                results.append((e, answer))
                print(".", end="", flush=True, file=sys.stderr)
        print("", file=sys.stderr)

        lines += [f"# крок 2. зайва деталізація — модель, {len(results)} рядків", "",
                  "Це пропозиція, а не вирок: ріжу я, і тільки те, що справді не несе факту.", ""]
        for e, answer in sorted(results, key=lambda r: 0 if verdict_of(r[1]).startswith("зайве") else 1):
            v = verdict_of(answer)
            counts[v] = counts.get(v, 0) + 1
            if not v.startswith("зайве"):
                continue
            lines += [f"## {e.get('time')} — {e.get('text','')[:160]}", ""]
            lines += [f"  {ln}" for ln in answer.splitlines() if ln.strip()]
            lines += [""]
    lines += ["## підсумок", "",
              f"- розшифровок без опори в мові: {len(gloss_hits)}",
              f"- рядків з колоритом стану: {len(colour_hits)}",
              f"- задовгих рядків: {len(long_hits)}"]
    lines += [f"- модель «{k}»: {v}" for k, v in sorted(counts.items())]

    dest = OUT / f"CLEANUP_{a.day}.md"
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:60]))
    print(f"\nзбережено: {dest.relative_to(REPO)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
