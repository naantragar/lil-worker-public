#!/usr/bin/env python3
"""report_check — a mechanical pass over EVERY line of a report, before a human reads any of it.

Why this exists. The recheck pass I do by hand is a RISK TRIAGE: I read the report, pick the lines
that look dangerous by category — killings, sides, prisoners, threats to our people — and pull the
intercepts for those. It is good where it looks and blind where it does not. On 04.10.2026 a line
invented a radio channel called «6 больших» (the speech said «выйди в 6 больших» — come on the air
at six o'clock, `большая` being their word for an hour) and it sailed through, because a channel
switch is the most boring class of line in the report and never entered my candidate list.

So this tool does the opposite of triage: it looks at ALL lines with checks that need no judgement,
and hands back a short list of lines that must be read against their intercepts. It never decides
whether a line is wrong — it says which ones cannot be cleared mechanically.

    python3 tools/upstream/report_check.py 05.10 [--events <path>] [--top 40] [--no-save]

Reads `<out>_events.json` + `<out>_sources.json`, the corpus (read-only) and our past reports.
Stdlib only. Touches nothing in the report pipeline.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "knowledge" / "upstream" / "reports_out"
sys.path.insert(0, str(REPO / "tools" / "analytics2"))
import losses                           # noqa: E402 — one definition of "who is a casualty"
CORPUS_DB = Path(os.environ.get("UPSTREAM_MESSAGES_DB", "~/wa-monitor/messages.db"))

# A report term on the left may only stand in a line if the speech under it carries one of the
# patterns on the right. Curated from the Class A errors we actually made, not invented in advance:
# every entry here is a mistake that shipped at least once.
TERM_EVIDENCE = {
    "канал": r"канал|частот|волн|перекл|переключ|перейд|перейти на",
    "вантаж|посилк|МТЗ|доставк": r"груз|посылк|вода|воду|продукт|бк\b|батаре|провиз|доставк|гумк|яндекс|закин|привез",
    "полонен|затриман|полон": r"плен|заложник|задерж|поймал|взяли|сдал|связал",
    "добит|стратит|розстріл|ліквідув|обнул": r"обнул|добей|добит|кончай|прикончи|расстрел|ликвид|ебни|въеб|пристрел",
    "евакуац|винос тіла": r"эвак|вынос|вытаскив|забрать|забрали|носилк|вывоз|тащи",
    "трофейн|захоплен": r"трофей|захват|захвач|отбил|забрал|вилуч|плен|сдал",
    "мінуван|розтяжк|вибухівк": r"мин[аыуоеи]|минир|растяж|магнитк|\bтм\b|лепест|подарок|заряж|взрывчат",
}

# Statements that cannot be true no matter what the speech says — a report line contradicting
# itself. They need no intercept to judge, so they are reported separately and first: these are not
# "go read it", they are "fix it".
FORBIDDEN = [
    # The closed list of their nicknames that must never reach a line (owner, 10.10.2026). Each one
    # came back a second or third time while it depended on me remembering, so it is checked by
    # machine instead: these need no intercept to judge, only replacing.
    (r"карандаш",
     "«карандаш» у рядок не йде НІКОЛИ - пишеться «о\\с РОВ» / «гр о\\с РОВ». Слово не задає "
     "сторони, і на ньому ми тричі зробили з їхньої піхоти нашу"),
    (r"крестик",
     "«крестик» - це ПЕРЕХРЕСТЯ лісосмуг/доріг, загальна назва, а не ім'я точки (1904 перехоплення, "
     "координат нема ні однієї; вони кажуть «на наступному перекрестку» і в множині). Пишеться "
     "«перехрестя», ніколи «ор КРЕСТИК» чи «т крестик»"),
    (r"открытк",
     "«открытка» - це ВІДКРИТА МІСЦЕВІСТЬ, поле, а не точка. «т «открытка»» - вигаданий пункт"),
    (r"\bнорк",
     "«норка» - це УКРИТТЯ або ЯМА, кличка нічого не додає"),
    (r"полочк",
     "«полочка» - це ЛІСОСМУГА (лс), загальна назва смуги; «через лс («полочка»)» каже «лс» двічі"),
    (r"оператор",
     "«оператор <позивний>» не пишемо: оператор — це нижчий чин за пультом, а той, кого ми чуємо, "
     "здебільшого командир, який дивиться картинку. Пишеться просто «супровід БпЛА <ПОЗИВНИЙ>»"),
]

IMPOSSIBLE = [
    # The escort word must stand NEXT TO «БпЛА СОУ», not merely in the same line. Four days running
    # (06-09.10.2026) this flagged correct lines of the commonest shape there is: their handler leads
    # the man («веде ШАЙБА», «супровід КОЛЮЧИЙ») while OUR drone is the threat that stopped him. Both
    # halves are true and the line is right; only adjacency distinguishes the inverted case.
    # `ведення` must also stand alone: `без наведення місця` contains it and means nothing here.
    (r"\b(супровід|супроводі|під наглядом|під спостереженням|ведення|веде|координац|керівництв)"
     r"\w*\s+(?:\w+\s+){0,2}?(?:бпла\s+)?соу\b",
     r".",
     "наш борт не водить їхнього бійця: «супровід/під наглядом» разом із БпЛА СОУ — це перевернуте, "
     "у мові майже завжди «рух через/під загрозою БпЛА СОУ»"),
    (r"\bнаш[іихе]?\b", r".",
     "«наш» у рядку звіту читається як СОУ, а в мові «наши» — це вони; написати прямо, чий"),
    (r"вантаж|посилк|доставк", r"вампір|бабка|баба\s*яга",
     "важкий ударний борт СОУ не возить їм вантаж: «сброс» з нього — це удар"),
]

# A line claiming a PERSON is one of ours — not «БпЛА СОУ», which is our drone and appears in most
# movement lines in the report. Only the person-claims are worth testing against their own traffic.
# NB: this is matched against norm(), which strips the backslash — `в\с СОУ` arrives as `в с соу`.
# Writing `в\\с` here silently never matched, and only the `полон…` branch was doing any work.
CLAIMS_OUR_MAN = re.compile(
    r"(?:\bв с|\bо с|\bгр|бійц\w*|військовослужб\w*|полонен\w*|затриман\w*|\bтіло|загибл\w*)\s+"
    r"(?:\w+\s+){0,2}?соу\b"
    r"|соу\b\s+(?:у полон|в полон|полонен)"
    r"|полон\w*\s+(?:\w+\s+){0,3}?соу\b")

SIDE_OURS = r"хохол|хохл|укроп|пидар|пидор|враг|противник|соу\b|чуж|их сторон|дрг"
SIDE_THEIRS = r"наш|союзник|свои|своих|позывн|братик|пацан|товарищ"

NUM_WORDS = {"один": "1", "одна": "1", "два": "2", "две": "2", "три": "3", "четыре": "4",
             "пять": "5", "шесть": "6", "семь": "7", "восемь": "8", "девять": "9", "десять": "10"}

CALLSIGN_RE = re.compile(r"\b[А-ЯЁЇІЄ]{3,}(?:[ -][А-ЯЁЇІЄ0-9]{1,})?\b")
# Our own abbreviations are not callsigns and must never be read as people.
NOT_A_NAME = {"РОВ", "СОУ", "БПЛА", "БПЛА СОУ", "МТЗ", "ФПВ", "ВУ", "ТЗ", "ТМ", "УКХ", "ДРГ",
              "КНП", "АКБ", "БК", "ПВН", "МГРС", "MGRS", "СІМ", "ЗСУ"}
QUOTED_RE = re.compile(r"«([^»]{2,30})»")
NUM_RE = re.compile(r"\b\d{1,6}\b")


def norm(s: str) -> str:
    return re.sub(r"[^а-яёіїєa-z0-9 ]", " ", (s or "").casefold())


def speech_of(src: dict, refs: list) -> str:
    """Everything the line is allowed to rest on: the speech, the analyst's marks, the stations."""
    parts = []
    for r in refs or []:
        s = src.get(r) or {}
        parts += list(s.get("speech") or [])
        parts += list(s.get("marks") or [])
        parts += list(s.get("stations") or [])
        parts.append(str(s.get("net") or ""))
    return norm(" ".join(parts))


