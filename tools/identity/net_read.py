#!/usr/bin/env python3
"""Read one net over a date range with a model and answer a question about the people on it.

    python3 tools/identity/net_read.py --freq 430.725 --from 2026-09-20 --to 2026-09-26 \
        --ask "хто веде людей, хто не дійшов, чи є ім'я схоже на Вальщик або Мухомор"

Two passes. The first reads the traffic in windows of ten consecutive intercepts and writes down
facts; the second reads only those notes and answers the question. The split matters: a single
model call cannot hold a week of one net, and a per-intercept call cannot see that the man being
guided at 20:55 is the one who did not report in at 23:10.

Sources are ours first (Invisible Hand, AllInARow); Patagonia rows whose speech repeats one already
taken are dropped, the rest are kept and marked.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import difflib
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CORPUS = "~/wa-monitor/messages.db"
BARE = Path("/tmp/krevetka-netread")
NO_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "Task"]
PRIMARY = ("Invisible Hand", "AllInARow", "All in a Row")
FREQ_LINE = re.compile(r"^\d{2,4}[.,]\d{2,4}$")
SPEECH_LINE = re.compile(r"^\s*(\d+\s*)?[–—-]\s")

READ = """Ти читаєш перехоплення радіомережі ПРОТИВНИКА (російські війська) і виписуєш ФАКТИ ПРО
ЛЮДЕЙ. Нічого не вигадуй: кожен факт мусить спиратися на дослівну фразу з мови.

Що виписати з цього вікна:
1. ПОЗИВНІ, які звучать, і як саме вжито - звертання, представлення («я Норд»), згадка.
   Пиши так, як почулося. Якщо той самий чоловік названий двома способами - скажи це.
2. ХТО ВЕДЕ - голос, що керує рухом інших: «починай рух», «прийми вправо», «проходь наскрізь».
   Це найцінніше. Дай позивний ведучого і фразу-доказ.
3. ХТО КОГО ВЕДЕ - пари «ведучий -> ведений», якщо видно.
4. ВТРАТИ ТА ЗНИКЛІ - хто не вийшов на зв'язок, кого шукають, хто 200 або 300, кого не дочекались.
5. ЗУСТРІЧІ - хто мав з кимось зустрітися, кого доганяють, кого чекають на точці.
6. НЕРОЗІБРАНІ ІМЕНА - місця, де самі абоненти перепитують ім'я («хто? не можу зрозуміти,
   повтори»). Це ознака позивного, який легко записати неправильно.

Відповідь - ТІЛЬКИ JSON:
{"callsigns": [{"name": "", "used": "", "quote": ""}],
 "leading": [{"who": "", "whom": "", "quote": ""}],
 "losses": [{"who": "", "what": "", "quote": ""}],
 "meetings": [{"who": "", "what": "", "quote": ""}],
 "unclear_names": [{"heard": "", "quote": ""}]}
Порожні списки - нормальна відповідь."""

SYNTH = """Тобі дають виписані факти з тижня роботи ОДНІЄЇ радіомережі противника, у хронології.
Дай відповідь на питання користувача - по-людськи, українською, стисло і БЕЗ ВИГАДОК.

