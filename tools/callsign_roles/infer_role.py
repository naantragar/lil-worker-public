#!/usr/bin/env python3
"""Read one callsign's own traffic and propose what he is - or abstain.

    python3 tools/callsign_roles/infer_role.py --only КАЩЕЙ БЕРЕГ --days 14

One callsign per model call, deliberately: a batch invites the model to make the answers look
consistent with each other instead of each one with its own evidence.

The prompt carries the analyst's own vocabulary, taken from the gold set rather than invented, so
what comes back is in his language. `basis` is mandatory - a verdict whose support cannot be traced
to the speech is discarded by the harness even when it happens to be right.
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "tools" / "analytics2"))
import run as A                                     # noqa: E402  model call + corpus door
import gather_evidence as G                         # noqa: E402
from build_gold import is_hedge, classes_of      # noqa: E402  one classifier, not two

GOLD = HERE / "gold.json"
EVIDENCE_FLOOR = 3          # fewer intercepts than this and the model is not asked at all


def vocabulary(gold: dict, top: int = 24) -> list[str]:
    """The analyst's own wording, most used first. Not a closed list - the tail is real."""
    c = Counter()
    for rec in gold["gold"].values():
        for r in rec["roles"]:
            c[r] += 1
    return [w for w, _ in c.most_common(top)]


SYSTEM = """Ти визначаєш РОЛЬ позивного в радіомережі противника за його ж ефіром.

Тобі дають перехоплення одного позивного за кілька діб, розділені на дві частини:
[ВІН У СТАНЦІЯХ] - він сам говорить або до нього звертаються;
[ЗГАДАНИЙ ІНШИМИ] - про нього говорять третім особам.

Друга частина зазвичай каже більше за першу. Хто ДОПОВІДАЄ комусь - той не старший; кому доповідають
- старший. Кого просять привезти - логістика. Повз чию позицію ходять - це місце, а не людина.

ГОЛОВНЕ ПРАВИЛО, і на ньому ми вже помилилися 19 разів поспіль:
ПОСАДА ВАЖЛИВІША ЗА ІНСТРУМЕНТ. Дрон, рація, машина - це те, ЧИМ людина працює, а не те, ХТО вона.

- Той, хто ГОЛОСОМ ВЕДЕ ЛЮДЕЙ по місцевості («прими левее», «займи укриття», «двигайся до полки»),
  ставить їм задачі і приймає від них доповіді - це КОМАНДНИЙ СКЛАД. Навіть якщо він робить це,
  дивлячись з дрона, і навіть якщо половина його ефіру - про політ цього дрона.
- РОЗРАХУНОК БпЛА - це той, хто ТІЛЬКИ пілотує і доповідає побачене старшому. Він не розпоряджається
  людьми на землі.
- Так само: не «водій» лише тому, що згадав машину; не «зв'язок» лише тому, що налаштовує антену.

Перевірка одним питанням: якщо прибрати з ефіру весь технічний бік (політ, заряджання, антени) -
чи лишиться людина, яка кимось РОЗПОРЯДЖАЄТЬСЯ? Якщо так, це командний склад.

ВІДПОВІДАЙ ОДНИМ JSON-ОБ'ЄКТОМ, без тексту навколо:
{"role": "...", "confidence": 0.0, "basis": "...", "abstain": false}

Правила:
- role - ОДНА роль, словами аналітика зі списку нижче, якщо хоч один підходить. Свої слова - лише
  коли жоден не лягає.
- НЕ СКЛЕЮЙ ДВІ РОЛІ ЧЕРЕЗ КОМУ. «мол ком склад, проміжний накопичувач» - це не відповідь, це
  небажання обрати. Якщо вагаєшся між двома ролями - обирай ту, що підпирається сильніше, і знижуй
  confidence; якщо жодна не переважає - утримайся. Уточнення до ОДНІЄЇ ролі комою можна
  («ком склад, супровід шг» - це одна роль з уточненням), друга роль комою - ні.
- Роль визначає ФУНКЦІЯ в обміні, а не тон, не лайка і не кількість сказаного. Балакучий - не роль.
- basis ОБОВ'ЯЗКОВИЙ: коротко, що саме в мові це підпирає, з посиланням на позивних співрозмовників.
  Без простежуваного обґрунтування відповідь буде відкинута, навіть якщо вона правильна.
- confidence - чесно. 0.9 лише коли функція видно прямо і неодноразово; 0.5 коли це найкраще
  пояснення з кількох; нижче 0.4 - краще abstain.
- abstain: true - повноцінна відповідь. Якщо з мови функція не видно, утримайся. Мовчання коштує
  дешевше за здогадку.
- Не вигадуй підрозділ, номер частини, посаду за званням. Тільки роль у мережі.

Словник аналітика (найчастіше вживані формулювання):
"""


