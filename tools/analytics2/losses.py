"""Casualty count under a net block — people, not lines.

Spec: `knowledge/upstream/losses-count-tz.md`. The whole problem is in the first sentence: lines are
not people. МАГА produced four `300` lines on 03.10 and МОРОЗИК was `300` at 08:53 and `200` at
09:13 — counting lines would print eleven casualties where there were four men. So this counts
DISTINCT PEOPLE, each in his worst state, and keeps the lines each person was folded from so the
number can be checked back against the report.

Three sides live in the same lines and must never be summed: theirs (the count), ours (`в\\с СОУ`,
never counted) and an ally's (`союзник ВАСЯ - 300`, counted apart after a slash).

Mechanical and model-free on purpose, so `--render-only` produces the same number as a full run and
two runs of the same report cannot disagree.
"""
from __future__ import annotations

import re

# A line is about a casualty if it carries one of these. `зник` is deliberately NOT here: a man who
# stopped answering is not a loss until somebody says he is.
CASUALTY_RE = re.compile(r"\b200\b|\b300\b|поранен|загибель|загинув|\bтіло\b|вбит", re.I)
KILLED_RE = re.compile(r"\b200\b|загибель|загинув|\bтіло\b|вбит", re.I)

# How close to the marker a callsign has to stand to be the casualty rather than a bystander.
NEAR = 30
# The marker itself: the word that says somebody is a loss.
MARKER_RE = re.compile(r"\b200\b|\b300\b|поранен\w*|загибель|загинув|\bтіло\b|вбит\w*|трьохсот\w*|двохсот\w*", re.I)
# A name right after one of these is somewhere he is going or somebody who helps — not the casualty.
NOT_SUBJECT_RE = re.compile(
    r"(?:до|біля|поруч з|повз|супровід|супроводі|ведення|веде|координац|керівництв|зустріч|позиції)\s*$"
    # a landmark is not a man: `ор ГОРОХ`, `т ЛЕНИНА`, `лс НЕВА`, `кв ЦВЕТ`
    r"|(?:\bор|\bт|\bлс|\bкв|\bн\.?п)\s*\.?\s*$", re.I)
NAME_RE = None   # set below

# `в\с ПЕТРО`, `о\с РОВ`, `гр ЗЫБА`, and lists: `в\с БОГОМОЛ, ГОРИЗОНТ`
SUBJ_RE = re.compile(r"(?:в[\\/]с|о[\\/]с|гр)\s+((?:[А-ЯЁЇІЄ][А-ЯЁЇІЄ0-9'’-]{2,})(?:\s*,\s*[А-ЯЁЇІЄ][А-ЯЁЇІЄ0-9'’-]{2,})*)")
# A bare callsign in front of the marker: `МОРОЗИК (РОВ) - 200`, `КУБИК - 300`
BARE_RE = re.compile(r"(?:^|[;,]\s*)([А-ЯЁЇІЄ][А-ЯЁЇІЄ0-9'’-]{2,})\s*(?:\([^)]*\))?\s*[-—–]\s*(?:ім\s*)?\b[23]00\b")

# `СОУ` means "ours" only when it qualifies the PERSON. Next to a drone it qualifies the aircraft,
# and `ВУ БпЛА СОУ по позиції ... 300 в\с ІВАН` is their wounded man, not ours. This is the single
# place where a wrong reading would silently add our losses to their total.
NAME_RE = re.compile(
    r"(?:в[\\/]с|о[\\/]с|гр|пораненого|трьохсотим|двохсотим|союзник\w*)\s+(?:\(ім\s+)?((?:[А-ЯЁЇІЄ][А-ЯЁЇІЄ0-9'’-]{2,})(?:\s*,\s*[А-ЯЁЇІЄ][А-ЯЁЇІЄ0-9'’-]{2,})*)"
    r"|(?:^|[;,]\s*)([А-ЯЁЇІЄ][А-ЯЁЇІЄ0-9'’-]{2,})(?=\s*(?:\([^)]*\))?\s*[-—–]\s*(?:ім\s*)?\b[23]00\b)")

DRONE_SOU_RE = re.compile(r"(?:бпла|фпв|борт|дрон|вампір|бабка|баба\s*яга|ждун|мавік|мавик)\s*[^,;]{0,18}соу", re.I)
ALLY_RE = re.compile(r"союзник", re.I)
# Our own abbreviations stand where a callsign stands but name nobody: `о\с РОВ` is a serviceman of
# theirs with no callsign at all — the «без позивного» case, not a man called РОВ.
NOT_A_CALLSIGN = {"РОВ", "СОУ", "ІМ", "ИМ", "ВУ", "БПЛА", "ФПВ", "МТЗ", "БК", "КНП"}

VOWELS = "аеёиоуыэюяіїє"


