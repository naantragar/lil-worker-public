#!/usr/bin/env python3
"""Accumulate OBSERVED RELATIONS about each callsign, day by day, and never a role.

    python3 tools/callsign_roles/dossier.py            # fold every ZVIT_*_events.json found
    python3 tools/callsign_roles/dossier.py --show КАЩЕЙ БАЗА

Why this exists, and why it is not the role module over again:

Four attempts to read a role out of a 14-day snapshot of raw speech were each measured against the
analyst's own answer key and each refuted - accumulators speak as much as commanders, give MORE
orders than commanders, and are named as a destination LESS often. The label is not recoverable from
a snapshot. What IS recoverable, and what the analyst himself works from, is the pattern of
relations repeated over weeks.

So this file stores no verdicts. It stores what was seen, with the day it was seen on and the
intercept it came from:

    who was seen with whom, who was named as a destination, who was ordered by whom, on how many
    distinct days, on which nets

The report's `_events.json` is the input rather than raw speech, and that is the whole point: the
relations there are ALREADY extracted and normalised by the report pass. Measured on 29 days of
events, "named as a destination" separates the analyst's classes 11.1% vs 2.4%; the same feature
regexed out of raw speech gave 0.0% vs 0.0%. The distillation is the signal.

Three rules keep this honest:

  * **Confidence is COUNTED, not asserted.** The strength of a relation is the number of DISTINCT
    DAYS it was observed and the number of DISTINCT counterparts, never how many times one line
    repeats. A single loud day cannot make a fact.
  * **Every observation keeps its source.** Day, net and the event text it came from, so any entry
    can be walked back to the intercept behind it.
  * **Our own inference never reinforces itself.** These are relations lifted from event lines, not
    conclusions about people. If a report line was wrong, the dossier carries a wrong observation -
    traceable and correctable - not a wrong belief that grows with repetition.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
REPORTS = REPO / "knowledge" / "upstream" / "reports_out"
STORE = HERE / "dossier.json"

# A callsign in a report line is CAPS, per the report's own rule ("ВЕЛИКІ ЛІТЕРИ = позивний або
# орієнтир. НІЧОГО ІНШОГО"). Unit designations and the analyst's abbreviations are not people.
NAME = re.compile(r"(?<![А-ЯЄІЇҐA-Z])([А-ЯЄІЇҐ][А-ЯЄІЇҐ0-9\-]{2,})(?![а-яєіїґa-z])")
NOT_A_NAME = {
    "БПЛА", "СОУ", "ЗСУ", "МТЗ", "ФПВ", "ВУ", "КНП", "СП", "ТМ", "РЕБ", "ДРГ", "БК",
    "ОМСБР", "МСП", "МСД", "МСБ", "МСР", "ОМБР", "ШГ", "ШР", "ШЗ", "ГР", "УКХ",
    "GPS", "DMR", "FPV", "ЦФП",
}
DEST = re.compile(r"(?:до|на|у)\s+(?:т\s*|укритт\w*\s+|позиці\w*\s+|ор\s+|н\\?п\s+|р-ну?\s+)?"
                  r"«?([А-ЯЄІЇҐ][А-ЯЄІЇҐ0-9\-]{2,})»?")
ORDER = re.compile(r"наказ\s+«?([А-ЯЄІЇҐ][А-ЯЄІЇҐ0-9\-]{2,})»?\s+(?:в\\?с\s+|гр\s+|о\\?с\s+)?"
                   r"«?([А-ЯЄІЇҐ][А-ЯЄІЇҐ0-9\-]{2,})»?")
ESCORT = re.compile(r"у супроводі\s+(?:БпЛА\s+)?«?([А-ЯЄІЇҐ][А-ЯЄІЇҐ0-9\-]{2,})»?")


def names_in(text: str) -> list[str]:
    return [n for n in NAME.findall(text or "") if n not in NOT_A_NAME and not n.isdigit()]


def blank():
    return {"days": set(), "nets": set(), "seen": 0,
            "as_destination": defaultdict(set),   # day -> event refs
            "ordered_by": defaultdict(set),       # who -> days
            "ordered": defaultdict(set),
            "escorted_by": defaultdict(set),
            "with": defaultdict(set),             # co-occurrence -> days
            "samples": []}


def fold(events: list, day: str, store: dict):
    for e in events:
        text = e.get("text") or ""
        net = " ".join(str(e.get("_net") or "").split())[:80]
        when = (e.get("time") or day)[:10]
        people = names_in(text)
        if not people:
            continue
        for n in set(people):
            d = store.setdefault(n, blank())
            d["days"].add(when)
            d["seen"] += 1
            if net:
                d["nets"].add(net)
            for other in set(people):
                if other != n:
                    d["with"][other].add(when)
            if len(d["samples"]) < 12:
                d["samples"].append({"day": when, "text": text[:150],
                                     "src": (e.get("_src_ref") or [None])[0]})
        for m in DEST.finditer(text):
            t = m.group(1)
            if t in NOT_A_NAME:
                continue
            store.setdefault(t, blank())["as_destination"][when].add(text[:100])
        for m in ORDER.finditer(text):
            giver, taker = m.group(1), m.group(2)
            if giver in NOT_A_NAME or taker in NOT_A_NAME:
                continue
            store.setdefault(giver, blank())["ordered"][taker].add(when)
            store.setdefault(taker, blank())["ordered_by"][giver].add(when)
        for m in ESCORT.finditer(text):
            esc = m.group(1)
            if esc in NOT_A_NAME:
                continue
            for n in set(people):
                if n != esc:
                    store.setdefault(n, blank())["escorted_by"][esc].add(when)


def add_air(store: dict, days: int, dt_to: str | None):
    """Attach how much AIR each callsign has, straight from the corpus.

    Not decoration - a correction for a bias measured on the answer key. Events per 1000 intercepts:

        накопичувач      218      an accumulator is where people GO, so he is written into the line
        командний склад   34      a commander is the man TALKING ABOUT somebody else's move
        БпЛА              28

    Of the twelve loudest callsigns with zero events, TEN are command staff by the analyst's own
    hand (ГРАНИТ 52 intercepts / 0 events, БЕРКУТ 50/0, ХОРОШИЙ 48/0, two company commanders among
    them). Judging a man by his event count therefore discards exactly the people the register
    exists for. The air counter is what keeps them visible while their relations accumulate.
    """
    sys.path.insert(0, str(HERE))
    import gather_evidence as G
    recs, prepared, lo, hi = G.collect(days, dt_to)

    # SEED from the register before counting. A card that only exists because somebody appeared in
    # an event would miss ГРАНИТ entirely - 52 intercepts, zero events, and `ком склад` in the
    # analyst's own hand. He is exactly the man this counter was added to keep. Every callsign the
    # analyst has ever written gets a card, even an empty one; the air number then says whether it
    # is empty because he is silent or because he never earns a line.
    try:
        gold = json.loads((HERE / "gold.json").read_text(encoding="utf-8"))
        for n in list(gold.get("gold", {})) + list(gold.get("unlabelled", {})):
            store.setdefault(n, blank())
    except FileNotFoundError:
        pass

    for n, d in store.items():
        ev = G.evidence_for(n, prepared)
        d["air_n"] = ev["n_spoke"] + ev["n_mentioned"]
        d["air_spoke"] = ev["n_spoke"]
        d["air_days"] = len(ev["days"])
        d["air_freqs"] = ev["freqs"][:6]
    return len(recs), lo, hi


# A relation is ESTABLISHED when it has been seen on this many distinct days AND with this many
# distinct counterparts. The threshold is on INDEPENDENT observations, never on a model's
# confidence: the whole point of the dossier is that nothing here is asserted, only counted. One
# loud night with one talkative partner is not a fact about a man.
MIN_DAYS = 3
MIN_PARTNERS = 2


def looks_like_a_place(rec: dict) -> str | None:
    """Landmark or man? The discriminator is the RATIO, not either count on its own.

    Found 2026-09-05, and it is the answer to a question four separate measurements missed - because
    each of them looked at one number. A place is WRITTEN INTO report lines constantly (people are
    sent to it) and is SILENT on the air (it has no radio). A man is the other way round.

        ЗАЛІЗНИЧНЕ  events/air 26.0   air 0     a settlement
        ПОГРЕБ                  5.25  air 4
        ХУРМА                   4.75  air 4
        ИЗЮМ                    3.88  air 16    orientirs: fruit, berries, a cellar
        ---------------------------------------------------------------- 1.0
        ПУХ                     0.41  air 59
        БУРЫЙ                   0.35  air 97    in the analyst's register
        ДОН                     0.34  air 121   in the analyst's register

    The gap is an order of magnitude and it is clean. Both names the analyst himself put in the
    register sit on the human side. So the flag is advisory, not a filter: it is printed, and a card
    it fires on is not offered as a person until somebody looks.
    """
    air = rec.get("air_n")
    if air is None or rec["n_events"] < 4:
        return None
    ratio = rec["n_events"] / max(air, 1)
    if ratio >= 2.0 and air <= 20:
        return f"ІМОВІРНО ОРІЄНТИР, не людина (подій {rec['n_events']}, ефіру лише {air})"
    if ratio >= 1.0:
        return f"можливо орієнтир (подій/ефір = {ratio:.1f})"
    return None


def established(rec: dict) -> dict:
    """What this card actually supports, and what it merely hints at.

    Returns the relations that clear the threshold, each with the count that earned it. Everything
    below stays in the card - visible, waiting for more days - but is not called established.
    """
    out = {"ready": False, "why": [], "pending": []}

    def check(kind, mapping, need_partners=MIN_PARTNERS):
        strong = {k: v for k, v in mapping.items() if v >= MIN_DAYS}
        if len(strong) >= need_partners:
            out["why"].append(f"{kind}: " + ", ".join(f"{k} ({v} діб)" for k, v in strong.items()))
            return True
        weak = {k: v for k, v in mapping.items() if v}
        if weak:
            out["pending"].append(f"{kind}: " + ", ".join(f"{k} ({v})" for k, v in
                                                         list(weak.items())[:5]))
        return False

    ok = False
    ok |= check("ставив задачі", rec.get("ordered_days") or {})
    ok |= check("отримував наказ", rec.get("ordered_by_days") or {})
    # a destination needs no counterparts - being the place people are sent to IS the relation
    if (rec.get("as_destination_days") or 0) >= MIN_DAYS:
        out["why"].append(f"був пунктом призначення: {rec['as_destination_days']} діб")
        ok = True
    elif rec.get("as_destination_days"):
        out["pending"].append(f"пункт призначення: {rec['as_destination_days']} діб")
    out["ready"] = ok
    return out


def serialise(store: dict) -> dict:
    out = {}
    for n, d in store.items():
        out[n] = {
            "callsign": n,
            "days_seen": sorted(d["days"]),
            "n_days": len(d["days"]),
            "n_events": d["seen"],
            "nets": sorted(d["nets"])[:6],
            # counted in DISTINCT DAYS, never in repetitions of one line
            "as_destination_days": len(d["as_destination"]),
            "ordered_days": {k: len(v) for k, v in sorted(
                d["ordered"].items(), key=lambda kv: -len(kv[1]))[:10]},
            "ordered_by_days": {k: len(v) for k, v in sorted(
                d["ordered_by"].items(), key=lambda kv: -len(kv[1]))[:10]},
            "escorted_by_days": {k: len(v) for k, v in sorted(
                d["escorted_by"].items(), key=lambda kv: -len(kv[1]))[:6]},
            "with_days": {k: len(v) for k, v in sorted(
                d["with"].items(), key=lambda kv: -len(kv[1]))[:12]},
            "samples": d["samples"],
            # from the corpus, not from the reports: a loud commander who never earns a line
            # must not look like an absent man
            "air_n": d.get("air_n"), "air_spoke": d.get("air_spoke"),
            "air_days": d.get("air_days"), "air_freqs": d.get("air_freqs"),
        }
        out[n]["established"] = established(out[n])
        out[n]["place_flag"] = looks_like_a_place(out[n])
    return out


def show(rec: dict):
    print(f"\n=== {rec['callsign']} ===")
    if rec["days_seen"]:
        print(f"  подій {rec['n_events']}, діб {rec['n_days']} "
              f"({rec['days_seen'][0]} .. {rec['days_seen'][-1]})")
    else:
        print("  подій: ЖОДНОЇ - у звіти не потрапляв")
    if rec.get("air_n") is not None:
        print(f"  ЕФІР: {rec['air_n']} перехоплень за {rec['air_days']} діб "
              f"({rec['air_spoke']} сам станція)")
    for net in rec["nets"][:3]:
        print(f"  мережа: {net}")
    if rec["as_destination_days"]:
        print(f"  БУВ ПУНКТОМ ПРИЗНАЧЕННЯ: у {rec['as_destination_days']} різних діб")
    if rec["ordered_days"]:
        print("  СТАВИВ ЗАДАЧІ:  " + ", ".join(f"{k} ({v} діб)" for k, v in rec["ordered_days"].items()))
    if rec["ordered_by_days"]:
        print("  ОТРИМУВАВ НАКАЗ: " + ", ".join(f"{k} ({v} діб)" for k, v in rec["ordered_by_days"].items()))
    if rec["escorted_by_days"]:
        print("  У СУПРОВОДІ:    " + ", ".join(f"{k} ({v})" for k, v in rec["escorted_by_days"].items()))
    if rec["with_days"]:
        print("  разом у подіях: " + ", ".join(f"{k} ({v})" for k, v in list(rec["with_days"].items())[:8]))
    if rec.get("place_flag"):
        print(f"  [!] {rec['place_flag']}")
    est = rec.get("established") or {}
    if est.get("ready"):
        print(f"  >>> ВСТАНОВЛЕНО (>={MIN_DAYS} діб, >={MIN_PARTNERS} співрозмовників):")
        for w in est["why"]:
            print(f"        {w}")
    elif est.get("pending"):
        print(f"  ще не встановлено, накопичується:")
        for w in est["pending"][:3]:
            print(f"        {w}")
    for s in rec["samples"][:3]:
        print(f"    [{s['day']}] {s['text']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reports", default=str(REPORTS))
    ap.add_argument("--show", nargs="*", help="print these callsigns after folding")
    ap.add_argument("--top", type=int, default=0, help="print the N most-seen callsigns")
    ap.add_argument("--air-days", type=int, default=14,
                    help="window for the corpus air counter; 0 to skip it")
    ap.add_argument("--air-to", default=None)
    ap.add_argument("-o", "--out", default=str(STORE))
    args = ap.parse_args()

    files = sorted(Path(args.reports).glob("ZVIT_*_events.json"))
    if not files:
        sys.exit(f"немає файлів подій у {args.reports}")

    store: dict = {}
    used = 0
    for f in files:
        try:
            events = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  пропущено {f.name}: {exc}", file=sys.stderr)
            continue
        fold(events, f.name.split("_")[1], store)
        used += 1

    if args.air_days:
        n, lo, hi = add_air(store, args.air_days, args.air_to)
        print(f"ефір з корпусу: {n} перехоплень, вікно {lo:%d.%m} - {hi:%d.%m}")

    out = serialise(store)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    strong = [r for r in out.values() if r["n_days"] >= 3]
    loud_silent = [r for r in out.values()
                   if (r.get("air_n") or 0) >= 20 and r["n_events"] == 0]
    print(f"згорнуто файлів подій: {used}")
    print(f"позивних у досьє: {len(out)}, з них бачені >=3 різних діб: {len(strong)}")
    places = [r for r in out.values() if (r.get("place_flag") or "").startswith("ІМОВІРНО")]
    ready = [r for r in out.values() if (r.get("established") or {}).get("ready")
             and not (r.get("place_flag") or "").startswith("ІМОВІРНО")]
    print(f"відношень ВСТАНОВЛЕНО (>={MIN_DAYS} діб, >={MIN_PARTNERS} співрозмовників): "
          f"{len(ready)} позивних")
    print(f"відсіяно як ІМОВІРНІ ОРІЄНТИРИ (багато подій, майже нема ефіру): {len(places)}")
    if loud_silent:
        print(f"гучні в ефірі, але БЕЗ жодної події: {len(loud_silent)} "
              f"(саме тут ховається командний склад)")
    print(f"записано: {args.out}")

    if args.top:
        for r in sorted(out.values(), key=lambda r: -r["n_events"])[:args.top]:
            show(r)
    for n in (args.show or []):
        r = out.get(n.upper())
        if r:
            show(r)
        else:
            print(f"\n{n.upper()}: у досьє немає")


if __name__ == "__main__":
    main()
