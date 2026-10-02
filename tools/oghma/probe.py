#!/usr/bin/env python3
"""probe — one deterministic answer to "what is this word in the traffic, and whose is it".

Stage 1 of `knowledge/upstream/word-probe-tz.md`: gather and slice, NEVER filter by meaning. The tool
finds the word, splits the hits by net / group / day / station-vs-speech, and prints the raw lines.
What the word MEANS is read by a human from those lines — it is not decided by a keyword list.

That rule is the whole point. On 01.10.2026 a hand-made "air context" filter (`висе|сопровожд|…`)
missed `висить` and every word the net actually uses, and the owner was told a callsign had been
silent since August while it was on the air that morning. A filter can prove presence; it can never
prove absence, and a conclusion drawn from one is a conclusion about the filter.

    python3 tools/upstream/probe.py заяц [--days 14] [--all] [--net 148.6500] [--lines 8]
                                      [--forms "заяц,зайца"] [--no-save]

Stdlib only; reads the corpus read-only.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CORPUS_DB = Path(os.environ.get("UPSTREAM_MESSAGES_DB", "~/wa-monitor/messages.db"))
PROBES_DIR = REPO / "knowledge" / "upstream" / "probes"
OURS = [
    REPO / "knowledge" / "upstream" / "reports_out",      # what we have already printed
    REPO / "tools" / "analytics2" / "callsign_notes.json",
    REPO / "knowledge" / "upstream",                      # top-level notes
]

FREQ_RE = re.compile(r"^(\d{3}\.\d{4})$")
NET_RE = re.compile(r"^\s*(укх|уквч|ув)\b", re.I)
SPEECH_RE = re.compile(r"^\s*(\d\s*[-—–]|[-—–])")
# "1-ЗАЯЦ," is a STATION line in one collector's format, not a spoken line: digits, a dash and
# nothing but capitals. Without this the roster leaks into the quoted speech.
STATION_LINE_RE = re.compile(r"^\s*\d+\s*[-—–]\s*[А-ЯЁЇІЄA-Z0-9 ,./-]+$")
WORD_RE = re.compile(r"[А-Яа-яЁёA-Za-z]{3,}")

# Consonant/vowel confusions the transcription actually makes. Used ONLY to offer neighbours as
# separate candidates — never merged into the main count (БЕТЕР / ВЕТЕР / БАТЕР / БАТТЕР).
SWAPS = [("б", "в"), ("б", "п"), ("в", "ф"), ("г", "к"), ("г", "х"), ("к", "х"),
         ("с", "з"), ("т", "д"), ("ж", "ш"), ("ч", "щ"), ("а", "о"), ("е", "и"),
         ("е", "э"), ("и", "ы"), ("ю", "у"), ("я", "а")]


def lev(a: str, b: str) -> int:
    """Plain Levenshtein — short words only, no dependency worth adding for it."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def split_intercept(text: str) -> tuple[str, str, list[str], list[str]]:
    """-> (freq, net header, station lines, speech lines).

    The station block is what stands between the net header and the first spoken line. The split
    matters more than it looks: a word in the STATION list is a correspondent of that net, the same
    word inside the dialogue is something being talked about. ЗАЯЦ is a station on 437.0650 and is
    only ever spoken about on 148.6500 — one is a man, the other a drone, and nothing else in the
    record separates them.
    """
    lines = text.split("\n")
    freq, net, stations, speech = "", "", [], []
    i, n = 0, len(lines)
    while i < n and not FREQ_RE.match(lines[i].strip()):
        i += 1
    if i < n:
        freq = lines[i].strip()
        i += 1
        if i < n and NET_RE.match(lines[i]):
            net = lines[i].strip()
            i += 1
    while i < n:
        ln = lines[i]
        if SPEECH_RE.match(ln) and not STATION_LINE_RE.match(ln):
            break
        if ln.strip():
            stations.append(ln.strip())
        i += 1
    speech = [ln.strip() for ln in lines[i:]
              if ln.strip() and not STATION_LINE_RE.match(ln)]
    stations += [ln.strip() for ln in lines[i:] if STATION_LINE_RE.match(ln)]
    return freq, net, stations, speech


VOWELS = set("аеёиоуыэюяіїєa eiouy".replace(" ", ""))


def skeleton(word: str) -> tuple:
    """The consonants of a word, with `й` dropped.

    This is what survives inflection: `заяц → зайца → зайцем` all keep з…ц, while `заря`, `зад`,
    `зато` do not. `й` goes because it is exactly the letter that alternates with `я/е` in the
    stem. Matching on the skeleton instead of a prefix is what makes `гуся` stop swallowing
    `густая` — same first letters, different skeleton.
    """
    return tuple(c for c in word.casefold() if c not in VOWELS and c != "й")