def _fold(s: str) -> str:
    """One spelling for a man written in Russian and in Ukrainian — same rule as run.py's."""
    t = (s or "").lower()
    for a, b in (("і", "и"), ("ї", "и"), ("ы", "и"), ("й", "и"),
                 ("є", "е"), ("э", "е"), ("ё", "е"), ("ґ", "г")):
        t = t.replace(a, b)
    return t.replace("ь", "").replace("ъ", "")


def _side(text: str, at: int) -> str:
    """ours | ally | theirs — judged from the words AROUND the subject, not from the whole line.

    The ally window is deliberately TIGHT and the ours window is wide, because the two mistakes are
    not symmetric. `союзник` reaches a man only when it stands right in front of him: in
    `300 о\\с РОВ поруч з укриттям «союзників»` the casualty is theirs and the allies are a landmark,
    and a ±60 window read that as an ally's loss. `СОУ` next to a person, on the other hand, must be
    seen from further away — counting our dead into their total is the one error we never ship.
    """
    at = max(at, 0)
    # `союзник` marks a side only where it QUALIFIES the man: right in front of him, or in brackets
    # right after. Anywhere else in the line it is somebody standing nearby — in
    # `300 о\\с РОВ, союзники допомагають з евакуацією` the casualty is theirs and the allies are the
    # stretcher party, and in `поруч з укриттям «союзників»` they are a landmark. Both were read as
    # an ally's loss while this looked at a window instead of at the grammar.
    if re.search(r"союзник\w*\s*$", text[max(0, at - 20): at], re.I) \
            or re.match(r"[А-ЯЁЇІЄ0-9'’-]*\s*\([^)]{0,12}союзник", text[at:], re.I):
        return "ally"
    near = text[max(0, at - 60): at + 60]
    for m in re.finditer(r"соу", near, re.I):
        chunk = near[max(0, m.start() - 30): m.end()]
        if not DRONE_SOU_RE.search(chunk):
            return "ours"
    return "theirs"


def collect(events: list[dict]) -> dict:
    """Fold a block's events into one row per person.

    The subject is taken from NEXT TO the casualty marker, not from anywhere in the line. That one
    rule is what separates the wounded man from the people around him: in
    `поранення в\\с АПЕЛЬСИН (300) ... під час переміщення гр ВАЛУН` only АПЕЛЬСИН is a casualty,
    and in `переміщення в\\с ЗУМ з трьохсотим СЛАВА до в\\с БУРЫЙ` only СЛАВА is. A first version
    took every callsign in the line and produced ten casualties out of eight lines.

    Returns {"rows": {key: {...}}, "unnamed": [...], "lines": N} — rows keep the texts they came
    from, which is what makes the printed number auditable.
    """
    rows: dict[str, dict] = {}
    unnamed: dict[tuple, dict] = {}
    n_lines = 0

    for e in events:
        text = str(e.get("text") or "")
        if not CASUALTY_RE.search(text):
            continue
        n_lines += 1

        found: list[tuple[str, int, str]] = []        # (name, pos, state)
        seen_spans: set[tuple[int, int]] = set()

        def add_unnamed(state: str, at: int) -> None:
            """A casualty nobody named. One per distinct state and time, with a body multiplier."""
            mult = 1
            m = re.search(r"(\d+)\s*(?:тіл|в[\\/]с|о[\\/]с|бійц)", text)
            if m and int(m.group(1)) in range(2, 10):
                mult = int(m.group(1))
            side = _side(text, at)
            if side == "ours":
                return
            key = (state, side, e.get("time"))
            u = unnamed.setdefault(key, {"state": state, "side": side, "n": mult,
                                         "lines": [], "text": text})
            u["lines"].append(text)
            u["n"] = max(u["n"], mult)

        for mk in MARKER_RE.finditer(text):
            state = "200" if re.match(r"200|двохсот|загиб|тіло|вбит", mk.group(0), re.I) else "300"
            best = None
            for nm in NAME_RE.finditer(text):
                pos = nm.start(1) if nm.group(1) is not None else nm.start(2)
                if abs(pos - mk.start()) > NEAR:
                    continue
                before = text[max(0, nm.start() - 22): nm.start()].lower()
                if NOT_SUBJECT_RE.search(before):
                    continue
                # `з трьохсотим СЛАВА` / `пораненого (ім АПЕЛЬСИН)` name the casualty outright, so
                # they beat a neighbour who merely stands closer to the digits.
                d = -1 if re.match(r"(?:пораненого|трьохсотим|двохсотим)", nm.group(0), re.I) \
                    else abs(pos - mk.start())
                if best is None or d < best[1]:
                    best = (nm, d)
            if best is None:
                if found:            # this line already named somebody; a second marker in the
                    continue         # same line is the same casualty, not an extra body
                add_unnamed(state, mk.start())
                continue
            nm = best[0]
            if (nm.start(), nm.end()) in seen_spans:
                continue
            seen_spans.add((nm.start(), nm.end()))
            at = nm.start(1) if nm.group(1) is not None else nm.start(2)
            named = False
            for part in re.split(r"\s*,\s*", (nm.group(1) or nm.group(2))):
                part = part.strip()
                if part and part not in NOT_A_CALLSIGN:
                    found.append((part, at, state))
                    named = True
            # `300 о\с РОВ` matched the subject pattern but named nobody — it is still a casualty,
            # and the whole marker used to be dropped here. Three such lines went uncounted on
            # 05.10.2026 before this branch existed.
            if not named and not found:
                add_unnamed(state, at)

        for name, at, state in found:
            side = _side(text, at)
            if side == "ours":
                continue                      # our people are visible but never in their total
            key = _fold(name)
            row = rows.setdefault(key, {"name": name, "side": side, "state": state, "lines": []})
            row["lines"].append(text)
            if state == "200":                # 200 beats 300 for the same man
                row["state"] = "200"
            if side == "ally":
                row["side"] = "ally"

    # A line may name its man too far from the digits to be matched — `в\\с ВЕТЕР лишається один на
    # позиції, лежить (ім 300)` is 45 characters wide — and then he is counted once by name from
    # another line and once more as a nameless body here. If the line names somebody who already
    # stands in the count IN THE SAME STATE, the nameless entry is that same man.
    cs_re = re.compile(r"\b[А-ЯЁЇІЄ][А-ЯЁЇІЄ0-9'’-]{2,}\b")
    kept = []
    for u in unnamed.values():
        same = any(_fold(c) in rows and rows[_fold(c)]["state"] == u["state"]
                   for c in cs_re.findall(u.get("text", "")) if c not in NOT_A_CALLSIGN)
        if not same:
            kept.append(u)
    return {"rows": rows, "unnamed": kept, "lines": n_lines}