def corpus_vocab() -> set:
    """Every word the traffic has ever said, lowercased. For the novelty check."""
    con = sqlite3.connect(f"file:{CORPUS_DB}?mode=ro", uri=True)
    vocab: set = set()
    for (t,) in con.execute("SELECT text FROM messages WHERE text IS NOT NULL"):
        vocab.update(re.findall(r"[а-яёa-z]{3,}", t.casefold()))
    return vocab


def ours_vocab(skip_day: str = "") -> set:
    """Everything our own PAST reports and notes have printed — a name we have used before is not
    novel even if this day's traffic does not say it.

    `skip_day` excludes the report being checked, and that argument is the whole point: the day's own
    txt sits in the same folder, so without it every invented name is "already in our reports" —
    found there because this very report printed it. The check was quietly validating itself.
    """
    v: set = set()
    files = [f for f in OUT.glob("ZVIT_*.txt") if not skip_day or f.name != f"ZVIT_{skip_day}.txt"]
    for f in files + list((REPO / "knowledge" / "upstream").glob("*.md")):
        try:
            v.update(re.findall(r"[а-яёіїєa-z]{3,}", f.read_text(encoding="utf-8").casefold()))
        except (OSError, UnicodeDecodeError):
            continue
    return v


def heard_as_their_station(names: set, day: str) -> dict:
    """For each callsign we have labelled СОУ: how often it is heard as a CORRESPONDENT on the
    enemy's own nets, and since when.

    The corpus is intercepts OF enemy traffic, so every net in it is theirs. A man of ours does not
    sit as a station on their net for months - so a long history here contradicts an СОУ label on
    the same name.

    This is the test that settled «98 полк» on 12.10.2026. A detained man named his commander as
    BABAI, the report wrote him up as `в\\с СОУ`, and the owner caught it: there is no 98th regiment
    on our side. BABAI had been sounding as a station on their nets since August - which made the
    detained man theirs (their 1198th, said short), not a captured Ukrainian soldier. Writing one of
    theirs as ours invents a captured soldier, and somebody may read that as a missing man, so the
    check is worth a full corpus pass.

    The day being checked is excluded: a callsign first heard today proves nothing either way
    (ВАЛЬДЕМАР, same afternoon, existed only inside the report of his own capture).
    """
    if not names:
        return {}
    con = sqlite3.connect(f"file:{CORPUS_DB}?mode=ro", uri=True)
    pats = {n: re.compile(r"(?<![А-ЯЁЇІЄҐа-яёїієґ])" + re.escape(n) + r"(?![А-ЯЁЇІЄҐа-яёїієґ])")
            for n in names}
    # a station line is the bare block between the net header and the speech: capitals only
    st_re = re.compile(r"^\s*\d?\s*[-–—]?\s*[А-ЯЁЇІЄҐ][А-ЯЁЇІЄҐ\s\"'0-9.()-]{1,34}$")
    day_dm = day.replace(".", r"\.")
    same_day = re.compile(r"\b" + day_dm + r"\.\d{4}")
    out: dict = {n: {"n": 0, "first": "", "nets": set()} for n in names}
    for (t,) in con.execute("SELECT text FROM messages WHERE text IS NOT NULL"):
        if same_day.search(t or ""):
            continue
        lines = [x.strip() for x in t.splitlines() if x.strip()]
        st = [x for x in lines if st_re.match(x)]
        if not st:
            continue
        blob = " ".join(st)
        for n, pat in pats.items():
            if not pat.search(blob):
                continue
            rec = out[n]
            rec["n"] += 1
            net = next((x[:54] for x in lines if x.startswith("УКХ")), "")
            if net:
                rec["nets"].add(net)
            d = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", t)
            if d:
                key = d.group(3) + d.group(2) + d.group(1)
                if not rec["first"] or key < rec["first"][0]:
                    rec["first"] = (key, d.group(0))
    return {n: r for n, r in out.items() if r["n"]}


