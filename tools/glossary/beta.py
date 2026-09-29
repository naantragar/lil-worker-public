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
"""
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
    out = {r[0] for r in c.execute(
        "SELECT word FROM beta_terms WHERE status IN ('сміття','жаргон','позивний')")}
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


def make_survey(min_hits: int = 1) -> Path:
    """One card per undecided word: how often it is really said, what the model guessed, and the
    speech it was heard in. The corpus count is the evidence the model never has — «крестик» 926
    times and always a place is a different thing from a word said once."""
    c = con()
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM beta_terms WHERE status IN ('new','не знаю') AND hits >= ? "
        "ORDER BY hits DESC, word", (min_hits,))]
    c.close()
    items = []
    for r in rows:
        g = json.loads(r["guesses"] or "[]")
        e = json.loads(r["examples"] or "[]")
        text = [f"«{r['word']}»",
                f"у всьому корпусі: {r['hits']} перехоплень"]
        if g:
            text.append("модель каже: " + "; ".join(str(x)[:110] for x in g[:2]))
        if e:
            text.append("")
            for x in e[:3]:
                q = (x.get("quote") or "").strip()
                text.append(f"{x.get('when','')} · {x.get('freq','')}")
                if q:
                    text.append(f"   {q[:220]}")
        text.append("\nЩо це насправді?")
        items.append({"id": f"b_{r['word']}", "text": "\n".join(text)})
    SURVEYS.mkdir(parents=True, exist_ok=True)
    p = SURVEYS / "beta_kody.json"
    p.write_text(json.dumps({"title": f"Бета: кодові слова ({len(items)})",
                             "options": OPTIONS, "items": items}, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    print(f"{p} — питань: {len(items)}")
    return p


def apply_answers() -> None:
    """Fold the bot's answers back into the table. `код` additionally becomes a real glossary entry,
    which is the ONLY door from beta into the sieve."""
    src = SURVEYS / "answers" / "beta_kody.json"
    if not src.exists():
        sys.exit(f"немає відповідей: {src}")
    ans = json.loads(src.read_text(encoding="utf-8"))
    c = con()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    n = codes = 0
    for a in ans if isinstance(ans, list) else ans.get("answers", []):
        # the bot writes `id`, not `item_id` — checked against a real answers file before trusting it
        w = str(a.get("id") or a.get("item_id") or "")[2:]
        st = a.get("verdict")
        if not w or not st:
            continue
        c.execute("UPDATE beta_terms SET status=?, meaning=coalesce(?,meaning), decided=? WHERE word=?",
                  (st, a.get("comment"), now, w))
        n += 1
        if st in CODE_KINDS:
            # The ONLY door from beta into the tables the sieve reads. It opens on a button press
            # and on nothing else.
            c.execute("INSERT INTO entries (term, kind, meaning, unit, note, source) "
                      "VALUES (?,?,?,?,?,?)",
                      (w, CODE_KINDS[st], a.get("comment") or "", a.get("unit") or "",
                       f"з бета-таблиці ({st}), підтвердив власник {now}", "beta_terms"))
            codes += 1
    c.commit()
    c.close()
    print(f"записано рішень: {n}, з них у таблицю кодів: {codes}")


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
    ap.add_argument("--min-hits", type=int, default=1,
                    help="не питати про слова, що звучали рідше N разів у корпусі")
    a = ap.parse_args()
    {"list": lambda: show(a.status), "survey": lambda: make_survey(a.min_hits),
     "apply": apply_answers, "seed": seed, "presort": presort,
     "callsigns": callsigns}[a.cmd]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
