#!/usr/bin/env python3
"""Stage A — read the SPEECH and emit typed observations, never verdicts.

    python3 tools/identity/extract_observations.py --day 2026-09-11 --freq 147.5050
    python3 tools/identity/extract_observations.py --day 2026-09-11 --net-contains "60 омсбр"

Everything built before this COUNTS. Nothing reads. And what the owner is actually after lives in
the words: «иди на 13-й квадрат, там блиндажик» is a POINT, «он должен дойти до Личи» is a ROUTE,
«Кочка пусть встречает» is a third man already at the place. No amount of counting produces those.

**THE MODEL READS, IT DOES NOT JUDGE.** This is the whole design, and it is not a style preference —
in `calibration.txt` our confidently-wrong cases sit in the TOP confidence bands, so a model asked
"who is the commander" returns fluent, coherent, unverifiable error. It is therefore asked for
OBSERVATIONS with the quote that justifies them and the intercept they came from:

    ДОК → ПОЛКИЛО : ordered movement to «Кочка»    [quote, msg_id, day, freq]

The verdict ("ДОК leads") is never produced here. It is COUNTED later out of these rows across
distinct days and distinct counterparts — the same discipline that makes the dossier trustworthy.

**Unit of work is a CONVERSATION**, not an intercept: intercepts of one net chained while the gap
between them stays under GAP_MIN. A march split into fragments cannot say where the group ended up;
the exchange around it can.

Read-only on the corpus. Writes only under `knowledge/upstream/callsigns/`. Touches nothing in
`tools/analytics2/` — it imports `run.py` for the corpus door and the model call, as `trace.py` does.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT_DIR = REPO / "knowledge" / "upstream" / "callsigns"
GAP_MIN = 25          # minutes of silence that ends a conversation
MAX_CHARS = 14000     # one conversation handed to the model; the report uses ~16k successfully

SYSTEM = """Ти розбираєш перехоплення радіопереговорів противника (рос. війська) і витягуєш ФАКТИ.

ЗАБОРОНЕНО робити висновки про людей. Не пиши «Х - командир», «Х керує», «Х головний».
Пиши ТІЛЬКИ те, що прямо чути в репліках, і до кожного факту наводь ДОСЛІВНУ цитату.

Види спостережень (type):
  order_move   - хтось наказав/скерував когось РУХАТИСЬ ПО МІСЦЕВОСТІ
  guide        - хтось голосом веде когось по місцевості (прийми праворуч, обходь, лягай)
  report       - хтось комусь доповідає обстановку/стан/прибуття
  place_named  - названо точку, орієнтир, укриття, накопичувач
  meet         - призначено зустріч/передачу/супровід між людьми
  supply       - доставка МТЗ, БК, води, евакуація
  relay        - хтось наказує СПІВРОЗМОВНИКУ передати щось ТРЕТЬОМУ: пояснити йому маршрут,
                 показати дорогу, вивести його, переказати наказ.
                 «Ученый, объясни ему еще раз маршрут» = relay (А каже Б пояснити В),
                 а НЕ ведення і НЕ наказ рухатись самому Б.
  comms        - вказівка щодо ЗВ'ЯЗКУ: вийти на когось, вийти на зв'язок, перейти на інший
                 канал/частоту, час наступного сеансу
  drone        - ДІЯ ДРОНА: оператор висилає/веде/саджає свій дрон, дрон підходить до когось,
                 заряджається, «працює» над кимось. who = оператор, to_whom = до кого летить.