def main() -> int:
    ap = argparse.ArgumentParser(description="mechanical pre-check of a built report")
    ap.add_argument("day", help="DD.MM of the report, e.g. 05.10")
    ap.add_argument("--events", help="events json to check (default: the day's)")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args()

    ev_path = Path(a.events) if a.events else OUT / f"ZVIT_{a.day}_events.json"
    src_path = OUT / f"ZVIT_{a.day}_sources.json"
    events = json.loads(ev_path.read_text(encoding="utf-8"))
    src = json.loads(src_path.read_text(encoding="utf-8"))

    corpus = corpus_vocab()
    mine = ours_vocab(a.day)

    # Every callsign in a line that claims a PERSON is one of ours, checked against their traffic.
    soy_names: set = set()
    for e in events:
        if CLAIMS_OUR_MAN.search(norm(e.get("text") or "")):
            soy_names.update(w.strip() for w in CALLSIGN_RE.findall(e.get("text") or "")
                             if w.strip() not in NOT_A_NAME and len(w.strip()) >= 4)
    their_station = heard_as_their_station(soy_names, a.day)

    flags: list[tuple[int, str, dict, list]] = []

    # --- per-line checks ---------------------------------------------------
    for e in events:
        text = e.get("text") or ""
        low = norm(text)
        refs = e.get("_src_ref") or []
        sp = speech_of(src, refs)
        reasons: list[str] = []
        sev = 0

        for f_re, why in FORBIDDEN:
            if re.search(f_re, low):
                reasons.append("ЗАБОРОНЕНЕ ФОРМУЛЮВАННЯ: " + why)
                sev += 9

        for a_re, b_re, why in IMPOSSIBLE:
            if re.search(a_re, low) and re.search(b_re, low):
                reasons.append("НЕМОЖЛИВЕ СПОЛУЧЕННЯ: " + why)
                sev += 9

        if not refs:
            reasons.append("під рядком НЕМА жодного перехоплення")
            sev += 3
        if text.rstrip().endswith(("...", "…")):
            reasons.append("рядок обірваний")
            sev += 2

        if sp:
            for term, evid in TERM_EVIDENCE.items():
                if re.search(term, low) and not re.search(evid, sp):
                    reasons.append(f"у рядку «{term.split('|')[0]}», у мові нема нічого з [{evid[:46]}…]")
                    sev += 3

            # numbers that exist nowhere in the speech
            missing = []
            for n in NUM_RE.findall(text):
                # 200/300/500 are report jargon, not quantities; single digits are almost always
                # re-expressed in speech ("на вторую кнопку" vs "т2") and only make noise.
                if n in {"100", "200", "300", "500"} or len(n) == 1:
                    continue
                if re.search(r"\b" + n + r"\b", sp):
                    continue
                if any(re.search(rf"\b{w}\b", sp) for w, d in NUM_WORDS.items() if d == n):
                    continue
                # a number spoken digit by digit: «4 1 2» for 412
                if " ".join(n) in sp:
                    continue
                missing.append(n)
            if missing:
                reasons.append(f"числа, яких нема в мові: {', '.join(missing[:6])}")
                sev += 2

            # side markers
            if re.search(r"\bсоу\b", low) and not re.search(SIDE_OURS, sp):
                reasons.append("позначено СОУ, у мові нема жодної ознаки сторони")
                sev += 2
            if re.search(r"\bров\b|союзник", low) and not re.search(SIDE_THEIRS, sp):
                reasons.append("позначено РОВ/союзник, у мові нема ознаки свого")
                sev += 2

        # A line that calls a PERSON ours, while the names in it live on their own nets.
        if CLAIMS_OUR_MAN.search(low):
            known = []
            for w in dict.fromkeys(x.strip() for x in CALLSIGN_RE.findall(text)):
                # A man issuing or relaying the order is plainly the speaker, never the person we
                # have labelled ours. Dropping those keeps the flag list short without touching the
                # case this exists for: «ПСИХ взяв у полон … (ім БАБАЙ)» has neither word in front.
                if re.search(r"(?:наказ\w*|через|ком\.|ст\.|за вказівкою|керує|веде)\s+(?:\w+\s+){0,1}?"
                             + re.escape(w) + r"\b", text, re.I):
                    continue
                rec = their_station.get(w)
                if rec and rec["n"] >= 3:
                    known.append("%s (%d раз з %s, %s)" %
                                 (w, rec["n"], rec["first"][1] if rec["first"] else "?",
                                  sorted(rec["nets"])[0][:46] if rec["nets"] else "?"))
            if known:
                reasons.append(
                    "СТОРОНА: рядок каже, що ЛЮДИНА - наша. Позивні в ньому, що звучать "
                    "кореспондентами у мережах РОВ: " + "; ".join(known[:4]) +
                    ". Якщо котрийсь із них і є той, кого ми назвали своїм - це їхній, "
                    "названий коротко")
                sev += 8

        # novel entities: a codeword or callsign nobody has ever said
        for w in QUOTED_RE.findall(text) + CALLSIGN_RE.findall(text):
            if w.strip() in NOT_A_NAME:          # our own abbreviations are not entities
                continue
            # A quote of three or more words is translated SPEECH, not a name: «ти знаєш, що робити»
            # renders «ты знаешь что делать», so none of its words stand in the Russian traffic and
            # the check fired on every quoted order. One- and two-word quotes stay — that is where a
            # channel, a codeword or a nickname lives, and «6 больших» is exactly that shape.
            if len(w.split()) >= 3:
                continue
            for tok in re.findall(r"[а-яёіїєa-z]{3,}", w.casefold()):
                if tok in corpus or tok in mine:
                    continue
                reasons.append(f"назва «{w}» — слова «{tok}» нема ні в ефірі, ні в наших звітах")
                sev += 3
                break

        if reasons:
            flags.append((sev, text, e, reasons))

    # --- cross-line checks -------------------------------------------------
    # Who is actually dead is decided by the same code that counts the casualties (`losses.py`), not
    # by "a callsign standing in a line that contains 200". The crude version read САИД out of
    # `2 тіла (200) ... поруч з САИД` and ХОХОЛ out of `доповідь «200» від ХОХОЛ` — a bystander and a
    # reporter — and then flagged every later line about them as a contradiction.
    dead: dict[str, str] = {}
    for e in events:
        t = e.get("text") or ""
        if not re.search(r"\b200\b", t):
            continue
        for row in losses.collect([e])["rows"].values():
            if row["state"] == "200":
                dead.setdefault(row["name"], e.get("time", ""))
    cross: list[str] = []
    for e in events:
        t = e.get("text") or ""
        if re.search(r"\b200\b", t):
            continue
        for c in CALLSIGN_RE.findall(t):
            if c in NOT_A_NAME:
                continue
            if c in dead and e.get("time", "") > dead[c]:
                cross.append(f"{c}: позначений 200 о {dead[c]}, але о {e.get('time')} — «{t[:90]}»")

    seen: dict[tuple, list] = {}
    for e in events:
        key = (e.get("time"), e.get("_net"))
        seen.setdefault(key, []).append(e.get("text") or "")
    dupes = []
    for (tm, _net), texts in seen.items():
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                a_, b_ = set(norm(texts[i]).split()), set(norm(texts[j]).split())
                # Two four-word lines share `в\с` and `ім` and look 50% identical while saying
                # nothing in common: `200 в\с ОКАБР (ім)` ~ `300 в\с КОРЕЯ (ім)`. Overlap only means
                # something once there is enough line to overlap.
                if min(len(a_), len(b_)) < 6:
                    continue
                if a_ and b_ and len(a_ & b_) / max(len(a_), len(b_)) > 0.45:
                    dupes.append(f"{tm}: «{texts[i][:70]}» ~ «{texts[j][:70]}»")

    # --- output ------------------------------------------------------------
    out: list[str] = []

    def say(s: str = "") -> None:
        out.append(s)
        print(s)

    say(f"# механічна перевірка ZVIT_{a.day} — {len(events)} рядків, під підозрою {len(flags)}")
    say()
    say("Це НЕ список помилок. Це список рядків, які не можна закрити механічно — їх треба читати")
    say("проти їхніх перехоплень. Усе, що тут не згадано, перевірено лише машиною і може бути хибним.")
    say()
    for sev, text, e, reasons in sorted(flags, key=lambda x: -x[0])[: a.top]:
        say(f"## [{sev}] {e.get('time')} — {text[:150]}")
        for r in reasons:
            say(f"- {r}")
        say(f"- перехоплення: {', '.join(e.get('_src_ref') or []) or '—'}")
        say()
    if cross:
        say("## суперечності між рядками")
        for c in cross[:12]:
            say(f"- {c}")
        say()
    if dupes:
        say("## схожі рядки одного часу й мережі (можливий дубль)")
        for d in dupes[:12]:
            say(f"- {d}")
        say()
    say("## чого ця перевірка НЕ бачить")
    say("- чи правильно переказаний зміст розмови (це читання, не лічба)")
    say("- кого саме мали на увазі, коли в мові кілька людей")
    say("- подію, якої в звіті нема взагалі (мовчазні мережі дивись у `_silent.txt`)")

    if not a.no_save:
        dest = OUT / f"CHECK_{a.day}.md"
        dest.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"\nзбережено: {dest.relative_to(REPO)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