# Only a casualty with a callsign is counted (owner's decision, 06.10.2026). A nameless body is
# real but unattributable, and three of them in one block were a guess dressed as a number: `2 тіла,
# приналежність не встановлена` seen at two times is either two men or four, and `300 о\с РОВ` is a
# man we cannot follow, name or cross-check tomorrow. The count now answers one question only — how
# many NAMED men of theirs this net lost — and that is the question worth a number.
# The nameless ones are still extracted and kept in `data["unnamed"]`: they stand in the events, and
# turning them back on is this one flag.
COUNT_UNNAMED = False


def counts(data: dict) -> dict:
    out = {"200": 0, "300": 0, "200_unnamed": 0, "300_unnamed": 0,
           "ally_200": 0, "ally_300": 0}
    for r in data["rows"].values():
        pref = "ally_" if r["side"] == "ally" else ""
        out[f"{pref}{r['state']}"] = out.get(f"{pref}{r['state']}", 0) + 1
    if COUNT_UNNAMED:
        for u in data["unnamed"]:
            pref = "ally_" if u["side"] == "ally" else ""
            n = int(u.get("n", 1))
            out[f"{pref}{u['state']}"] = out.get(f"{pref}{u['state']}", 0) + n
            if not pref:
                out[f"{u['state']}_unnamed"] += n
    return out


def invariants(data: dict, c: dict) -> list[str]:
    """What must hold before a number is printed. A wrong number is worse than no number."""
    bad = []
    total = c["200"] + c["300"] + c["ally_200"] + c["ally_300"]
    capacity = data["lines"]
    if COUNT_UNNAMED:
        capacity += sum(max(0, int(u.get("n", 1)) - 1) for u in data["unnamed"])
    if total > capacity:
        bad.append(f"людей ({total}) більше, ніж рядків про втрати ({capacity})")
    for r in data["rows"].values():
        if not r["lines"]:
            bad.append(f"{r['name']}: порахований, але не стоїть у жодному рядку")
        if r["side"] == "ours":
            bad.append(f"{r['name']}: позначений СОУ і все одно в рахунку")
    if c["200_unnamed"] > c["200"] or c["300_unnamed"] > c["300"]:
        bad.append("безіменних більше, ніж усього")
    return bad


def render(c: dict) -> str:
    """`Втрати: 200 - 2, 300 - 5 (2 без позивного) / союзник 300 - 1`

    Brackets mean ONE thing — how many of that number have no callsign. Allies live after a slash
    and are spelled out, so the two can never be read for each other.
    """
    if not any(c.values()):
        return ""
    parts = []
    for st in ("200", "300"):
        if c[st]:
            tail = f" ({c[f'{st}_unnamed']} без позивного)" if c[f"{st}_unnamed"] else ""
            parts.append(f"{st} - {c[st]}{tail}")
    ally = [f"союзник {st} - {c['ally_' + st]}" for st in ("200", "300") if c["ally_" + st]]
    line = "Втрати: " + ", ".join(parts) if parts else "Втрати:"
    if ally:
        line = (line + " / " + ", ".join(ally)) if parts else "Втрати: " + ", ".join(ally)
    return line


def line_for(events: list[dict]) -> tuple[str, dict, list[str]]:
    """-> (printable line or "", counts, broken invariants)."""
    data = collect(events)
    c = counts(data)
    bad = invariants(data, c)
    return ("" if bad else render(c)), c, bad
