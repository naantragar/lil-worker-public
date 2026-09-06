#!/usr/bin/env python3
"""Score the verdicts against what the live analyst actually wrote.

    python3 tools/callsign_roles/calibrate.py [verdicts.json]

Reads a verdicts file produced by `infer_role.py --gold-set` and prints, per confidence band:

  * coverage      - of the callsigns we were ASKED about, how many we answered at all
  * accuracy      - of the answered, how many match ANY reading the analyst recorded for that man
  * the dangerous cell - confident AND wrong, listed individually, in full, always

Matching is by class first (`ком склад` / `мол ком склад` / `кр` are one thing) and by exact
wording second. The class number is the honest one; the wording number says how close we are to
writing text he would have written himself.

The output is one decision for the owner: the confidence threshold above which a proposal is worth
printing at all.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from build_gold import classify, classes_of, is_hedge   # noqa: E402  one classifier

BANDS = [(0.85, 1.01, "0.85+"), (0.7, 0.85, "0.70-0.84"),
         (0.55, 0.7, "0.55-0.69"), (0.0, 0.55, "<0.55")]


def band_of(c: float) -> str:
    for lo, hi, name in BANDS:
        if lo <= c < hi:
            return name
    return "<0.55"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("verdicts", nargs="?", default=str(HERE / "verdicts.json"))
    ap.add_argument("--out", default=str(HERE / "calibration.txt"))
    args = ap.parse_args()

    data = json.loads(Path(args.verdicts).read_text(encoding="utf-8"))
    graded = [v for v in data if v.get("gold_roles")]
    if not graded:
        sys.exit("у файлі немає жодного позивного з еталонною роллю - нічого калібрувати")

    L = []
    def say(s=""):
        L.append(s)
        print(s)

    skipped = [v for v in graded if v.get("skipped")]
    asked = [v for v in graded if not v.get("skipped")]
    answered = [v for v in asked if not v["abstain"]]

    say(f"КАЛІБРУВАННЯ на еталоні аналітика")
    say(f"  позивних з еталоном у файлі : {len(graded)}")
    say(f"  не питали (мало матеріалу)  : {len(skipped)}")
    say(f"  питали                      : {len(asked)}")
    say(f"  відповіли                   : {len(answered)}")
    say(f"  утримались                  : {len(asked) - len(answered)}")
    hedged = [v for v in answered if is_hedge(v["role"])]
    if hedged:
        say(f"  З НИХ СКЛЕЙКИ (дві ролі)    : {len(hedged)}  <- не читання, а небажання обрати;")
        say(f"                                 склейка збігається з будь-яким класом і роздуває точність")
    say()

    for v in answered:
        # correct if OUR class is among the classes his readings cover. Sets, not single labels:
        # he may have described the man twice, and either reading counts.
        gold_cls = set().union(*(classes_of(r) for r in v["gold_roles"]))
        v["_cls_ok"] = bool(classes_of(v["role"]) & gold_cls)
        v["_exact_ok"] = any(v["role"].lower() == r.lower() for r in v["gold_roles"])
        v["_band"] = band_of(v["confidence"])

    say(f"{'смуга':<12} {'відпов':>7} {'клас +':>8} {'точність':>9} {'дослівно':>9}")
    for _lo, _hi, name in BANDS:
        b = [v for v in answered if v["_band"] == name]
        if not b:
            say(f"{name:<12} {'-':>7}")
            continue
        ok = sum(1 for v in b if v["_cls_ok"])
        ex = sum(1 for v in b if v["_exact_ok"])
        say(f"{name:<12} {len(b):>7} {ok:>8} {100*ok/len(b):>8.1f}% {100*ex/len(b):>8.1f}%")
    say()

    # покриття від усіх, кого питали
    ok_all = sum(1 for v in answered if v["_cls_ok"])
    say(f"загалом: покриття {100*len(answered)/max(len(asked),1):.1f}% від запитаних, "
        f"точність по класу {100*ok_all/max(len(answered),1):.1f}%")
    say()

    say("НЕБЕЗПЕЧНА КЛІТИНКА - упевнено (>=0.70) і НЕВІРНО:")
    bad = sorted([v for v in answered if v["confidence"] >= 0.70 and not v["_cls_ok"]],
                 key=lambda v: -v["confidence"])
    if not bad:
        say("  (порожньо)")
    for v in bad:
        say(f"  {v['callsign']:<18} conf={v['confidence']:.2f}  n={v['evidence_n']}")
        say(f"      ми:      {v['role']}   [{classify(v['role'])}]")
        say(f"      аналітик: {'; '.join(v['gold_roles'])}")
        say(f"      basis:   {v['basis'][:180]}")
    say()

    say("ЩО ВТРАЧАЄМО, УТРИМУЮЧИСЬ - позивні з еталоном, де ми змовчали:")
    silent = [v for v in asked if v["abstain"] and not v.get("skipped")]
    for v in silent[:20]:
        say(f"  {v['callsign']:<18} n={v['evidence_n']:<4} аналітик: {'; '.join(v['gold_roles'])}")
    if len(silent) > 20:
        say(f"  ... ще {len(silent)-20}")
    say()

    say("ПОМИЛКИ ПО КЛАСАХ (що з чим плутаємо):")
    conf = Counter()
    for v in answered:
        if not v["_cls_ok"]:
            want = sorted(set().union(*(classes_of(r) for r in v["gold_roles"])))
            conf[("+".join(want), classify(v["role"]))] += 1
    for (want, got), n in conf.most_common(10):
        say(f"  {n:>3}  аналітик «{want}» -> ми «{got}»")
    say()

    thr = None
    for _lo, _hi, name in BANDS:
        b = [v for v in answered if v["_band"] == name]
        if b and sum(1 for v in b if v["_cls_ok"]) / len(b) >= 0.85:
            thr = name
            break
    if hedged:
        clean = [v for v in answered if not is_hedge(v["role"])]
        ok_clean = sum(1 for v in clean if v["_cls_ok"])
        say(f"БЕЗ СКЛЕЙОК (чесне число): відповіли {len(clean)}, "
            f"точність по класу {100*ok_clean/max(len(clean),1):.1f}%")
        say("  склеєні: " + ", ".join(f"{v['callsign']}({v['role']})" for v in hedged[:12]))
        say()
    say(f"РЕКОМЕНДАЦІЯ: найвища смуга з точністю >=85% - {thr or 'жодна'}. "
        f"Поріг друку затверджує власник.")

    Path(args.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"\nзаписано: {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
