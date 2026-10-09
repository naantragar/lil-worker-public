#!/usr/bin/env python3
"""freq_card — one frequency in, a card out: callsigns, orientirs, coordinates, and what is UNKNOWN.

Why this exists. On 09.10.2026 the owner asked a one-line question — which orientirs were named on
146.1300 — and I got it wrong four times in a row, each time in a different way:

1. **A false "there is none".** My first sweep used SQLite `lower()`, which does NOT lowercase
   Cyrillic, plus a hand-written list of words to look for. So every ALL-CAPS name and every name I
   had not thought of fell through, and I reported one orientir where there were seven.
2. **A false merge.** «Аккордеон 7» heard on air and «АКОРД 7» in the analyst's notes sounded alike
   and shared the number, so I declared them the same place. They are two different orientirs in two
   different naming families, and the analyst spells both, separately.
3. **A confused source.** I answered "we heard X" while X existed only in a Patagonia analyst's
   comment and had never been on our air at all.
4. **A frequency that is not one number.** The same intercept arrives in AllInARow labelled
   `146.130` and in Patagonia labelled `146.1250`, so "strictly on this frequency" is a statement
   about who typed the header, not about the radio.

Every one of those is a question the owner then had to ask me. This tool exists so the answer comes
with its own uncertainty attached, instead of me supplying confidence I have not earned.

    python3 tools/upstream/freq_card.py 146.1300 [--days 30] [--name ВОДЯНИКА]

Design rules, each one paid for by a mistake above:
- **All matching happens in Python**, never in SQL. `lower()` is lethal on Cyrillic here.
- **Absence is reported WITH the shape of the search.** "Not found" is always "not found by THIS
  pattern over THIS window", printed next to the claim.
- **Spelling variants are listed as CANDIDATES and never merged.** Folding groups them for the eye;
  the tool states what evidence a merge would need and refuses to perform one.
- **Source is always labelled**: ours (Invisible Hand / AllInARow) versus Patagonia, and heard-on-air
  versus written-in-an-analyst's-mark. Those are different kinds of knowledge.
- **A name in a station header is a correspondent; a name only in speech is a place candidate.** Some
  are both, and the tool says so instead of choosing.

Stdlib only, read-only on the corpus.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import os
import re
import sqlite3
import sys

CORPUS = os.environ.get("UPSTREAM_MESSAGES_DB", "~/wa-monitor/messages.db")
OURS = ("Invisible Hand", "AllInARow")

# A speech line in these transcripts starts with a dash, or a speaker number then a dash.
SPEECH = re.compile(r"^\s*(?:\d{1,2}\s*)?[—–\-]{1,3}\s*")
# Header furniture that is never a callsign.
HDR_SKIP = re.compile(r"^(?:УКХ|Коментар|Мітк|\d{2}\.\d{2}\.\d{4}|\d{3}\.\d{4}|🔈|📞|📷)")
NAME = re.compile(r"\b[А-ЯЁЇІЄ][А-ЯЁЇІЄа-яёїіє'’-]{2,}\b")
# `ВЫДРА 33`, `АКОРД 11`, `Водяника 9` — a name with a section number is the strongest orientir shape
NAMENUM = re.compile(r"\b([А-ЯЁЇІЄ][А-ЯЁЇІЄа-яёїіє'’-]{2,})\s+(\d{1,3})\b")
# Locatives that put a place after them.
LOC = re.compile(r"(?:на|до|від|з|із|у|в|біля|возле|около|район\w*|р-н\w*|ор\.?|точк\w*|лісосмуз\w*|"
                 r"лесополк\w*|л[\\/.]?с)\s+$", re.I)
COORD = re.compile(r"37\s*T\s*[A-Z]{2}\s*\d{4,5}\s*\d{4,5}|MGRS", re.I)
COORD_ONE = re.compile(r"(37\s*T\s*[A-Z]{2}\s*\d{4,5}\s*\d{4,5})", re.I)

# Words that look like names because they open a sentence. Not a knowledge list — a stop-list of
# RUSSIAN FUNCTION WORDS, so it cannot hide a place name the way a whitelist of places would.
STOP = set("""как принял вопрос приём прием давай сейчас командир там вот все это тебе если
правильно бля блядь нет что пока надо мне уже где кто так теперь только куда может быть над под
слышу вижу понял понятно хорошо нормально плохо тихо темно сюда туда дальше ближе левее правее
влево вправо прямо назад обратно стой стоп начинай продолжай двигайся выходи выйди лети иди идем
идешь жду ждем ответь повтори доложи скажи говори смотри слушай смотрим знаю думаю хочу буду
будет был была были есть нету нема плюс минус минуту секунду через потом сначала завтра вчера
сегодня ночью утром днем вечером никто ничего никуда ничем везде рядом поруч спасибо пожалуйста
хватит конечно точно примерно почти совсем очень чуть немного много мало
метр метра метров метрах дистанция дистанцию секунд секунды секунда минут минуты минута
каждый каждые каждую каждого будьте буквально примерно мітки коментар мітка
час часа часов сутки суток штук штуки раз раза человек человека бойца номер номера""".split())


def stem(s: str) -> str:
    """Case-insensitive, declension-insensitive key. `БОГОМОЛУ` and `БОГОМОЛ` share a stem, so a
    callsign in the dative stops masquerading as a place name — that was half the noise in the first
    run of this tool. Russian case endings are short, so cutting two characters is enough to make
    `ОЛЬХУ`/`ОЛЬХИ`/`ОЛЬХЕ` one entry without colliding distinct names."""
    f = fold(s)
    return f[:-2] if len(f) > 5 else f[:-1] if len(f) > 3 else f


def same_name(a: str, b: str) -> bool:
    """True when two spellings are the same word in different cases — NOT when they merely rhyme.
    One must be a prefix of the other and they must differ by at most two trailing characters."""
    x, y = fold(a), fold(b)
    if x == y:
        return True
    lo, hi = sorted((x, y), key=len)
    return hi.startswith(lo) and len(hi) - len(lo) <= 2


def fold(s: str) -> str:
    """One key for one name written several ways. For GROUPING on screen, never for merging."""
    t = (s or "").lower()
    for a, b in (("і", "и"), ("ї", "и"), ("ы", "и"), ("й", "и"), ("є", "е"),
                 ("э", "е"), ("ё", "е"), ("ґ", "г")):
        t = t.replace(a, b)
    return t.replace("ь", "").replace("ъ", "").replace("'", "").replace("’", "")


def skeleton(s: str) -> str:
    """Consonant skeleton — catches `Аккордеон`/`Акордеон`, and deliberately also catches
    `АКОРД`, which is a DIFFERENT place. That is the point: it offers candidates, it never decides."""
    return re.sub(r"[аеёиоуыэюяіїє']", "", fold(s))


def load(path: str):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = []
    for ts, text, group in con.execute(
            "SELECT timestamp, text, group_name FROM messages WHERE text IS NOT NULL"):
        rows.append((dt.datetime.fromtimestamp(ts / 1000), text, group or "—"))
    rows.sort(key=lambda r: r[0])
    return rows


def split_intercept(text: str):
    """-> (net_lines, station_lines, speech_lines, mark_lines). The station block is what stands
    between the net header and the first spoken line; everything after is speech."""
    net, stations, speech, marks = [], [], [], []
    in_speech = False
    for raw in text.split("\n"):
        s = raw.strip()
        if not s:
            continue
        if s.startswith(("Коментар", "Мітк")):
            marks.append(s)
            continue
        if SPEECH.match(s):
            in_speech = True
            speech.append(s)
            continue
        if in_speech:
            speech.append(s)          # continuation of a reply
            continue
        if s.startswith("УКХ"):
            net.append(s)
            continue
        if HDR_SKIP.match(s):
            continue
        stations.append(s)
    return net, stations, speech, marks


def main() -> int:
    ap = argparse.ArgumentParser(description="frequency card: callsigns, orientirs, coordinates")
    ap.add_argument("freq", help="e.g. 146.1300 (a prefix also works: 146.13)")
    ap.add_argument("--days", type=int, default=0, help="limit to the last N days")
    ap.add_argument("--name", help="dig one name: every mention, every spelling, every coordinate")
    a = ap.parse_args()

    rows = load(CORPUS)
    if not rows:
        print("корпус порожній або недоступний", file=sys.stderr)
        return 1
    horizon = rows[-1][0] - dt.timedelta(days=a.days) if a.days else None

    # --- 1. coverage. The frequency is a LABEL, so collect every label variant that co-occurs. ---
    mine, theirs, labels = [], [], collections.Counter()
    for when, text, group in rows:
        if a.freq not in text:
            continue
        if horizon and when < horizon:
            continue
        for f in re.findall(r"\b\d{3}\.\d{2,4}\b", text):
            labels[f] += 1
        (mine if group in OURS else theirs).append((when, text, group))

    both = mine + theirs
    print(f"# картка частоти {a.freq}")
    print()
    if not both:
        print(f"Жодного перехоплення, де в тексті стоїть «{a.freq}».")
        print("Це НЕ означає, що мережа молчала: частота - це підпис, який ставить група.")
        return 0
    print(f"вікно: {min(w for w, _, _ in both):%d.%m.%Y} - {max(w for w, _, _ in both):%d.%m.%Y}"
          f"{'' if not a.days else f' (останні {a.days} діб)'}")
    pg = collections.Counter(g for _, _, g in both)
    print(f"перехоплень: {len(both)}  ·  НАШІ (IH/AllInARow): {len(mine)}  ·  Патагонія: {len(theirs)}")
    print("по групах: " + ", ".join(f"{g} {c}" for g, c in pg.most_common()))
    if len(labels) > 1:
        print("підписи частот у цих же текстах: "
              + ", ".join(f"{f}×{c}" for f, c in labels.most_common()))
        print("  ⚠ один і той самий перехват різні групи підписують різною частотою - "
              "«строго на цій частоті» це про того, хто набрав шапку, а не про радіо")
    print()

    # --- 2. callsigns: names standing in a station block ---
    cs = collections.Counter()
    cs_first, cs_src = {}, collections.defaultdict(set)
    say = collections.Counter()
    say_first, say_src, say_ctx = {}, collections.defaultdict(set), {}
    marks_names = collections.Counter()
    marks_first = {}
    for when, text, group in both:
        _net, stations, speech, marks = split_intercept(text)
        tag = "наші" if group in OURS else "Патагонія"
        for line in stations:
            for w in NAME.findall(line):
                if w.lower() in STOP:
                    continue
                cs[w.upper()] += 1
                cs_first.setdefault(w.upper(), when)
                cs_src[w.upper()].add(tag)
        for line in speech:
            for m in NAME.finditer(line):
                w = m.group(0)
                if w.lower() in STOP:
                    continue
                key = w.upper()
                say[key] += 1
                say_first.setdefault(key, when)
                say_src[key].add(tag)
                before = line[max(0, m.start() - 24):m.start()]
                if key not in say_ctx or LOC.search(before):
                    say_ctx[key] = (when, group, line.strip()[:120], bool(LOC.search(before)))
        for line in marks:
            for w in NAME.findall(line):
                if w.lower() in STOP or w.upper() in ("МІТКИ", "КОМЕНТАР", "МІТКА"):
                    continue
                marks_names[w.upper()] += 1
                marks_first.setdefault(w.upper(), when)

    print("## 1. позивні (стоять у шапці перехоплення)")
    print()
    for w, c in cs.most_common(30):
        print(f"   {w:<16} {c:>3}  з {cs_first[w]:%d.%m}  [{'/'.join(sorted(cs_src[w]))}]")
    if not cs:
        print("   шапок зі станціями не знайдено")
    print()

    # --- 3. orientir candidates: in speech, not in a header; number form is the strong signal ---
    # A callsign in any case is still a callsign, so compare by stem, not by exact string.
    cs_stems = {stem(w) for w in cs}

    def is_callsign(w: str) -> bool:
        return any(same_name(w, c) for c in cs)

    numbered = collections.defaultdict(set)
    for when, text, group in both:
        _n, _s, speech, _m = split_intercept(text)
        for line in speech:
            for m in NAMENUM.finditer(line):
                w = m.group(1).upper()
                if w.lower() in STOP or stem(w) in cs_stems or is_callsign(w):
                    continue
                numbered[stem(w)].add(m.group(2))

    # fold the declensions of one name into one entry, keeping every spelling for the eye
    forms = collections.defaultdict(set)
    place = collections.Counter()
    for w, c in say.items():
        if stem(w) in cs_stems or is_callsign(w):
            continue
        place[stem(w)] += c
        forms[stem(w)].add(w)

    def label(key: str) -> str:
        v = sorted(forms[key], key=len)
        return v[0] if len(v) == 1 else f"{v[0]} ({'/'.join(v[1:])})"

    def ctx_of(key: str):
        for w in sorted(forms[key], key=lambda x: -say.get(x, 0)):
            if w in say_ctx:
                return say_ctx[w]
        return (min(w for w, _, _ in both), "—", "", False)
    print("## 2. орієнтири - КАНДИДАТИ з мови (не стоять у жодній шапці)")
    print()
    print("   Сильна ознака - назва з номером ділянки. Слабка - назва після локатива.")
    print()
    strong = [k for k in place if k in numbered]
    weak = [k for k in place if k not in numbered and place[k] >= 2 and ctx_of(k)[3]]
    for k in sorted(strong, key=lambda x: -place[x]):
        nums = ", ".join(sorted(numbered[k], key=lambda s: int(s)))
        when, group, line, _ = ctx_of(k)
        print(f"   ● {label(k):<26} ділянки: {nums:<16} {place[k]:>2} згадок, з {when:%d.%m}")
        print(f"       {group} | «{line}»")
    for k in sorted(weak, key=lambda x: -place[x]):
        when, group, line, _ = ctx_of(k)
        print(f"   ○ {label(k):<26} (без номера)        {place[k]:>2} згадок, з {when:%d.%m}")
        print(f"       {group} | «{line}»")
    if not strong and not weak:
        print("   кандидатів не знайдено")
    print()
    print("   Шаблони, якими шукали: назва+число, та велика літера після "
          "на/до/від/у/біля/р-н/ор/точка/лс.")
    print("   Чого ЦЕ не покриває: назву, сказану без прийменника й без номера, і назву, "
          "яку транскрибували з помилкою.")
    print()

    # --- 4. names that exist ONLY in an analyst's mark — different kind of knowledge ---
    say_stems = {stem(w) for w in say}
    only_mark = {w: c for w, c in marks_names.items()
                 if stem(w) not in cs_stems and stem(w) not in say_stems and len(w) > 3}
    print("## 3. назви, що є ТІЛЬКИ в мітках аналітика (в ефірі не звучали)")
    print()
    if only_mark:
        for w, c in sorted(only_mark.items(), key=lambda x: -x[1])[:25]:
            print(f"   {w:<18} {c:>3}  з {marks_first[w]:%d.%m}")
        print()
        print("   ⚠ це НЕ те, що ми чули. Це висновок аналітика. На питання «що чули» "
              "такі назви відповіддю не є.")
    else:
        print("   немає")
    print()

    # --- 5. coordinates for every candidate, searched over the WHOLE corpus ---
    want = sorted({sorted(forms[k], key=len)[0] for k in strong} | set(only_mark))
    if a.name:
        want = [a.name.upper()]
    print("## 4. координати (по ВСЬОМУ корпусу; лише сильні кандидати й назви з міток)")
    print()
    for w in want:
        rx = re.compile(re.escape(w), re.I)
        found = []
        for when, text, group in rows:
            if not rx.search(text):
                continue
            for line in text.split("\n"):
                if rx.search(line) and COORD.search(line):
                    for c in COORD_ONE.findall(line):
                        found.append((when, group, re.sub(r"\s+", " ", c)))
        uniq = list(dict.fromkeys((f"{c}", f"{d:%d.%m.%Y}", g) for d, g, c in found))
        if uniq:
            print(f"   {w}: привязок {len(uniq)}")
            for c, d, g in uniq[:10]:
                print(f"      {c}   {d}   [{g}]")
        else:
            print(f"   {w}: КООРДИНАТ НЕМА — шукали «{w}» по всьому корпусу разом із "
                  f"37T/MGRS у тому ж рядку")
    print()

    # --- 6. spelling neighbours: offered, never merged ---
    print("## 5. схожі написання - КАНДИДАТИ, НЕ ЗЛИТО")
    print()
    pool = collections.defaultdict(set)
    for w in set(list(cs) + list(say) + list(marks_names)):
        if len(w) > 3:
            pool[skeleton(w)].add(w)
    shown = 0
    for sk, group in sorted(pool.items(), key=lambda x: -len(x[1])):
        if len(group) < 2:
            continue
        print("   " + "  ~  ".join(sorted(group)))
        shown += 1
        if shown >= 12:
            break
    if not shown:
        print("   схожих пар немає")
    print()
    print("   Ці назви лише ЗВУЧАТЬ схоже. 09.10.2026 я на цій підставі оголосив «Аккордеон 7»")
    print("   і «АКОРД 7» одним місцем - вони різні, і аналітик пише обидві окремо.")
    print("   Злити дві назви можна тільки за одним з трьох доказів:")
    print("     1) мітка аналітика на ТОМУ Ж перехопленні називає друге ім'я;")
    print("     2) координати обох відомі й лежать в межах ~100 м;")
    print("     3) у мові хтось прямо сказав, що це одне й те саме.")
    print("   Інакше вони лишаються двома різними орієнтирами.")
    print()

    print("## чого ця картка не бачить")
    print("   - мережу, яку наші групи взагалі не підписали цією частотою")
    print("   - назву, сказану без прийменника й без номера")
    print("   - зв'язок «хто кого водить» (це інша задача, тут лише хто звучить)")
    print("   - чи орієнтир ще живий: координата може бути з серпня, а смуга вже інша")
    return 0


if __name__ == "__main__":
    sys.exit(main())