!!! НАЙЧАСТІША ПОМИЛКА - слово «вийти/выйти». В ефірі це майже завжди ПРО ЗВ'ЯЗОК, а не про рух:
  «Ученый, выйди сейчас на Графа»      -> comms (зв'яжись із Графом), НЕ рух до Графа
  «ты меня на связь выйдешь»            -> comms
  «выйди на синюю, ходи по синей»       -> comms (синя = канал зв'язку, НЕ місце!)
  «в 12 на связь выйдете»               -> comms
Рухом це є лише в зворотах «вийти на маршрут», «вихід з позиції», «вийти до лісосмуги» -
тобто коли поруч стоїть місцевість, а не позивний, канал чи слово «зв'язок».

Рух - це: двигайся, начинай движение, продвинуться, подходи, заходи, прыгай, перемещайся,
іди, дійти до, зайди в кущі, повертайся.

!!! ДРОНОВОД ГОВОРИТЬ ПРО ДРОН ВІД ПЕРШОЇ ОСОБИ. Це не зв'язок:
  «я на тебя вылечу»                    -> drone (вишлю до тебе свій дрон), НЕ comms
  «сейчас на вас будут работать»        -> drone (над вами працюватиме дрон)
  «подзарядиться, потом на тебя выйду»  -> drone (дрон заряджається)
  «как будет подходить, я на вас выйду» -> drone (сповістить, коли дрон підійде)
Часто з однієї репліки НЕ видно, доставка це чи ведення піхоти. Тоді просто drone,
не вигадуй призначення.

!!! НАЗВИ МІСЯЦІВ У ЦЬОМУ ЕФІРІ - ПОЗИВНІ ЛЮДЕЙ, а не дати й не місця. Виміряно за 15 діб у
шапках станцій: ДЕКАБРЬ 127, АВГУСТ 32, НОЯБРЬ 24, ОКТЯБРЬ 12, СЕНТЯБРЬ 5, МАРТ 3.
«довёл до октября» = довів до людини ОКТЯБРЬ (to_person), а не до точки й не до жовтня.

Формат відповіді - ТІЛЬКИ JSON-масив, без пояснень до чи після:
[
 {"type":"order_move","who":"ДОК","to_whom":"ПОЛКИЛО","to":"Кочка","to_person":null,
  "quote":"дослівна цитата з репліки","msg_id":"<msg_id того перехоплення>"},
 {"type":"order_move","who":"УЧЕНЫЙ","to_whom":"ГРАФ","to":null,"to_person":"УЧЕНЫЙ",
  "quote":"Граф, давай двигайся до меня","msg_id":"..."}
]

ПОЗИВНІ (who, to_whom, to_person):
- бери ТІЛЬКИ зі списку позивних цієї розмови, який наведено на початку матеріалу;
- позивний часто чути в самій репліці: «Багул, Багул, я Кащей» - той, хто говорить, це КАЩЕЙ,
  а звертається він до БАГУЛА. Користуйся цим, коли в шапці стоїть НВ;
- якщо визначити неможливо - став null. НІКОЛИ не пиши «НВ», «НП», «невідомо».

КУДИ:
- to - лише НАЗВАНА ТОЧКА НА МІСЦЕВОСТІ: орієнтир, укриття, населений пункт, «Лайм 14»,
  «третя Брусниця». НІКОЛИ не став сюди назву каналу зв'язку («синя», «марс», «1 10»),
  позивний або слово «зв'язок».
- to_person - якщо рух до ЛЮДИНИ («до мене», «до тебе», «на Графа»), постав сюди позивний тієї
  людини, а to залиши null. «До мене» = позивний того, хто говорить.
- якщо напрямок не названо - обидва null.

ЦИТАТА:
- дослівно з рядка репліки, без переказу, без склеювання двох реплік, до 200 символів;
- цитата мусить бути з того перехоплення, чий msg_id ти вказав. Це перевіряється автоматично,
  і спостереження з чужою або переказаною цитатою відкидається.

ОДНА РЕПЛІКА МОЖЕ НЕСТИ ДВА ФАКТИ - тоді дай ДВА спостереження з тією самою цитатою і тим самим
msg_id. «тебе направо, на комод» - це і guide (веде голосом), і place_named (названо «комод»).
Не вибирай одне з двох, коли чути обидва.

Якщо в розмові нічого з переліченого немає - поверни []."""


def load_run():
    spec = importlib.util.spec_from_file_location(
        "analytics2_run", REPO / "tools" / "analytics2" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_guide():
    """The station hygiene lives in guide_layer.py — one door for `1-ДОК`, leaked headers, splitting."""
    spec = importlib.util.spec_from_file_location("identity_guide_layer", HERE / "guide_layer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def conversations(recs: list[dict]) -> list[list[dict]]:
    """Chain intercepts of one net while the silence between them stays under GAP_MIN."""
    by_net: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        by_net[(r.get("network") or "?").strip()].append(r)
    out = []
    for net, rows in by_net.items():
        rows.sort(key=lambda r: r["_dt"])
        cur = [rows[0]]
        for prev, r in zip(rows, rows[1:]):
            gap = (r["_dt"] - prev["_dt"]).total_seconds() / 60
            if gap > GAP_MIN or sum(len(str(x)) for x in cur) > MAX_CHARS:
                out.append(cur)
                cur = []
            cur.append(r)
        if cur:
            out.append(cur)
    return out


UNKNOWN = {"НВ", "НП", "БП", "Б/П", "НЕВІДОМО", "НЕИЗВЕСТНО", "?"}
# Channels, not places. `known_codes.md` records «синяя, синька» as a comms channel; a colour or a
# bare number landing in `to` would put a point on the map that does not exist.
CHANNEL = re.compile(r"^\s*(син|зелен|червон|красн|жовт|желт|біл|бел|чорн|черн|марс|"
                     r"канал|частот|зв.?яз|связ)", re.I)
GLOSSARIES = [REPO / "matrix" / "bot" / "prompts" / "refraz" / "10_glossary.md",
              REPO / "knowledge" / "upstream" / "known_codes.md"]


def system_prompt() -> str:
    """The extraction rules plus the slang tables the report and refraz already maintain.

    The vocabulary lives in ONE place for the whole project; duplicating it here would guarantee the
    copies drift. `синяя` is in `known_codes.md` as a comms channel — which is exactly the kind of
    fact that stops a channel name being printed as a point on a map."""
    out = [SYSTEM]
    for p in GLOSSARIES:
        if p.exists():
            out.append(f"\n\n# Глосарій ({p.name}) - сленг ефіру, користуйся ним\n\n"
                       + p.read_text(encoding="utf-8"))
    return "".join(out)
# «до мене», «сюди», «туди» — a direction, not a place. If one of these lands in `to`, it is moved
# out: a relative target is only useful once it is resolved to a person.
RELATIVE = re.compile(r"^\s*(до |к )?(мене|меня|тебе|тебя|нього|него|неї|нее|нас|вас|них|"
                      r"сюди|сюда|туди|туда|назад|обратно|вперед|вперёд)\b", re.I)
QUOTE_MIN = 18          # how much of the quote must be found verbatim in the cited intercept


def norm(s: str) -> str:
    """Fold away punctuation, case and spacing so a quote can be matched against the speech."""
    return re.sub(r"[^а-яёіїєґa-z0-9]+", "", (s or "").lower())


def mentioned(name: str, text: str) -> bool:
    """Is this callsign or place actually SAID in this text? Matched by stem, because the air
    declines them — ГРАФ→«Графа», ТУГАРИН→«Тугарину», ИБРА→«Ибре»."""
    n = norm(name)
    if not n:
        return False
    stem = n[:max(4, len(n) - 2)]
    return len(stem) >= 3 and stem in norm(text)


def place_said(place: str, quote: str) -> bool:
    """A named place counts as grounded when one of its own words is audible in the quote.
    «Лайм 14» attached to a line about a лесополка is a field inherited from a neighbour, not a fact."""
    words = [w for w in re.findall(r"[А-Яа-яЁёІіЇїЄєҐґA-Za-z]{4,}", place or "")]
    if not words:                       # a bare number or a one-syllable name: demand the whole thing
        return norm(place) in norm(quote)
    return any(mentioned(w, quote) for w in words)


def roster_of(conv: list[dict], M, G) -> set[str]:
    """Every callsign audible in this conversation — the closed vocabulary the model may use."""
    out: set[str] = set()
    for r in conv:
        for raw in (r.get("stations") or []):
            out.update(G.clean_slot(raw, M))
    return {n for n in out if n not in UNKNOWN}


def render(conv: list[dict], roster: set[str]) -> str:
    lines = [f"# Мережа: {conv[0].get('network') or '?'}",
             "# Позивні цієї розмови (інших НЕ вигадуй): "
             + (", ".join(sorted(roster)) if roster else "жодного не розібрано"), ""]
    for r in conv:
        st = " -> ".join(x for x in (r.get("stations") or [])[:2] if x) or "НВ"
        mark = " ".join(x for x in (r.get("comment_above"), r.get("comment_below")) if x)
        lines.append(f"[msg_id={r.get('msg_id')}] {r['date']} {r['time']} · "
                     f"{r.get('freq') or '?'} · {st}")
        if mark:
            lines.append(f"  мітка оператора: {mark[:200]}")
        for s in (r.get("speech") or []):
            lines.append(f"  {s[:400]}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--freq", default=None, help="restrict to one frequency")
    ap.add_argument("--net-contains", default=None, help="restrict to nets whose header contains this")
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--limit", type=int, default=0, help="stop after N conversations (trial runs)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    M = load_run()
    day = datetime.strptime(a.day, "%Y-%m-%d")
    dt_to, dt_from = f"{day:%Y-%m-%d} 15:00", f"{day - timedelta(days=1):%Y-%m-%d} 15:00"
    recs = M.fetch(dt_from, dt_to)
    if a.freq:
        recs = [r for r in recs if r.get("freq") and
                abs(float(r["freq"]) - float(a.freq)) * 1000 <= 5]
    if a.net_contains:
        recs = [r for r in recs if a.net_contains.lower() in (r.get("network") or "").lower()]
    if not recs:
        sys.exit("під ці умови не підпало жодного перехоплення")

    convs = conversations(recs)
    if a.limit:
        convs = convs[:a.limit]
    print(f"перехоплень {len(recs)}   розмов {len(convs)}", file=sys.stderr)

    G = _load_guide()
    SYS = system_prompt()
    rows, spent, failed = [], 0.0, 0
    drop = defaultdict(int)
    for i, conv in enumerate(convs, 1):
        roster = roster_of(conv, M, G)
        body = render(conv, roster)
        speech_of = {r.get("msg_id"): norm(" ".join(r.get("speech") or [])) for r in conv}
        called_of = {r.get("msg_id"): G.clean_slot((r.get("stations") or ["", ""])[1]
                                                   if len(r.get("stations") or []) > 1 else "", M)
                     for r in conv}
        try:
            raw, dt = M.call_model(SYS, body, a.model, a.effort)
            spent += dt
            got = M.extract_json(raw)
        except Exception as exc:                              # noqa: BLE001
            failed += 1
            print(f"розмова {i}/{len(convs)}: впала - {exc}", file=sys.stderr)
            continue
        kept = 0
        for o in got:
            if not isinstance(o, dict) or not o.get("type"):
                drop["не спостереження"] += 1
                continue
            # 1. It must point at an intercept of THIS conversation, or it is invented.
            if o.get("msg_id") not in speech_of:
                drop["чужий msg_id"] += 1
                continue
            # 2. The quote must actually be in that intercept's speech — catches a paraphrase and
            #    a quote lifted from the other side of the exchange.
            q = norm(o.get("quote"))
            if len(q) < QUOTE_MIN or q[:60] not in speech_of[o["msg_id"]]:
                drop["цитата не знайдена"] += 1
                continue
            # 3. Callsigns only from the closed vocabulary of this conversation; "НВ" is not a name.
            for f in ("who", "to_whom", "to_person"):
                v = (o.get(f) or "").strip().upper()
                o[f] = v if v and v not in UNKNOWN and v in roster else None
            # 3b. The ADDRESSEE must be grounded, not picked. `who` comes from the station slot and
            #     stands; `to_whom` comes from the model reading, so it survives only when the name
            #     is audible in its own quote OR the intercept named exactly one counterpart.
            #     Measured 13.09: one line whose addressee was masked («........») with TWO names in
            #     the slot came back as ТУГАРИН in one run and ИБРА in the next — that is guessing,
            #     and a plausible wrong callsign is worse than an empty field: empty gets filled
            #     later, wrong lives in the graph as a fact.
            called = called_of.get(o["msg_id"]) or []
            for f in ("to_whom", "to_person"):
                v = o.get(f)
                if not v:
                    continue
                if mentioned(v, o.get("quote")):
                    continue
                if f == "to_person" and v == o.get("who"):
                    continue                          # «до мене» resolved to the speaker himself
                if len(called) == 1 and v == called[0]:
                    continue                          # only one counterpart on the air, no choice
                o[f] = None
                drop["адресат не підтверджений цитатою"] += 1
            # 4. A relative target is not a place, and neither is a radio channel.
            if o.get("to") and RELATIVE.match(str(o["to"])):
                o["to"] = None
                drop["«до мене» прибрано з to"] += 1
            if o.get("to") and CHANNEL.match(str(o["to"])):
                o["to"] = None
                drop["канал прибрано з to"] += 1
            # 5. A place must be audible in its OWN quote. Three observations pulled out of one
            #    intercept used to share its fields: «Лайм 14» ended up on a line about a лісосмуга.
            if o.get("to") and not place_said(str(o["to"]), o.get("quote")):
                o["to"] = None
                drop["місце не звучить у цитаті"] += 1
            o.update(day=conv[0]["date"], net=conv[0].get("network"),
                     freq=conv[0].get("freq"), t_from=conv[0]["time"], t_to=conv[-1]["time"])
            rows.append(o)
            kept += 1
        print(f"розмова {i}/{len(convs)}: {kept} спостережень з {len(got)} за {dt:.0f}s "
              f"({len(conv)} перехоплень)", file=sys.stderr)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = a.freq or (a.net_contains or "all").replace(" ", "_")
    path = Path(a.out) if a.out else OUT_DIR / f"SPOST_{day:%d.%m.%Y}_{tag}.json"
    path.write_text(json.dumps({"day": f"{day:%Y-%m-%d}", "window": [dt_from, dt_to],
                                "model": a.model, "effort": a.effort,
                                "conversations": len(convs), "failed": failed,
                                "model_seconds": round(spent, 1),
                                "observations": rows}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    by_type = defaultdict(int)
    for r in rows:
        by_type[r["type"]] += 1
    named = sum(1 for r in rows if r.get("who"))
    print(f"\nспостережень: {len(rows)}   час моделі: {spent:.0f}s   впало розмов: {failed}",
          file=sys.stderr)
    print(f"з них із названим who: {named} ({named / max(len(rows), 1) * 100:.0f}%)", file=sys.stderr)
    for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<12} {v}", file=sys.stderr)
    if drop:
        print("відкинуто перевірками:", file=sys.stderr)
        for k, v in sorted(drop.items(), key=lambda kv: -kv[1]):
            print(f"  {k:<24} {v}", file=sys.stderr)
    print(f"{path}", file=sys.stderr)


if __name__ == "__main__":
    main()
