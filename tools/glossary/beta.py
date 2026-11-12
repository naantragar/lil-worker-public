#!/usr/bin/env python3
"""The BETA table: words the model thinks are coded, before a human has said whether they are.

    python3 tools/glossary/beta.py list [--status new]
    python3 tools/glossary/beta.py survey            # build the button set for the phone
    python3 tools/glossary/beta.py apply             # fold answered ones into the glossary
    python3 tools/glossary/beta.py seed              # the owner's first corrections

Why this exists, in the owner's words (28.09.2026): «кандидаты тоже - растить таблицы с этого не
обязательно». The extraction pass offers 15 candidate code words a day and most are wrong, because
a model reading radio traffic cannot tell a convention from an ordinary noun:

    «посылочка»      usually an actual parcel
    «повербанки»     usually actual power banks
    «контроль неба»  nothing coded at all
    «малых»          NOT small drones - «две малые» is TWO MINUTES, «две большие» is up to two hours

That last one is the whole argument. The model was confident and wrong in a way no calibration can
catch, because the word never appears in our signed tables at all. Only a person who has listened
knows it.

So a candidate lands HERE and nowhere else. It reaches the code tables only when the owner says so.

Four verdicts, split by what the word DOES for us - not by whether he wants to type something
(the explanation attaches to any verdict, the survey bot already asks for it as text or voice):

    код      - a real convention of a unit. Goes into `entries`, joins the sieve, gets calibrated
               against the signed corpus like every other code.
    жаргон   - it means something, but it is not a unit's code: shared slang of the whole air
               («малые» = minutes). Goes into knowledge ONLY. Never becomes sieve material, because
               it discriminates nothing - everyone says it.
    сміття   - an ordinary word the model over-read. Goes on the NEGATIVE list.
    не знаю  - stays in beta, comes back next time with more examples.

The negative list is the part that pays off daily: `blacklist()` is handed to the extraction prompt,
so a word he has already dismissed stops being offered every single day.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "knowledge" / "upstream" / "glossary" / "glossary.db"
SURVEYS = REPO / "knowledge" / "upstream" / "surveys"

SCHEMA = """
CREATE TABLE IF NOT EXISTS beta_terms (
    word       TEXT PRIMARY KEY,
    first_seen TEXT, last_seen TEXT,
    hits       INTEGER DEFAULT 0,
    guesses    TEXT,           -- what the model proposed, JSON list
    examples   TEXT,           -- where it was heard, JSON list
    status     TEXT DEFAULT 'new',     -- new | код | жаргон | сміття | не знаю
    meaning    TEXT,           -- the owner's own wording
    unit       TEXT,
    decided    TEXT
);
-- One word, many nets (owner, 04.11.2026): «Юпитер» is a man on one net and may be a point on
-- another; «РУБИН» is a 186 мсп commander in 993 of 1182 intercepts and «по рубину» on 455.3106.
-- A verdict is therefore stored PER FREQUENCY, and the per-net profile says where the word is a
-- callsign, which outranks any code verdict on that net.
CREATE TABLE IF NOT EXISTS beta_scoped (
    word TEXT, freq TEXT, status TEXT, meaning TEXT, decided TEXT,
    PRIMARY KEY (word, freq)
);
CREATE TABLE IF NOT EXISTS beta_profile (
    word TEXT, freq TEXT, n INTEGER, cs INTEGER, form TEXT, computed TEXT,
    PRIMARY KEY (word, freq)
);
"""
CORPUS = "file:~/wa-monitor/messages.db?mode=ro"
GROUPS = ("Invisible Hand", "AllInARow", "PATAGONIA_GP")
# A net where the word stands as a station / in a call at least this often is a net where it is a
# MAN. Half of its mentions: «Юпитер» 59 of 77 in the station header, РУБИН 993 of 1182.
PERSON_SHARE, PERSON_MIN = 0.5, 1   # «Бек, я Юпитер» once in two mentions is already a man there
MAX_ID_BYTES = 0        # set below OPTIONS: callback_data is ≤64 bytes = "a:beta_kody:" + id + ":" + key
# The buttons name the KIND of thing the word is, because that is what decides where it goes and
# what it is worth. A code that names a PLACE is evidence of where a unit operates; a code that
# names an ACTION is evidence of what it does; slang is neither and must never enter the sieve.
# Every code-ish verdict asks for a comment, so the owner's own wording is what gets stored —
# the model's guess is only there to save him typing when it happens to be right.
OPTIONS = [
    {"key": "точка", "label": "точка / орієнтир / місцевість", "comment": True},
    {"key": "дія", "label": "код дії або сигналу", "comment": True},
    {"key": "техніка", "label": "техніка / вантаж / БпЛА", "comment": True},
    {"key": "підрозділ", "label": "назва підрозділу", "comment": True},
    # A callsign taken for a code is a class of its own, and it was missing until the owner hit
    # «Юпитер» in the markup («А Юпитер с ним едет или здесь остается?» - plainly a man). It must
    # never enter the code tables: a person's name calibrated as a code would make every net he
    # speaks on look like evidence of a formation.
    {"key": "позивний", "label": "це ПОЗИВНИЙ (людина)", "comment": True},
    {"key": "жаргон", "label": "жаргон, спільний для всіх", "comment": True},
    {"key": "сміття", "label": "звичайне слово"},
    {"key": "не знаю", "label": "не знаю, лишити в беті"},
]
# «підрозділ» is 18 bytes, so the id budget is 64 - 12 - 1 - 18 = 33, not a round guess.
MAX_ID_BYTES = 64 - len("a:beta_kody::".encode()) - max(len(o["key"].encode()) for o in OPTIONS)
CODE_KINDS = {"точка": "settlement_code", "дія": "signal_code",
              "техніка": "code_word", "підрозділ": "code_word"}


def con() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB))
    c.executescript(SCHEMA)
    c.row_factory = sqlite3.Row
    return c


def add(word: str, guess: str, example: dict) -> None:
    """Record one sighting. Re-seeing a word adds evidence, never resets a verdict."""
    w = " ".join(str(word or "").lower().split())
    if not w or len(w) > 40:
        return
    c = con()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    r = c.execute("SELECT guesses, examples, hits FROM beta_terms WHERE word=?", (w,)).fetchone()
    if r:
        g = json.loads(r["guesses"] or "[]")
        e = json.loads(r["examples"] or "[]")
        if guess and guess not in g:
            g.append(guess)
        if example and len(e) < 6:
            e.append(example)
        c.execute("UPDATE beta_terms SET last_seen=?, hits=hits+1, guesses=?, examples=? WHERE word=?",
                  (now, json.dumps(g, ensure_ascii=False), json.dumps(e, ensure_ascii=False), w))
    else:
        c.execute("INSERT INTO beta_terms (word, first_seen, last_seen, hits, guesses, examples) "
                  "VALUES (?,?,?,1,?,?)",
                  (w, now, now, json.dumps([guess] if guess else [], ensure_ascii=False),
                   json.dumps([example] if example else [], ensure_ascii=False)))
    c.commit()
    c.close()


def blacklist() -> set[str]:
    """Words the owner has already dismissed. Handed to the extraction prompt so the same noise
    does not come back every day — the single most useful thing this table does."""
    if not DB.exists():
        return set()
    c = con()
    out = {r[0] for r in c.execute("SELECT word FROM beta_terms WHERE status IN ('сміття','жаргон')")}
    # «позивний» silences the word for the extraction pass only when it is a man on (nearly) every
    # net it is heard on; a word that is a man here and a point there must be allowed to come back.
    for (w,) in c.execute("SELECT word FROM beta_terms WHERE status='позивний'"):
        prof = cached_profile(c, w)
        tot = sum(e["n"] for e in prof.values())
        if not prof or sum(e["n"] for e in prof.values() if is_person(e)) >= 0.8 * tot:
            out.add(w)
    c.close()
    return out


def known() -> set[str]:
    c = con()
    out = {r[0] for r in c.execute("SELECT word FROM beta_terms")}
    c.close()
    return out


def seed() -> None:
    """The owner's corrections of 28.09.2026, entered as the first decided rows. They are the
    calibration of this whole table: three of the four were the model being confidently wrong."""
    rows = [
        ("малых", "жаргон", "«дві малі» = дві ХВИЛИНИ, «дві великі» = до двох годин. Не дрони.", ""),
        ("малые", "жаргон", "«дві малі» = дві хвилини; «великі» = години. Міра ЧАСУ.", ""),
        ("посылочка", "сміття", "найчастіше просто посилка", ""),
        ("повербанки", "сміття", "найчастіше справді повербанки", ""),
        ("повербанк", "сміття", "найчастіше справді повербанк", ""),
        ("контроль неба", "сміття", "нічого кодового", ""),
        ("картинка", "не знаю", "цікаве: найчастіше «картинкою» говорять про розвідувальний "
                                "БпЛА / його відео. Тримати в беті, збирати приклади.", ""),
    ]
    c = con()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for w, st, mean, unit in rows:
        c.execute("INSERT INTO beta_terms (word, first_seen, last_seen, hits, guesses, examples, "
                  "status, meaning, unit, decided) VALUES (?,?,?,0,'[]','[]',?,?,?,?) "
                  "ON CONFLICT(word) DO UPDATE SET status=excluded.status, meaning=excluded.meaning, "
                  "unit=excluded.unit, decided=excluded.decided",
                  (w, now, now, st, mean, unit, now))
    c.commit()
    c.close()
    print(f"засіяно {len(rows)} рішень власника")


PRESORT_SYSTEM = """Ти прибираєш ОЧЕВИДНИЙ шум зі списку слів-кандидатів у кодові, ПЕРЕД тим як
список побачить людина. Твоя єдина дія - відкинути слово або лишити його людині.

