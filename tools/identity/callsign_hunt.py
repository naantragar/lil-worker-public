#!/usr/bin/env python3
"""Look a list of callsigns up in everything we have heard.

    python3 tools/identity/callsign_hunt.py Мухомор Вальщик Лис --alias Кирей=Керя
    python3 tools/identity/callsign_hunt.py --file names.txt --examples 3

Answers one question: have WE heard this name on the air, and where. Two things make it more than
a grep:

**Source priority.** Invisible Hand and AllInARow are what we hear ourselves and are reported
first; Patagonia is a second opinion and is counted separately. A name present only in Patagonia
is a weaker answer than the same count in IH.

**Header beats speech.** A callsign written into the intercept's header was identified by the
analyst; the same letters inside speech may be an ordinary word («белый», «зима», «лис»). Both are
counted, but never mixed: the header column is evidence, the speech column is a lead.

Declensions are handled by stemming the name and allowing the usual Russian endings, because the
air says «Мухомора», «Вальщику», «на Лешего».
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

CORPUS = "~/wa-monitor/messages.db"
TRANSFER_JID = "120363408965106490@g.us"
PRIMARY = ("Invisible Hand", "AllInARow", "All in a Row")
FREQ_LINE = re.compile(r"^\d{2,4}[.,]\d{2,4}$")
SPEECH_LINE = re.compile(r"^\s*(\d+\s*)?[–—-]\s")
RE_AIR_DATE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4}),")

# names that are also ordinary words — a speech hit means nothing without a callsign frame
ORDINARY = {"белый", "зима", "лис", "запал", "норд", "мухомор"}
# «я Белый», «Белый, приём», «на Белого», «Белый 202» — the shapes a callsign takes in speech
FRAME = (r"(?:я|это|для|на|от|к|у)\s+{w}\b", r"\b{w}\s*,\s*(?:я|прием|приём|ответь|на связи)",
         r"\b{w}\b\s*(?:на связи|принял|ответь|прием|приём)")


WORD = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]{3,}")

# Приголосні, які плутають на слух і в розшифровці: аналітик пише зі слуху, тому «Вальщик»
# може стати «Валчик», а «Мухомор» - «Мухамор». Ключ згортає такі пари в один символ, і слова,
# що звучать однаково, дають однаковий ключ.
SOUND = str.maketrans({
    "б": "б", "п": "б", "в": "в", "ф": "в", "г": "г", "к": "г", "х": "г",
    "д": "д", "т": "д", "ж": "ж", "ш": "ж", "щ": "ж", "ч": "ж", "з": "ж", "с": "ж", "ц": "ж",
    "о": "а", "ы": "и", "і": "и", "й": "и", "е": "и", "є": "и", "э": "и", "ё": "а", "я": "а",
    "ю": "у", "ї": "и", "ґ": "г",
    "ь": "", "ъ": "",
})


def sound_key(w: str) -> str:
    k = w.lower().translate(SOUND)
    return re.sub(r"(.)\1+", r"\1", k)


def levenshtein(a: str, b: str, cap: int = 2) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def fuzzy_hits(text: str, name: str) -> list[str]:
    """Слова, що звучать як шуканий позивний або відрізняються на одну-дві літери.

    Ловить три речі, яких не бачить точний пошук: помилку розшифровки («Мухамор»), інше
    написання того ж звуку («Валчик»), і розрив слова («Мухо мор» - через текст без пробілів)."""
    n = name.lower()
    nk = sound_key(n)
    cap = 2 if len(n) >= 6 else 1
    out = set()
    for m in WORD.finditer(text):
        w = m.group(0).lower()
        if w == n:
            continue
        if sound_key(w) == nk or levenshtein(w, n, cap) <= cap:
            out.add(m.group(0))
    glued = re.sub(r"[\s-]+", "", text.lower())
    if n in glued and n not in text.lower():
        out.add(f"{name} (написано з розривом)")
    return sorted(out)


def stem_pattern(name: str) -> str:
    n = name.strip().lower()
    if n.endswith("ий"):                       # Леший -> леш(ий|его|ему|им|ем)
        return n[:-2] + r"(?:ий|его|ему|им|ем|ий)"
    if n.endswith("ый"):                       # Белый -> бел(ый|ого|...)
        return n[:-2] + r"(?:ый|ого|ому|ым|ом)"
    if n.endswith("ей"):                       # Кирей -> кире(й|я|ю|ем|е)
        return n[:-2] + r"(?:ей|я|ю|ем|е)"
    if n.endswith("я"):                        # Керя -> кер(я|и|е|ю|ей)
        return n[:-1] + r"(?:я|и|е|ю|ей)"
    if n.endswith("а"):                        # Зима -> зим(а|ы|е|у|ой)
        return n[:-1] + r"(?:а|ы|е|у|ой)"
    return n + r"(?:а|у|ом|е|ы|и)?"            # Мухомор, Вальщик, Норд, Лис, Монгол


def speech(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if SPEECH_LINE.match(l))


def header_callsigns(text: str) -> str:
    """Everything between the net line and the first speech line: the analyst's callsign lines."""
    lines = [l.strip() for l in text.splitlines()]
    out, started = [], False
    for l in lines:
        if SPEECH_LINE.match(l):
            break
        if started and l and not FREQ_LINE.match(l):
            out.append(l)
        if RE_AIR_DATE.match(l):
            started = True
    return "\n".join(out)


def net_of(text: str) -> str:
    for l in (x.strip() for x in text.splitlines()):
        if "р/м" in l or "УКХ" in l.upper():
            return l
    return ""


