#!/usr/bin/env python3
"""Read the screen's candidates and say, per intercept, WHO directs and WHO is directed.

    python3 tools/identity/guide_verdicts.py --screen knowledge/upstream/callsigns/MOVEMENT_SCREEN_2d.json
    python3 tools/identity/guide_verdicts.py --days 3            # runs the screen itself first

Second half of the pair. `movement_screen.py` is mechanical and cheap: it narrows thousands of
intercepts to the few hundred that carry movement words. This step is the expensive one and it only
ever sees what the screen handed over — the same division of labour that makes the daily report
affordable: machinery selects, the model reads.

What it must NOT do is hand back a verdict about a PERSON. One intercept is one moment: «иди к ней,
я тебя встречу» says somebody is guiding somebody here, not that he is a handler. The verdict about
a man is made by COUNTING these moments across days, exactly as the dossier does — and that is a
separate step again, deliberately, because a man who guides once is a man who guided once.

Output per intercept: `guides` (callsign doing the directing), `guided` (who is being moved),
`quote` (their words, verbatim), `kind`, `confidence`. Anything the model cannot pin to a callsign
comes back with a null and is kept — an unattributed piece of guidance is still evidence that the
net carries guidance, and those are the nets worth watching.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT_DIR = REPO / "knowledge" / "upstream" / "callsigns"
BATCH = 12                      # intercepts per model call — small enough to keep quotes honest

SYS = """Ти читаєш перехоплення радіообміну противника (рос. мова) і визначаєш ОДНЕ:
чи КЕРУЄ тут хтось чиїмось пересуванням, і ХТО КИМ.

Керування рухом - це коли одна людина каже іншій, КУДИ, ЯК або КОЛИ рухатись, або супроводжує її
рух (бачить її, підсвічує, орієнтує голосом, зустрічає). Не є керуванням: доповідь про власне
пересування без вказівок, обговорення планів, загальна балаканина, наказ на вогонь.

НАПРЯМОК ВИЗНАЧАЄТЬСЯ ТІЛЬКИ ЗА МОВОЮ. Список станцій над перехопленням - це СЛОВНИК допустимих
позивних, а не вказівка, хто кому говорить. Його склав оператор, і він БУВАЄ ПЕРЕВЕРНУТИЙ: 22.09
мова МОТОМОТО була підписана як мова НИНДЗЯ, і саме так пішло в наш аналіз. Порядок станцій не
означає нічого.

Як читати напрямок з мови:
  - хто каже «двигайся», «иди», «стой», «левее» - той КЕРУЄ;
  - хто відповідає «принял», «понял», «иду», «я на месте» - того ВЕДУТЬ;
  - звертання на початку репліки називає ТОГО, ДО КОГО говорять: «Ниндзя, двигаешся правильно» -
    отже НИНДЗЯ ведений, а керує той, хто це сказав;
  - «Я <позивний>» називає того, хто говорить.

На кожне перехоплення поверни об'єкт:
  id       - ідентифікатор, який тобі дали
  is_guide - true/false: чи є тут керування рухом
  guides   - позивний того, хто КЕРУЄ (рядок) або null, якщо не встановлено
  guided   - позивний того, КИМ керують (рядок) або null
  quote    - ДОСЛІВНА цитата з мови, що доводить керування (до 200 знаків)
  kind     - одне з: напрямок | темп | зупинка | супровід | маршрут | інше
  confidence - 0.0-1.0
  station_conflict - true, якщо за мовою напрямок ВИХОДИТЬ ІНШИМ, ніж підказує порядок станцій
                     (тобто оператор, схоже, підписав перехоплення навпаки); інакше false

ЗАЛІЗНІ ПРАВИЛА:
1. Позивний бери ЛИШЕ зі списку станцій або з самої мови. НЕ вигадуй.
2. Якщо чути керування, але з мови незрозуміло хто кому - is_guide=true, guides/guided=null.
   Це нормальна і корисна відповідь. НЕ вгадуй за порядком станцій.
3. quote мусить дослівно бути в тексті. Без цитати - is_guide=false.
3a. «НВ» у списку станцій означає НЕ ВСТАНОВЛЕНО - це не позивний. Ніколи не повертай його
    як guides чи guided; у такому разі став null.
4. Оцінюєш ЦЕЙ момент, а не людину. Не пиши висновків про те, хто командир.