ТИ НЕ МОЖЕШ ОГОЛОСИТИ СЛОВО КОДОМ. Такої відповіді в тебе немає. Ціна помилок різна: дарма
відкинуте слово коштує пропущеної підказки, і воно повернеться завтра, бо ефір повторюється;
а дарма впущений код сідає в таблицю, отримує вагу і починає брехати мовчки.

Відкидай (dismiss=true) лише тоді, коли побутове прочитання ПОВНІСТЮ пояснює вживання:
  «вынос наебнулся», «одноразка» про дрон, «вспышку не проеби», число у розмові про звʼязок.
Лишай людині (dismiss=false) усе, де слово поводиться не як звичайне:
  на нього ЙДУТЬ, від нього МІРЯЮТЬ напрямок, через нього ПРОХОДЯТЬ, ним НАЗИВАЮТЬ те, чим воно
  бути не може. Сумніваєшся - лишай. Сумнів завжди на користь людини.

Відповідай ЛИШЕ JSON: {"dismiss": true|false, "why": "<коротко, до 120 символів>"}"""

# Words the owner has asked to keep in front of him no matter what the presort thinks. «ф» is the
# reason this list exists: it looks exactly like a stray letter, and it is a lettered map point -
# «мимо Ф и озером», «от Ф, 5 тяни до Ф 1». An automatic pass would have thrown it away.
PROTECTED = {"ф"}


def presort(model: str = "claude-sonnet-5", effort: str = "medium", limit: int = 60) -> None:
    """Take the obvious noise off the list before it reaches the owner's phone.

    This pass can ONLY dismiss. There is no branch in it that writes a code, by construction and
    not by instruction - `apply_answers` is the single door into the tables, and it opens on a
    button press. Measured on the first 19 candidates: it should clear about eight.
    """
    import json as _json
    import subprocess
    cwd = Path("/tmp/krevetka-beta-presort")
    cwd.mkdir(parents=True, exist_ok=True)
    c = con()
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM beta_terms WHERE status='new' ORDER BY hits DESC LIMIT ?", (limit,))]
    dropped = kept = 0
    for r in rows:
        w = r["word"]
        if w in PROTECTED:
            kept += 1
            continue
        ex = _json.loads(r["examples"] or "[]")
        material = (f"СЛОВО: «{w}»\nу всьому корпусі перехоплень: {r['hits']}\n\nМОВА:\n"
                    + "\n".join("  " + (x.get("quote") or "")[:220] for x in ex[:3]))
        try:
            p = subprocess.run(
                ["claude", "-p", "--model", model, "--system-prompt", PRESORT_SYSTEM,
                 "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                 "--disallowedTools", "Bash", "Read", "Write", "Edit", "Grep", "Glob",
                 "--effort", effort],
                input=material, capture_output=True, text=True, timeout=180, cwd=str(cwd))
            m = re.search(r"\{.*\}", p.stdout, re.S)
            v = _json.loads(m.group(0)) if m else {}
        except Exception as e:                                   # noqa: BLE001
            print(f"  «{w}»: збій перевірки ({type(e).__name__}) - лишаю людині")
            kept += 1
            continue
        if v.get("dismiss") is True:
            c.execute("UPDATE beta_terms SET status='сміття', meaning=?, decided=? WHERE word=?",
                      (f"знято автоматично: {v.get('why','')}"[:200],
                       datetime.now().strftime("%Y-%m-%d %H:%M"), w))
            dropped += 1
            print(f"  ЗНЯТО   «{w}» ×{r['hits']} — {v.get('why','')[:90]}")
        else:
            kept += 1
            print(f"  лишаю   «{w}» ×{r['hits']} — {v.get('why','')[:90]}")
    c.commit()
    c.close()
    print(f"\nзнято автоматично: {dropped}, лишилось людині: {kept}")


def _parse(txt: str):
    sys.path.insert(0, str(REPO / "tools" / "identity"))
    from posady import parse                                   # one intercept parser for the project
    return parse(txt)


def _formation(header: str) -> str:
    sys.path.insert(0, str(REPO / "tools" / "identity"))
    from roles_db import formation
    return formation(header)


def profile(word: str, c: sqlite3.Connection | None = None) -> dict[str, dict]:
    """Per frequency: how often the word is SAID there, how often as a callsign (station header or a
    call «X, я…» / «я X»), and which formation the net's headers name. Cached in `beta_profile`."""
    w = word.lower()
    # the same stem rule as code_sieve: «крыло» → «крыл», not «кры» (which also matched КРЫМ, крыша)
    stem_ = w[:-3] if len(w) > 7 else w[:-2] if len(w) > 5 else w[:-1] if len(w) > 3 else w
    tail = r"\w{0,3}" if len(w) > 3 else ""            # a 1-3 letter word is matched exactly
    word_re = re.compile(rf"\b{re.escape(stem_)}{tail}\b", re.I)
    # The callsign test uses the EXACT word, never the stem: a station line and a call are in the
    # nominative, and the stem of «крыло» is also КРЫЛ, a 38 омсбр commander (1046 «hits» on 421.55).
    call_re = re.compile(rf"(?:\b{re.escape(w)}\s*,?\s*(?:я|прием|приём|на связи)\b)"
                         rf"|(?:\bя\s+{re.escape(w)}\b)", re.I)
    db = sqlite3.connect(CORPUS, uri=True)
    pats = {f"%{v}%" for v in (stem_, stem_.capitalize(), stem_.upper())}
    q = (f"SELECT text FROM messages WHERE group_name IN ({','.join('?' * len(GROUPS))}) AND ("
         + " OR ".join("text LIKE ?" for _ in pats) + ")")
    out: dict[str, dict] = {}
    seen: set[str] = set()
    forms: dict[str, dict[str, int]] = {}
    for (txt,) in db.execute(q, (*GROUPS, *pats)):
        r = _parse(txt or "")
        if not r:
            continue
        sp = " ".join(r["speech"])
        if not word_re.search(sp) and not any(word_re.search(x) for x in r["stations"]):
            continue
        body = re.sub(r"\W", "", sp)[:200]
        if body in seen:
            continue                                  # one transmission through two groups
        seen.add(body)
        f = nf(r["ref"].split()[-1])
        e = out.setdefault(f, {"n": 0, "cs": 0, "form": ""})
        e["n"] += 1
        names = {n.strip().upper() for x in r["stations"] for n in re.split(r"[,/]", x)}
        if w.upper() in names or call_re.search(sp):
            e["cs"] += 1
        fm = _formation(r["header"])
        if fm:
            forms.setdefault(f, {})[fm] = forms.setdefault(f, {}).get(fm, 0) + 1
    for f, e in out.items():
        if forms.get(f):
            e["form"] = max(forms[f], key=forms[f].get)
    if c is not None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        c.execute("DELETE FROM beta_profile WHERE word=?", (word,))
        c.executemany("INSERT INTO beta_profile VALUES (?,?,?,?,?,?)",
                      [(word, f, e["n"], e["cs"], e["form"], now) for f, e in out.items()])
        c.commit()
    return out