Правила:
- спирайся лише на подані факти; чого в них нема - того не стверджуй;
- якщо на питання нема відповіді - скажи це прямо, це нормальний результат;
- імена пиши так, як вони звучать у фактах, і назви різні написання того самого чоловіка;
- де можеш, наводь дослівну фразу як доказ;
- окремо виділи ВЕДУЧИХ: хто керує рухом людей - це найцінніше."""


def speech(t: str) -> str:
    return "\n".join(l for l in t.splitlines() if SPEECH_LINE.match(l)).strip()


def freq_of(t: str) -> str:
    for l in (x.strip() for x in t.splitlines()):
        if FREQ_LINE.match(l):
            return l.replace(",", ".")
    return "?"


def norm(s: str) -> str:
    return re.sub(r"[^а-яіїєґa-z0-9]+", "", s.lower())


def ask(system: str, material: str, model: str, effort: str) -> dict:
    BARE.mkdir(parents=True, exist_ok=True)
    cmd = ["claude", "-p", "--model", model, "--system-prompt", system,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--disallowedTools", *NO_TOOLS]
    if effort:
        cmd += ["--effort", effort]
    try:
        p = subprocess.run(cmd, input=material, capture_output=True, text=True,
                           timeout=600, cwd=str(BARE))
        return {"raw": p.stdout}
    except Exception as e:  # noqa: BLE001
        return {"raw": "", "error": f"{type(e).__name__}: {e}"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", required=True, help="префікс частоти, напр. 430.725")
    ap.add_argument("--from", dest="dfrom", required=True)
    ap.add_argument("--to", dest="dto", required=True)
    ap.add_argument("--ask", required=True)
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--size", type=int, default=10)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    lo = int(datetime.strptime(a.dfrom, "%Y-%m-%d").timestamp() * 1000)
    hi = int((datetime.strptime(a.dto, "%Y-%m-%d") + timedelta(days=1)).timestamp() * 1000)
    con = sqlite3.connect(f"file:{CORPUS}?mode=ro", uri=True)
    raw = con.execute("SELECT timestamp, group_name, coalesce(text,'') FROM messages "
                      "WHERE timestamp BETWEEN ? AND ? AND coalesce(category,'') != 'transfer'",
                      (lo, hi)).fetchall()
    con.close()

    rows = [{"ts": ts, "src": g, "text": t, "speech": speech(t)}
            for ts, g, t in raw if freq_of(t).startswith(a.freq) and speech(t)]
    rows.sort(key=lambda r: (0 if r["src"] in PRIMARY else 1, r["ts"]))
    kept: list[dict] = []
    for r in rows:
        n = norm(r["speech"])
        if any(abs(k["ts"] - r["ts"]) < 15 * 60 * 1000
               and difflib.SequenceMatcher(None, n[:500], norm(k["speech"])[:500]).ratio() > 0.82
               for k in kept):
            continue
        kept.append(r)
    kept.sort(key=lambda r: r["ts"])
    print(f"частота {a.freq}*, {a.dfrom}..{a.dto}: {len(rows)} -> {len(kept)} після дедупу",
          file=sys.stderr)
    if not kept:
        sys.exit("нічого читати")

    wins = [kept[i:i + a.size] for i in range(0, len(kept), a.size)]
    print(f"вікон {len(wins)}, модель {a.model}", file=sys.stderr)

    notes: list[str | None] = [None] * len(wins)
    t0 = time.time()

    def one(i: int) -> None:
        mat = "\n\n---\n\n".join(
            f"{datetime.fromtimestamp(r['ts']/1000).strftime('%d.%m %H:%M')} [{r['src']}]\n"
            + r["speech"][:2200] for r in wins[i])
        notes[i] = ask(READ, "ВІКНО ПЕРЕХОПЛЕНЬ:\n\n" + mat, a.model, a.effort)["raw"]
        if (i + 1) % 5 == 0:
            print(f"   {i+1}/{len(wins)}, {time.time()-t0:.0f}с", file=sys.stderr)

    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(one, range(len(wins))))

    facts = []
    for i, n in enumerate(notes):
        m = re.search(r"\{.*\}", n or "", re.S)
        if m:
            d0 = datetime.fromtimestamp(wins[i][0]["ts"] / 1000).strftime("%d.%m %H:%M")
            facts.append(f"--- вікно {i+1}, з {d0} ---\n{m.group(0)}")
    print(f"вікон з фактами: {len(facts)} з {len(wins)}", file=sys.stderr)

    answer = ask(SYNTH, f"ПИТАННЯ: {a.ask}\n\nФАКТИ:\n\n" + "\n\n".join(facts)[:400000],
                 a.model, a.effort)["raw"]
    out = Path(a.out) if a.out else (REPO / "knowledge" / "upstream" / "callsigns" /
                                     f"NET_{a.freq}_{a.dfrom}_{a.dto}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"# Мережа {a.freq}* · {a.dfrom} .. {a.dto}\n\n"
                   f"Питання: {a.ask}\n\nПерехоплень {len(kept)}, вікон {len(wins)}\n\n"
                   f"{answer}\n\n---\n\n## Сирі факти\n\n" + "\n\n".join(facts),
                   encoding="utf-8")
    print(answer)
    print(out)


if __name__ == "__main__":
    main()