Поверни JSON-масив і нічого більше."""


def _load_run():
    spec = importlib.util.spec_from_file_location("analytics2_run", REPO / "tools" / "analytics2" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PLACEHOLDER = {"НВ", "Н/В", "NV"}


def stations_of(text: str) -> list[str]:
    """The station line the collector puts above the speech — the closed vocabulary of callsigns."""
    names = re.findall(r"^[А-ЯЁЇІЄҐ][А-ЯЁЇІЄҐ0-9 \-]{1,28}$", text, re.M)
    out = []
    for n in names:
        n = n.strip()
        if 2 < len(n) < 30 and not re.search(r"УКХ|DMR|МСП|ОМСБР|МСБ|МСР|МСД|КОМЕНТАР", n, re.I):
            out += [p.strip() for p in n.split(",") if p.strip() and p.strip().upper() not in PLACEHOLDER]
    return list(dict.fromkeys(out))[:12]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screen", help="MOVEMENT_SCREEN_*.json; omit to build one now")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--group", default="PATAGONIA_GP")
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--limit", type=int, default=0, help="stop after N candidates (0 = all)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    if a.screen:
        data = json.loads(Path(a.screen).read_text())
    else:
        scr = OUT_DIR / f"MOVEMENT_SCREEN_{a.days}d.json"
        subprocess.run([sys.executable, str(HERE / "movement_screen.py"),
                        "--days", str(a.days), "--group", a.group, "--show", "0"], check=True)
        data = json.loads(scr.read_text())

    cands = data["candidates"]
    if a.limit:
        cands = cands[:a.limit]
    print(f"кандидатів на розбір: {len(cands)}   модель: {a.model} ({a.effort})", file=sys.stderr)

    M = _load_run()
    verdicts: list[dict] = []
    t0 = datetime.now()
    for i in range(0, len(cands), BATCH):
        chunk = cands[i:i + BATCH]
        body = []
        for c in chunk:
            body.append(f"--- id: {c['msg_id']}\nсловник позивних (порядок НІЧОГО не означає): "
                        f"{', '.join(stations_of(c['text'])) or '(не вказані)'}\n"
                        f"ознаки решета: {', '.join(c['signs'])}\n{c['text']}\n")
        try:
            raw, _dt = M.call_model(SYS, "\n".join(body), a.model, a.effort)
            got = json.loads(re.search(r"\[.*\]", raw, re.S).group(0))
        except Exception as e:
            print(f"  партія {i//BATCH+1}: збій ({e}) — пропущено", file=sys.stderr)
            continue
        by_id = {c["msg_id"]: c for c in chunk}
        for v in got:
            src = by_id.get(v.get("id"))
            if not src:
                continue
            q = (v.get("quote") or "").strip()
            # The one check code can make and the model cannot be trusted to: the quote must be
            # really there. A verdict resting on words nobody said is the failure mode we already
            # met in the report (ЧЕРЕП/МОРЯК, 22.09) and it is cheap to kill here.
            if v.get("is_guide") and q and q[:40].lower() not in src["text"].lower():
                v["is_guide"], v["quote_verified"] = False, False
            else:
                v["quote_verified"] = bool(q)
            # «НВ» is the collector's placeholder for an unidentified station, not a callsign —
            # the model handed it back six times in the first 24-candidate run. Same family as the
            # invented-entity failure: a label that looks like a name and names nobody.
            for k in ("guides", "guided"):
                if (v.get(k) or "").strip().upper() in ("НВ", "Н/В", "NV", "НЕ ВСТАНОВЛЕНО", "-"):
                    v[k] = None
            v["in_report"] = src.get("in_report")
            verdicts.append(v)
        done = min(i + BATCH, len(cands))
        print(f"  {done}/{len(cands)}  ({(datetime.now()-t0).seconds}s)", file=sys.stderr)

    conflicts = [v for v in verdicts if v.get("station_conflict")]
    if conflicts:
        print(f"\nперехоплень, де НАПРЯМОК СУПЕРЕЧИТЬ підпису оператора: {len(conflicts)}",
              file=sys.stderr)
        for v in conflicts[:8]:
            print(f"   {v.get('guides')} -> {v.get('guided')} | {(v.get('quote') or '')[:70]}",
                  file=sys.stderr)
    guides = [v for v in verdicts if v.get("is_guide")]
    named = [v for v in guides if v.get("guides")]
    print(f"\nрозібрано {len(verdicts)}; керування руху: {len(guides)}; "
          f"з названим ведучим: {len(named)}", file=sys.stderr)
    top = Counter(v["guides"] for v in named).most_common(15)
    for cs, n in top:
        print(f"   {cs:<14} {n}", file=sys.stderr)

    out = Path(a.out) if a.out else OUT_DIR / f"GUIDE_VERDICTS_{a.days}d.json"
    out.write_text(json.dumps({
        "built": datetime.now().isoformat(timespec="seconds"), "model": a.model,
        "source_screen": a.screen or str(scr), "counts": {
            "candidates": len(cands), "verdicts": len(verdicts),
            "is_guide": len(guides), "named": len(named),
            "station_conflict": len(conflicts)},
        "by_guide": dict(Counter(v["guides"] for v in named)),
        "verdicts": verdicts}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{out}", file=sys.stderr)


if __name__ == "__main__":
    main()