def nf(f) -> str:
    """One spelling for a frequency: the examples carry «150.5», the headers «150.5000»."""
    try:
        return f"{float(str(f).replace(',', '.')):.4f}"
    except (TypeError, ValueError):
        return str(f or "")


def is_person(e: dict | None) -> bool:
    return bool(e) and e["cs"] >= PERSON_MIN and e["cs"] >= PERSON_SHARE * e["n"]


def cached_profile(c: sqlite3.Connection, word: str) -> dict[str, dict]:
    return {r["freq"]: {"n": r["n"], "cs": r["cs"], "form": r["form"]}
            for r in c.execute("SELECT * FROM beta_profile WHERE word=?", (word,))}


def _profile_line(prof: dict[str, dict]) -> str:
    """«позивний на: 154.2500 (9 з 10) · інше на: 455.3106 (3)» — the split the owner decides on."""
    pers = sorted(((f, e) for f, e in prof.items() if is_person(e)), key=lambda x: -x[1]["n"])
    other = sorted(((f, e) for f, e in prof.items() if not is_person(e)), key=lambda x: -x[1]["n"])
    parts = []
    if pers:
        parts.append("позивний на: " + ", ".join(f"{f} ({e['cs']} з {e['n']})" for f, e in pers[:4])
                     + (f" +{len(pers) - 4}" if len(pers) > 4 else ""))
    if other:
        parts.append("інше вживання на: " + ", ".join(f"{f} ({e['n']})" for f, e in other[:4])
                     + (f" +{len(other) - 4}" if len(other) > 4 else ""))
    return " · ".join(parts)