def collect_forms(con, query: str, explicit: list[str] | None) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Which spellings of the word the corpus actually holds — and which look-alikes were dropped.

    Nothing is cut silently: the rejected list is printed too, because the ЗАЯЦ miss had a twin in
    the «гуся» search, where `гус*` silently swallowed every `густий` in the band.
    """
    q = query.casefold()
    numeric = not any(c.isalpha() for c in q)
    counts: collections.Counter = collections.Counter()
    rejected_counts: collections.Counter = collections.Counter()
    sk = skeleton(q)
    for (text,) in con.execute("SELECT text FROM messages WHERE text IS NOT NULL"):
        if numeric:
            if re.search(rf"(?<!\d){re.escape(q)}(?!\d)", text):
                counts[q] += 1
            continue
        for tok in WORD_RE.findall(text):
            t = tok.casefold()
            if t[:2] != q[:2] or not (len(q) - 1 <= len(t) <= len(q) + 3):
                continue
            (counts if skeleton(t) == sk else rejected_counts)[t] += 1
    if explicit:
        keep = {f.casefold() for f in explicit}
        taken = [(f.casefold(), counts.get(f.casefold(), 0) + rejected_counts.get(f.casefold(), 0))
                 for f in explicit]
        rest = collections.Counter(counts) + collections.Counter(rejected_counts)
        return taken, [(t, c) for t, c in rest.most_common() if t not in keep][:20]
    return counts.most_common(), rejected_counts.most_common(20)


def phonetic_neighbours(query: str) -> list[str]:
    q = query.casefold()
    out = set()
    for a, b in SWAPS:
        for x, y in ((a, b), (b, a)):
            if x in q:
                out.add(q.replace(x, y, 1))
    for i in range(1, len(q)):                      # doubled consonant: батер -> баттер
        if q[i] not in "аеёиоуыэюя":
            out.add(q[:i] + q[i] + q[i:])
    return sorted(out - {q})


def ours(forms: list[str]) -> list[str]:
    """What WE already say about the word — read before the corpus, always."""
    alts = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
    hits = []
    pat = re.compile(rf"(?<![А-Яа-яЁёЇїІіЄєA-Za-z])({alts})(?![А-Яа-яЁёЇїІіЄєA-Za-z])", re.I)
    for root in OURS:
        files = []
        if root.is_file():
            files = [root]
        elif root.is_dir():
            # The final reports and our own notes only. The events/sources dumps repeat every line
            # three times over, and LAYOUT_/TEST_ files are old experiments — both drown the section
            # that is supposed to be read first.
            files = sorted(f for f in list(root.glob("*.md")) + list(root.glob("*.txt"))
                           if not re.search(r"LAYOUT|TEST|_sources|_events|_dubli", f.name))
        for f in files:
            try:
                for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                    if pat.search(line):
                        hits.append(f"{f.relative_to(REPO)}:{n}: {line.strip()[:170]}")
            except (OSError, UnicodeDecodeError):
                continue
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="probe a word across the intercept corpus")
    ap.add_argument("word")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--all", action="store_true", help="scan the whole corpus, not a window")
    ap.add_argument("--net", help="restrict to one frequency (follow-up pass only)")
    ap.add_argument("--lines", type=int, default=8, help="raw lines printed per net")
    ap.add_argument("--forms", help="comma-separated spellings to use instead of the automatic ones")
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{CORPUS_DB}?mode=ro", uri=True)
    out: list[str] = []

    def say(s: str = "") -> None:
        out.append(s)
        print(s)

    now = dt.datetime.now()
    lo_dt = dt.datetime(2000, 1, 1) if a.all else now - dt.timedelta(days=a.days)
    lo = int(lo_dt.timestamp() * 1000)
    window = "весь корпус" if a.all else f"останні {a.days} діб (з {lo_dt:%d.%m.%Y})"

    say(f"# probe: «{a.word}»")
    say()
    say(f"вікно: {window} · корпус: {CORPUS_DB} · знято {now:%d.%m.%Y %H:%M}")
    say()

    taken, rejected = collect_forms(con, a.word, a.forms.split(",") if a.forms else None)
    forms = {t for t, c in taken if c} or {a.word.casefold()}

    say("## 1. Що МИ вже про це пишемо")
    say()
    o = ours(sorted(forms))
    if o:
        for h in o[:20]:
            say(f"- `{h}`")
        if len(o) > 20:
            say(f"- … ще {len(o) - 20} рядків у наших файлах")
    else:
        say("- нічого в наших звітах, легендах і нотатках")
    say()

    say("## 2. Форми слова в корпусі")
    say()
    say("взято: " + (", ".join(f"{t} ({c})" for t, c in taken if c) or "—"))
    say("відкинуто як схоже, але інше: " + (", ".join(f"{t} ({c})" for t, c in rejected[:12]) or "—"))
    say()
    neigh = phonetic_neighbours(a.word)
    say("## 3. Фонетичні сусіди (окремі кандидати, у рахунок не входять)")
    say()
    found_n = []
    for cand in neigh:
        c = con.execute("SELECT count(*) FROM messages WHERE lower(text) LIKE ?", (f"%{cand}%",)).fetchone()[0]
        if c:
            found_n.append(f"{cand} ({c})")
    say(", ".join(found_n) if found_n else "— нічого")
    say()

    alts = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
    # Whole words only: the form `зац` must not light up inside `зацепить`.
    form_re = re.compile(rf"(?<![А-Яа-яЁёЇїІіЄєA-Za-z])({alts})(?![А-Яа-яЁёЇїІіЄєA-Za-z])", re.I)

    per_net = collections.defaultdict(lambda: {"st": 0, "sp": 0, "net": "", "groups": collections.Counter()})
    cover = collections.Counter()
    by_day = collections.Counter()
    company = collections.Counter()
    samples = collections.defaultdict(list)
    first_seen = last_seen = None
    total = 0

    for ts, grp, text in con.execute(
            "SELECT timestamp, group_name, text FROM messages WHERE timestamp>=? AND text IS NOT NULL"
            " ORDER BY timestamp", (lo,)):
        freq, net, stations, speech = split_intercept(text)
        if a.net and freq != a.net:
            continue
        cover[freq or "?"] += 1
        if not form_re.search(text):
            continue
        d = dt.datetime.fromtimestamp(ts / 1000)
        total += 1
        first_seen = first_seen or (d, freq)
        last_seen = (d, freq)
        by_day[f"{d:%d.%m}"] += 1
        rec = per_net[freq or "?"]
        rec["net"] = rec["net"] or net
        rec["groups"][grp or "?"] += 1
        if any(form_re.search(s) for s in stations):
            rec["st"] += 1
        if any(form_re.search(s) for s in speech):
            rec["sp"] += 1
        for s in stations:
            for tok in re.findall(r"[А-ЯЁ]{3,}", s):
                if not form_re.search(tok):
                    company[tok] += 1
        for s in speech:
            if form_re.search(s):
                samples[freq or "?"].append((d, grp or "?", s))

    say("## 4. Де воно звучить")
    say()
    if not per_net:
        say(f"за {window} — жодного влучання. Покриття нижче показує, чи було кого чути взагалі.")
    for freq, rec in sorted(per_net.items(), key=lambda kv: -(kv[1]["st"] + kv[1]["sp"])):
        grp = ", ".join(f"{g}:{c}" for g, c in rec["groups"].most_common())
        say(f"- **{freq}** — у шапці (кореспондент): {rec['st']} · у мові (про нього говорять): "
            f"{rec['sp']} · з {cover[freq]} перехоплень мережі · {grp}")
        if rec["net"]:
            say(f"  - {rec['net'][:120]}")
    say()

    say("## 5. Коли")
    say()
    if first_seen:
        say(f"перше: {first_seen[0]:%d.%m.%Y %H:%M} ({first_seen[1]}) · "
            f"останнє: {last_seen[0]:%d.%m.%Y %H:%M} ({last_seen[1]}) · усього перехоплень: {total}")
        say("по днях: " + ", ".join(f"{d}:{c}" for d, c in sorted(by_day.items())))
    else:
        say("—")
    say()

    say("## 6. З ким ходить (позивні з тих самих перехоплень)")
    say()
    say(", ".join(f"{k} ({v})" for k, v in company.most_common(15)) or "—")
    say()

    say("## 7. Сирі рядки, свіжі зверху")
    say()
    for freq, rec in sorted(per_net.items(), key=lambda kv: -(kv[1]["st"] + kv[1]["sp"])):
        say(f"### {freq} — {rec['net'][:100]}")
        say()
        seen = set()
        shown = 0
        for d, grp, s in sorted(samples[freq], key=lambda x: -x[0].timestamp()):
            key = s[:60]
            if key in seen:
                continue
            seen.add(key)
            say(f"- `{d:%d.%m %H:%M}` [{grp[:10]}] {s[:230]}")
            shown += 1
            if shown >= a.lines:
                break
        if not shown:
            say("- (тільки в шапці, у мові не звучить)")
        say()

    say("## 8. Чим шукали — рядок, на який спирається будь-яке твердження")
    say()
    say(f"шукав форми: {', '.join(sorted(forms))} · вікно: {window}"
        + (f" · мережа: {a.net}" if a.net else "")
        + f" · знайдено {total} перехоплень, мереж {len(per_net)}")
    say("ТВЕРДЖЕННЯ ПРО ВІДСУТНІСТЬ допускається лише після `--all` і прочитаних рядків: "
        "пошук доводить наявність, відсутність — ніколи.")
    say()
    say("## 9. Читання (етап 2 — робить людина)")
    say()
    say("Висновок пишеться ОКРЕМО ПО КОЖНІЙ МЕРЕЖІ, з двома цитатами на кожне твердження, "
        "у категоріях людина / предмет / місце / код, з рівнем «підтверджено · ймовірно · "
        "не встановлено». Головний різнитель: чи можна його викликати по рації (відгукується — "
        "кореспондент; про нього лише говорять у третій особі — предмет).")

    if not a.no_save:
        PROBES_DIR.mkdir(parents=True, exist_ok=True)
        dest = PROBES_DIR / f"PROBE_{a.word}_{now:%Y-%m-%d}.md"
        dest.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"\nзбережено: {dest.relative_to(REPO)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