def build_prompt(gold: dict) -> str:
    return SYSTEM + "\n".join(f"  {w}" for w in vocabulary(gold))


def parse_verdict(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    d["role"] = " ".join(str(d.get("role") or "").split())
    d["basis"] = " ".join(str(d.get("basis") or "").split())
    try:
        d["confidence"] = float(d.get("confidence") or 0)
    except (TypeError, ValueError):
        d["confidence"] = 0.0
    d["abstain"] = bool(d.get("abstain")) or not d["role"]
    # harness rules, applied here so they can never be forgotten downstream
    if not d["abstain"] and len(d["basis"]) < 15:
        d["abstain"] = True
        d["dropped"] = "basis відсутній або порожній"
    # Two substantive classes welded with a comma is a refusal to choose, not a reading. It is
    # caught in code as well as in the prompt, because a hedge scores as "correct" against either
    # class and would quietly inflate every accuracy number in the calibration.
    if not d["abstain"] and is_hedge(d["role"]):
        d["hedged"] = sorted(classes_of(d["role"]))
        d["confidence"] = min(d["confidence"], 0.45)
        d["abstain"] = True
        d["dropped"] = "склейка двох ролей: " + ", ".join(d["hedged"])
    return d


def infer_one(name: str, ev: dict, system: str, model: str, effort: str) -> dict:
    total = ev["n_spoke"] + ev["n_mentioned"]
    if total < EVIDENCE_FLOOR:
        return {"callsign": name, "abstain": True, "role": "", "confidence": 0.0,
                "basis": "", "skipped": f"матеріалу {total} < {EVIDENCE_FLOOR}",
                "evidence_n": total, "evidence_days": len(ev["days"])}
    raw, dt = A.call_model(system, G.as_material(ev), model, effort)
    v = parse_verdict(raw) or {"abstain": True, "role": "", "confidence": 0.0,
                               "basis": "", "dropped": "модель не повернула JSON"}
    v.update({"callsign": name, "evidence_n": total, "evidence_days": len(ev["days"]),
              "freqs": ev["freqs"], "seconds": round(dt, 1), "model": model})
    return v


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--to", dest="dt_to", default=None)
    ap.add_argument("--only", nargs="*", help="these callsigns (default: candidates with no role)")
    ap.add_argument("--gold-set", action="store_true",
                    help="run over the callsigns the analyst HAS described (calibration)")
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("-o", "--out", default=str(HERE / "verdicts.json"))
    args = ap.parse_args()

    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    if args.only:
        names = [n.upper() for n in args.only]
    elif args.gold_set:
        names = sorted(gold["gold"])
    else:
        names = sorted(gold["unlabelled"])

    system = build_prompt(gold)
    recs, prepared, lo, hi = G.collect(args.days, args.dt_to)
    print(f"вікно {lo:%d.%m} - {hi:%d.%m}: {len(recs)} перехоплень, {len(names)} позивних",
          file=sys.stderr)

    out = []
    for i, n in enumerate(names, 1):
        ev = G.evidence_for(n, prepared)
        v = infer_one(n, ev, system, args.model, args.effort)
        v["gold_roles"] = gold["gold"].get(n, {}).get("roles", [])
        out.append(v)
        tag = ("пропуск" if v.get("skipped") else
               "утримався" if v["abstain"] else f"{v['role']}  ({v['confidence']:.2f})")
        print(f"  [{i:>3}/{len(names)}] {n:<20} n={v['evidence_n']:<4} {tag}", file=sys.stderr)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    asked = [v for v in out if not v.get("skipped")]
    answered = [v for v in asked if not v["abstain"]]
    print(f"\nзапитано {len(asked)} з {len(out)}, відповіли {len(answered)}, "
          f"утримались {len(asked) - len(answered)}", file=sys.stderr)
    print(f"записано: {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