def _item_id(word: str, freq: str = "") -> str:
    iid = f"b_{word}" + (f"@{freq}" if freq else "")
    if len(iid.encode()) <= MAX_ID_BYTES:
        return iid
    import hashlib
    return "b#" + hashlib.sha1(iid.encode()).hexdigest()[:10]


def _answered_ids() -> set[str]:
    p = SURVEYS / "answers" / "beta_kody.json"
    try:
        return {str(a.get("id")) for a in json.loads(p.read_text(encoding="utf-8"))}
    except Exception:                                          # noqa: BLE001
        return set()


def _example_from_corpus(word: str, freq: str, k: int = 2) -> list[dict]:
    w = word.lower()
    stem_ = w[:-3] if len(w) > 7 else w[:-2] if len(w) > 5 else w[:-1] if len(w) > 3 else w
    rx = re.compile(rf"\b{re.escape(stem_)}" + (r"\w{0,3}" if len(w) > 3 else "") + r"\b", re.I)
    db = sqlite3.connect(CORPUS, uri=True)
    out = []
    for (txt,) in db.execute("SELECT text FROM messages WHERE text LIKE ? ORDER BY timestamp DESC",
                             (f"%{freq.rstrip('0').rstrip('.')}%",)):
        r = _parse(txt or "")
        if not r or nf(r["ref"].split()[-1]) != freq:
            continue
        line = next((l for l in r["speech"] if rx.search(l)), "")
        if line:
            out.append({"when": r["ref"].rsplit(" ", 1)[0][:11], "freq": freq, "quote": line})
            if len(out) >= k:
                break
    return out


