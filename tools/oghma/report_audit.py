#!/usr/bin/env python3
"""report_audit — a second, independent model pass over a BUILT report, line by line.

The step after my own A/B/C recheck, and deliberately not the same work. My recheck is a risk
triage: I pick the lines that look dangerous and read those. It is blind where it does not look —
on 04.10.2026 a line invented a radio channel «6 больших» (the speech said «выйди в 6 больших», i.e.
come on the air at six; `большая` is their word for an hour) and it sailed through, because a
channel switch is the most boring line class in the report.

So this pass has no triage at all. Every line that rests on at least one intercept is handed to a
fresh model together with ITS OWN INTERCEPTS AND NOTHING ELSE, and the only question asked is: does
this line carry anything the speech does not? No report around it, no neighbouring lines, no day
context — so there is nothing to rationalise from.

The input is small because the corpus was already cut: a line plus a handful of intercepts, not the
day. That is what makes a second pass cheap enough to be routine.

**It only reports. It never edits the report.** A second opinion that can edit is a second source of
invention; the verdicts come back to me and I decide.

    python3 tools/upstream/report_audit.py 05.10 [--events <path>] [--model claude-sonnet-5]
                                              [--workers 6] [--limit N] [--flagged]

Writes `AUDIT_<day>.md` next to the report. Stdlib only.

Model choice is measured, not assumed (05.10.2026, on the three lines whose truth we already knew):
sonnet caught the «вантаж» invention and passed a line that said «наказ ... (ім добити)» where the
speech only said «ты знаешь что делать»; opus marked that one «не встановлено» and additionally
caught a plural I had missed myself («поранених» for one wounded man). So the default is opus — the
whole point of this pass is the line nobody else questions. ~23 s per line, so a 117-line report is
about 8 minutes at six workers: durable-job work, not inline.
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

PROMPT = """Ти перевіряєш ОДИН рядок розвідзвіту проти перехоплень, на яких він стоїть.

РЯДОК ({time}):
{line}

ПЕРЕХОПЛЕННЯ (це все, що є під цим рядком, іншого немає):
{speech}

Питання одне: чи є в рядку те, чого в цій мові немає?

Правила:
- Нічого не домислюй і не добирай з власних знань про війну.
- Якщо мова про деталь мовчить - це «не встановлено», а не «розходиться».
- «Розходиться» - лише коли рядок каже інше, ніж мова: не та людина, не та сторона, зворотний
  зміст, названа річ чи подія, якої в мові нема.
- Жаргон: «сброс» з важкого борта - це удар, не доставка; «малая» - хвилина, «большая» - година;
  «карандаш» - їхній піхотинець; «двухсотый»/«двадцатый» - загиблий.

Відповідь рівно в три рядки, без вступу:
ВЕРДИКТ: збігається | розходиться | не встановлено
НЕ ПІДТВЕРДЖЕНО: <слова рядка, яких нема в мові, через кому; якщо все підтверджено - прочерк>
ЦИТАТА: <один дослівний рядок з мови, який це доводить>"""


def speech_block(src: dict, refs: list) -> str:
    out = []
    for r in refs or []:
        s = src.get(r) or {}
        out.append(f"--- {r} · {s.get('net','')} · станції: {', '.join(s.get('stations') or [])}")
        for m in s.get("marks") or []:
            out.append(f"    мітка аналітика: {m}")
        for l in s.get("speech") or []:
            out.append(f"    {l}")
    return "\n".join(out) if out else "(немає)"


def ask(model: str, prompt: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    try:
        p = subprocess.run(["claude", "-p", "--model", model, prompt],
                           capture_output=True, text=True, timeout=180, env=env)
        return (p.stdout or p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return "ВЕРДИКТ: не встановлено\nНЕ ПІДТВЕРДЖЕНО: -\nЦИТАТА: (таймаут)"


def verdict_of(answer: str) -> str:
    m = re.search(r"ВЕРДИКТ:\s*(\S+)", answer)
    return (m.group(1) if m else "?").strip().lower()


def main() -> int:
    ap = argparse.ArgumentParser(description="second model pass over a built report")
    ap.add_argument("day")
    ap.add_argument("--events")
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="audit only the first N lines (testing)")
    ap.add_argument("--flagged", action="store_true",
                    help="audit only what report_check flagged, instead of every line")
    a = ap.parse_args()

    ev_path = Path(a.events) if a.events else OUT / f"ZVIT_{a.day}_events.json"
    events = json.loads(ev_path.read_text(encoding="utf-8"))
    src = json.loads((OUT / f"ZVIT_{a.day}_sources.json").read_text(encoding="utf-8"))

    todo = [e for e in events if e.get("_src_ref")]
    if a.flagged:
        check = OUT / f"CHECK_{a.day}.md"
        if check.exists():
            marked = set(re.findall(r"^## \[\d+\] (.+?) — ", check.read_text(encoding="utf-8"), re.M))
            todo = [e for e in todo if e.get("time") in marked]
    if a.limit:
        todo = todo[: a.limit]

    # The mechanical pass runs FIRST and its findings head the file. They need no intercept and no
    # model — an impossible pair like "супровід БпЛА СОУ" is a line contradicting itself — so they
    # are fixed, not read. Keeping the two in one job means one step after the A/B/C pass, not two.
    pre = ""
    try:
        cmd = [sys.executable, str(Path(__file__).with_name("report_check.py")), a.day, "--no-save"]
        if a.events:
            cmd += ["--events", a.events]
        pre = subprocess.run(cmd, capture_output=True, text=True, timeout=900).stdout
    except Exception as e:                      # noqa: BLE001 — never lose the audit over the pre-pass
        pre = f"(механічна перевірка не вийшла: {e!r})"

    print(f"аудит {len(todo)} рядків, модель {a.model}, потоків {a.workers}", file=sys.stderr)

    def one(e: dict) -> tuple:
        prompt = PROMPT.format(time=e.get("time", ""), line=e.get("text", ""),
                               speech=speech_block(src, e.get("_src_ref")))
        return e, ask(a.model, prompt)

    results = []
    with cf.ThreadPoolExecutor(max_workers=a.workers) as pool:
        for e, answer in pool.map(one, todo):
            results.append((e, answer))
            print(".", end="", flush=True, file=sys.stderr)
    print("", file=sys.stderr)

    order = {"розходиться": 0, "не": 1, "збігається": 2, "?": 3}
    results.sort(key=lambda r: order.get(verdict_of(r[1])[:10], 3))

    lines = ["# крок 1. механічна перевірка (без моделі)", "",
             pre.strip(), "",
             f"# крок 2. аудит ZVIT_{a.day} — друга модель, {len(results)} рядків",
             "",
             "Кожен рядок перевірявся ОКРЕМО, лише проти своїх перехоплень, без решти звіту.",
             "Це оцінка, а не вирок: правлю я, прочитавши перехоплення сам.",
             ""]
    counts: dict = {}
    for e, answer in results:
        v = verdict_of(answer)
        counts[v] = counts.get(v, 0) + 1
        if v.startswith("збігається"):
            continue
        lines += [f"## {e.get('time')} — {e.get('text','')[:160]}", ""]
        lines += [f"  {ln}" for ln in answer.splitlines() if ln.strip()]
        lines += [f"  перехоплення: {', '.join(e.get('_src_ref') or [])}", ""]
    lines += ["## підсумок", ""] + [f"- {k}: {v}" for k, v in sorted(counts.items())]

    dest = OUT / f"AUDIT_{a.day}.md"
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:60]))
    print(f"\nзбережено: {dest.relative_to(REPO)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