def freq_of(text: str) -> str:
    for l in (x.strip() for x in text.splitlines()):
        if FREQ_LINE.match(l):
            return l.replace(",", ".")
    return "?"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*")
    ap.add_argument("--file", default="")
    ap.add_argument("--alias", action="append", default=[],
                    help="Кирей=Керя — друге написання того самого позивного")
    ap.add_argument("--examples", type=int, default=2)
    ap.add_argument("--fuzzy", action="store_true",
                    help="шукати за звучанням і з допуском на 1-2 літери")
    ap.add_argument("--ours", action="store_true",
                    help="тільки наші джерела - Invisible Hand і AllInARow")
    a = ap.parse_args()

    names = list(a.names)
    if a.file:
        names += [l.strip() for l in Path(a.file).read_text(encoding="utf-8").splitlines() if l.strip()]
    if not names:
        sys.exit("дай хоч один позивний")
    alias = {}
    for pair in a.alias:
        k, _, v = pair.partition("=")
        alias.setdefault(k.strip(), []).append(v.strip())

    probes = {}
    for n in names:
        variants = [n] + alias.get(n, [])
        pat = "|".join(stem_pattern(v) for v in variants)
        probes[n] = (re.compile(rf"\b(?:{pat})\b", re.I), variants)

    con = sqlite3.connect(f"file:{CORPUS}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT timestamp, group_name, coalesce(text,'') FROM messages "
        "WHERE coalesce(group_jid,'') != ? AND coalesce(category,'') != 'transfer'",
        (TRANSFER_JID,)).fetchall()
    if a.ours:
        rows = [r for r in rows if r[1] in PRIMARY]
    con.close()
    print(f"перевірено {len(rows)} перехоплень\n", file=sys.stderr)

    hits: dict[str, dict] = {n: {"header": [], "speech": []} for n in names}
    for ts, group, text in rows:
        head = header_callsigns(text)
        sp = speech(text)
        if not sp and not head:
            continue
        for n, (rx, _) in probes.items():
            where = None
            if rx.search(head):
                where = "header"
            elif rx.search(sp):
                if n.lower() in ORDINARY:
                    framed = any(re.search(f.format(w=rx.pattern), sp, re.I) for f in FRAME)
                    if not framed:
                        continue
                where = "speech"
            if where:
                hits[n][where].append((ts, group, freq_of(text), net_of(text), sp[:300]))

    if a.fuzzy:
        # СЛОВНИК, А НЕ ДОКУМЕНТИ. Порівнювати кожне слово кожного перехоплення з іменем - це
        # мільйони порівнянь і хвилини очікування. Але різних СЛІВ у корпусі на два порядки
        # менше, ніж слововживань: збираємо словник один раз, шукаємо схожі в ньому, і лише
        # потім піднімаємо перехоплення, де ці варіанти справді стоять.
        vocab: dict[str, int] = defaultdict(int)
        for _, _, text in rows:
            for m in WORD.finditer(text):
                vocab[m.group(0).lower()] += 1
        print("=" * 76)
        print(f"НЕЧІТКИЙ ПОШУК: за звучанням і з допуском на літери · словник {len(vocab)} слів")
        for n in names:
            low = n.lower()
            nk = sound_key(low)
            cap = 2 if len(low) >= 6 else 1
            variants = {w: c for w, c in vocab.items() if w != low
                        and (sound_key(w) == nk or levenshtein(w, low, cap) <= cap)}
            glued = [(ts, g) for ts, g, txt in rows
                     if low in re.sub(r"[\s-]+", "", txt.lower()) and low not in txt.lower()]
            if not variants and not glued:
                print(f"  {n.upper():<12} нічого — ані схожого слова, ані розриву")
                continue
            print(f"  {n.upper():<12} схожих слів у словнику: {len(variants)}")
            for w, c in sorted(variants.items(), key=lambda x: -x[1])[:10]:
                rx = re.compile(r"\b" + re.escape(w) + r"\b", re.I)
                where = [(ts, g, txt) for ts, g, txt in rows if rx.search(txt)]
                ours = [r for r in where if r[1] in PRIMARY]
                ex = (ours or where)[0]
                d = datetime.fromtimestamp(ex[0] / 1000).strftime("%d.%m")
                print(f"      «{w}» × {c}  (наші {len(ours)} з {len(where)})  напр. {d} "
                      f"{ex[1]} {freq_of(ex[2])}")
            if glued:
                print(f"      написано з розривом: {len(glued)} разів")
        print()

    for n in names:
        h, s = hits[n]["header"], hits[n]["speech"]
        def split(rs):
            p = [r for r in rs if r[1] in PRIMARY]
            return p, [r for r in rs if r[1] not in PRIMARY]
        hp, hs = split(h)
        sp_, ss = split(s)
        mark = "ЧУЄМО" if (hp or sp_) else ("тільки Патагонія" if (hs or ss) else "НЕ ЧУЛИ")
        print("=" * 76)
        print(f"{n.upper():<12} {mark}")
        print(f"  у шапці (позивний упізнано аналітиком): наші {len(hp)}, Патагонія {len(hs)}")
        print(f"  у мові  (згадка в розмові):             наші {len(sp_)}, Патагонія {len(ss)}")
        nets = defaultdict(int)
        for ts, g, f, net, _ in (hp + sp_ if a.ours else h + s):
            nets[(f, net[:70])] += 1
        for (f, net), c in sorted(nets.items(), key=lambda x: -x[1])[:6]:
            print(f"    {f:>9}  {c:>4}  {net}")
        for rs, label in ((hp, "шапка, наші"), (sp_, "мова, наші"), (hs, "шапка, Патагонія")):
            for ts, g, f, net, txt in rs[:a.examples]:
                d = datetime.fromtimestamp(ts / 1000).strftime("%d.%m %H:%M")
                print(f"    [{label}] {d} · {f} · {g}")
                print("        " + txt.replace("\n", "\n        ")[:280])


if __name__ == "__main__":
    main()