def make_survey(min_hits: int = 1) -> Path:
    """One card per undecided word: how often it is really said, what the model guessed, and the
    speech it was heard in. The corpus count is the evidence the model never has — «крестик» 926
    times and always a place is a different thing from a word said once."""
    c = con()
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM beta_terms WHERE status IN ('new','не знаю','по сітках') AND hits >= ? "
        "ORDER BY hits DESC, word", (min_hits,))]
    answered = _answered_ids()
    done_scoped = {(r[0], r[1]) for r in c.execute("SELECT word, freq FROM beta_scoped")}
    items = []
    for r in rows:
        w = r["word"]
        g = json.loads(r["guesses"] or "[]")
        e = json.loads(r["examples"] or "[]")
        prof = profile(w, c)
        pers = {f for f, x in prof.items() if is_person(x)}
        other = sorted((f for f, x in prof.items() if not is_person(x) and x["n"] >= 2),
                       key=lambda f: -prof[f]["n"])
        # A word that is a man on some nets and something else on others is asked PER NET — unless
        # he already answered it as a whole, in which case his answer is scoped at apply time.
        # «Mixed» needs a real callsign behind it, not one stray call: КАРАНДАШ once as «я Карандаш»
        # on one net must not split a word heard 300 times as a counted thing. And 1-3 letter words
        # («ф», «д») are lettered points far more than names - they stay one card.
        cs_tot = sum(prof[f]["cs"] for f in pers)
        n_tot = sum(x["n"] for x in prof.values()) or 1
        mixed = len(w) >= 4 and cs_tot >= 3 and cs_tot >= 0.1 * n_tot
        scoped = bool(mixed and other) and _item_id(w) not in answered
        targets = [f for f in other[:2] if (w, f) not in done_scoped] if scoped else [""]
        if r["status"] == "по сітках" and targets == [""]:
            continue                       # answered per net already, nothing new to ask
        for f in targets:
            text = [f"«{w}»" + (f"   на {f}" + (f" ({prof[f]['form']})" if prof[f]["form"] else "") if f else ""),
                    f"у всьому корпусі: {r['hits']} перехоплень"]
            pl = _profile_line(prof)
            if pl:
                text.append(pl)
            if f:
                text.append("на інших мережах це людина - питаю лише про цю частоту")
            if g:
                text.append("модель каже: " + "; ".join(str(x)[:110] for x in g[:2]))
            ex = [x for x in e if not f or nf(x.get("freq")) == f]
            if f and not ex:
                ex = _example_from_corpus(w, f)
            if ex:
                text.append("")
                for x in ex[:3]:
                    q = (x.get("quote") or "").strip()
                    text.append(f"{x.get('when','')} · {x.get('freq','')}")
                    if q:
                        text.append(f"   {q[:220]}")
            text.append("\nЩо це насправді" + (" на цій частоті?" if f else "?"))
            items.append({"id": _item_id(w, f), "word": w, "freq": f, "text": "\n".join(text)})
    c.close()
    SURVEYS.mkdir(parents=True, exist_ok=True)
    p = SURVEYS / "beta_kody.json"
    # Answered cards stay in the set: `apply` resolves a hashed id only through its card, and the
    # bot counts progress as answers against items — dropping them would break both.
    have = {i["id"] for i in items}
    try:
        old = json.loads(p.read_text(encoding="utf-8")).get("items", [])
    except Exception:                                          # noqa: BLE001
        old = []
    keep = [i for i in old if str(i.get("id")) in answered and i.get("id") not in have]
    for i in keep:
        if "word" not in i and str(i["id"]).startswith("b_"):
            i["word"], i["freq"] = str(i["id"])[2:].split("@")[0], (str(i["id"]).split("@", 1) + [""])[1]
    items = keep + items
    p.write_text(json.dumps({"title": f"Бета: кодові слова ({len(items)})",
                             "options": OPTIONS, "items": items}, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    print(f"{p} — питань: {len(items)}")
    return p


def apply_answers(dry: bool = False) -> None:
    """Fold the bot's answers back. Every verdict lands PER FREQUENCY (`beta_scoped`):
    a card asked about one net applies to that net; an older whole-word card applies to the nets of
    the examples he was shown. A code verdict becomes a glossary entry — the ONLY door from beta into
    the sieve — but never on a net where the word is a man: there the callsign wins and the skip is
    printed. Re-running adds nothing twice."""
    src = SURVEYS / "answers" / "beta_kody.json"
    if not src.exists():
        sys.exit(f"немає відповідей: {src}")
    ans = json.loads(src.read_text(encoding="utf-8"))
    items = {}
    try:
        items = {str(i["id"]): i for i in json.loads((SURVEYS / "beta_kody.json").read_text())["items"]}
    except Exception:                                          # noqa: BLE001
        pass
    c = con()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    n = codes = guarded = 0
    for a in ans if isinstance(ans, list) else ans.get("answers", []):
        iid = str(a.get("id") or a.get("item_id") or "")
        st = a.get("verdict")
        it = items.get(iid, {})
        w = it.get("word") or (iid[2:].split("@")[0] if iid.startswith("b_") else "")
        if not w or not st:
            continue
        f = it.get("freq") or (iid.split("@", 1)[1] if "@" in iid else "")
        row = c.execute("SELECT examples FROM beta_terms WHERE word=?", (w,)).fetchone()
        freqs = [nf(f)] if f else sorted({nf(x.get("freq")) for x in json.loads((row or {"examples": "[]"})["examples"] or "[]")
                                      if x.get("freq")}) or [""]
        prof = cached_profile(c, w) or profile(w, c)
        n += 1
        line = []
        for fq in freqs:
            if not dry:
                c.execute("INSERT INTO beta_scoped VALUES (?,?,?,?,?) ON CONFLICT(word,freq) DO UPDATE SET "
                          "status=excluded.status, meaning=excluded.meaning, decided=excluded.decided",
                          (w, fq, st, a.get("comment"), now))
            if st not in CODE_KINDS:
                line.append(f"{fq or 'усі'}: {st}")
                continue
            if is_person(prof.get(fq)):
                guarded += 1
                line.append(f"{fq}: НЕ ставлю код - тут це позивний ({prof[fq]['cs']} з {prof[fq]['n']})")
                continue
            # No formation on an unsigned net: the code is pinned to the FREQUENCY. code_sieve
            # ignores a code with no formation as evidence, so it is inert there, not global.
            # A formation is given ONLY when the word's non-callsign use is concentrated in it (≥80%):
            # code_sieve reads a formation-scoped code as evidence FOR that formation, and «крыло»
            # (heard on ~180 nets) or «ф» (lettered points of three formations) would sign nets wrongly.
            form = (prof.get(fq) or {}).get("form", "")
            plain = [x for x in prof.values() if not is_person(x)]
            share = (sum(x["n"] for x in plain if x["form"] == form) / (sum(x["n"] for x in plain) or 1)
                     if form else 0)
            unit = form if form and share >= 0.8 else (f"част {fq}" if fq else "")
            note = f"з бета-таблиці ({st}), частота {fq or '-'}, підтвердив власник {now}"
            dup = c.execute("SELECT 1 FROM entries WHERE term=? AND kind=? AND IFNULL(unit,'')=? AND source='beta_terms'",
                            (w, CODE_KINDS[st], unit)).fetchone()
            if not dup and not dry:
                c.execute("INSERT INTO entries (term, kind, meaning, unit, note, source) VALUES (?,?,?,?,?,?)",
                          (w, CODE_KINDS[st], a.get("comment") or "", unit, note, "beta_terms"))
            codes += 0 if dup else 1
            line.append(f"{fq}: код «{st}» → {unit or 'без частини'}" + (" (уже є)" if dup else ""))
        if not dry:
            # A whole-word answer settles the word; a per-net one leaves its other nets to be asked.
            c.execute("UPDATE beta_terms SET status=?, meaning=coalesce(?,meaning), decided=? WHERE word=?",
                      ("по сітках" if f else st, a.get("comment"), now, w))
        print(f"  «{w}» — " + "; ".join(line))
    if not dry:
        c.commit()
    c.close()
    print(f"{'(перевірка, нічого не записано) ' if dry else ''}рішень: {n}, у таблицю кодів: {codes}, "
          f"не поставлено через позивний: {guarded}")


def callsigns() -> None:
    """Words the owner marked as PEOPLE. They leave the code pipeline entirely; this list is what
    gets checked against the callsign register - a different base, a different question."""
    c = con()
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM beta_terms WHERE status='позивний' ORDER BY hits DESC")]
    c.close()
    print(f"позивних, знятих із кодових кандидатів: {len(rows)}")
    for r in rows:
        ex = json.loads(r["examples"] or "[]")
        where = ", ".join(sorted({str(x.get("freq")) for x in ex if x.get("freq")}))
        print(f"  {r['word'].upper():<16} у корпусі {r['hits']:5}"
              + (f"  чути на: {where}" if where else "")
              + (f"  — {r['meaning'][:60]}" if r["meaning"] else ""))


def show(status: str | None) -> None:
    c = con()
    q = "SELECT * FROM beta_terms" + (" WHERE status=?" if status else "") + " ORDER BY hits DESC"
    rows = [dict(r) for r in (c.execute(q, (status,)) if status else c.execute(q))]
    c.close()
    import collections
    print("у беті:", len(rows), dict(collections.Counter(r["status"] for r in rows)))
    for r in rows[:60]:
        print(f"  [{r['status']:<8}] «{r['word']}» ×{r['hits']}"
              + (f" — {r['meaning'][:70]}" if r["meaning"] else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "survey", "apply", "seed", "presort", "callsigns"])
    ap.add_argument("--status")
    ap.add_argument("--dry", action="store_true", help="apply: показати, куди ляже, нічого не писати")
    ap.add_argument("--min-hits", type=int, default=1,
                    help="не питати про слова, що звучали рідше N разів у корпусі")
    a = ap.parse_args()
    {"list": lambda: show(a.status), "survey": lambda: make_survey(a.min_hits),
     "apply": lambda: apply_answers(a.dry), "seed": seed, "presort": presort,
     "callsigns": callsigns}[a.cmd]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
