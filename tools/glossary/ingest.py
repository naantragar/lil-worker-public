#!/usr/bin/env python3
"""Read the transfer group's documents into the glossary database, unattended.

    python3 tools/glossary/ingest.py scan            # register what the collector downloaded
    python3 tools/glossary/ingest.py plan            # what would be read, how deep, and why
    python3 tools/glossary/ingest.py run [--limit N] # do it
    python3 tools/glossary/ingest.py report          # level-5 run report

The procedure, the depth rules and the entry vocabulary are specified in
`knowledge/upstream/glossary/INGEST_TZ.md` — this file is that spec made executable, and the spec is
the thing to read first. What lives HERE is only the mechanism: conversion, chunking, the model
call, and the write.

Design note worth keeping: the model never sees a whole file and never sees a table. It sees a
chunk of prose with an explicit extraction contract, and everything mechanical around it —
declaring which files exist, converting them, deciding depth, deduplicating, writing FTS — is code.
That is the same division that makes the daily report affordable.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GLOSS = REPO / "knowledge" / "upstream" / "glossary"
RAW = GLOSS / "raw"
CACHE = GLOSS / "text"                      # extracted plain text, so a re-run costs nothing
DB = GLOSS / "glossary.db"

ENTRY_KINDS = ["person", "place", "settlement_code", "route_code", "code_word", "signal_code",
               "numeric_code", "radio_term", "radio_mode", "doctrine_term", "practice",
               "uav_type", "net_fix", "note"]

# Level 0: source classification. The filename settles it in almost every case — these documents
# are named by the people who made them, and the naming is remarkably consistent.
KIND_RULES = [
    (r"кодуван|кодиров|код[аи]\b", "code_table"),
    (r"штатка|^штат\b|штат\.xlsx", "roster"),
    (r"статут|памятка|инструкц|наставлен", "manual"),
    (r"силуэт|силует|розпізнав", "recognition_sheet"),
    (r"^плг|пеленг", "net_log"),
    (r"позивн|еталонк", "register"),
]
# A debrief is recognised by its SHAPE, not by a word: surname, unit, date. Everything that arrives
# as a document with a person's name on it in this group has so far been one.
# NB: `\b` is useless here — the collector joins name parts with UNDERSCORES, and `_` is a word
# character, so «38_омсбр__14.05.26» has no boundary after the unit. Eight debriefs classified
# themselves as «other» on the first scan because of exactly that.
DEBRIEF_RE = re.compile(r"[А-ЯІЇЄҐ][а-яіїєґ']+.*?(мсп|омсбр|отбр|мсбр|мср|мсб|абр)(?![а-яіїєґ])", re.I)

CHUNK = 11000                               # characters of prose per model call
OVERLAP = 600

SYS = """Ти читаєш ТРОФЕЙНИЙ документ противника (або наш робочий документ по ньому) і витягуєш з
нього факти для бази знань радіорозвідки. Мова документа - російська або українська.

База відповідає рівно на три питання: ХТО ЦЕ (позивний/прізвище -> посада), ЩО ОЗНАЧАЄ ЦЕ СЛОВО
(їхні коди і терміни), ДЕ ЦЕ ВЖЕ ЗУСТРІЧАЛОСЬ. Усе, що не відповідає на жодне з них, у базу не йде.

Поверни JSON-масив об'єктів. Кожен об'єкт:
  kind    - одне з: person | place | settlement_code | route_code | code_word | signal_code |
            numeric_code | radio_term | radio_mode | doctrine_term | practice | uav_type | net_fix | note
  term    - сам термін: позивний, прізвище, кодове слово, назва об'єкта, частота
  meaning - що це: звання і посада для людини; розшифровка для коду; координати для об'єкта
  unit    - найконкретніше назване формування ("3 мср 2 мсб 38 омсбр") або null
  dated   - дата у форматі YYYY-MM-DD, якщо факт прив'язаний до дати, інакше null
  note    - звідки саме це в документі, одним рядком
  marked  - 1 якщо документ каже це ПРЯМО; 0 якщо це твій висновок (тоді в note - міркування)

ЗАЛІЗНІ ПРАВИЛА:
1. НЕ ВИГАДУЙ підрозділ. Не сказано - null. АЛЕ: якщо документ від початку описує конкретну
   частину, а посада названа в її межах («командир батальйону», «командир роти»), - став цю
   частину в unit. Це не здогадка, це контекст документа. Здогадка - це коли частину назвав ти. Цей підрозділ потім використовується, щоб приписати
   непідписану частоту; здогадка тут псує аналіз за три кроки звідси.
2. Позивний і прізвище - це ДВА різні записи про одну людину лише тоді, коли документ сам їх
   не пов'язує. Якщо пов'язує - один запис: term = позивний, у meaning додай прізвище.
3. Суперечність НЕ ЗЛИВАЙ і не розв'язуй. Пиши обидва варіанти окремими записами.
4. Командний склад - найцінніше. Кожна названа посада = окремий запис.
5. Рядовий склад теж бери (note: «рядовий склад»), але без роздування: один запис на людину.
6. Координати бери ДОСЛІВНО, символ у символ.
7. Якщо в шматку немає нічого вартого - поверни [].

Поверни JSON-масив і більше нічого."""

SUM_SYS = """Ти прочитав документ противника. Дай стислий підсумок українською: 3-6 речень.
Скажи, ЩО це за документ, чия частина, і головне - ЩО В НЬОМУ НОВОГО або особливо цінного для
радіорозвідки. Без вступів, без «цей документ». Тільки текст підсумку."""


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def _load_run():
    spec = importlib.util.spec_from_file_location("analytics2_run", REPO / "tools" / "analytics2" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- level 0: intake

def classify(name: str, head: str) -> str:
    low = name.lower()
    for rx, kind in KIND_RULES:
        if re.search(rx, low):
            return kind
    if low.endswith((".jpg", ".jpeg", ".png")):
        return "code_table"                  # every image that has arrived so far was one
    if DEBRIEF_RE.search(name) or "допит" in head[:3000].lower() or "полонен" in head[:3000].lower():
        return "pow_debrief"
    return "other"


def extract_text(path: Path) -> str:
    """Plain text of a document, cached. Returns '' for files with no text layer (images)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / (path.stem + ".txt")
    if cached.exists():
        return cached.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
    text = ""
    try:
        if suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            text = ""                        # needs eyes, not a parser
        elif suffix == ".doc":
            # The old binary format: our reader cannot open it, LibreOffice can. This silently
            # produced nothing on 22.09 until it was converted.
            subprocess.run(["soffice", "--headless", "--convert-to", "docx", "--outdir", "/tmp",
                            str(path)], capture_output=True, timeout=300)
            conv = Path("/tmp") / (path.stem + ".docx")
            text = _office(conv) if conv.exists() else ""
        elif suffix == ".pdf":
            r = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                               capture_output=True, timeout=600)
            text = r.stdout.decode("utf-8", "replace")
        else:
            text = _office(path)
    except Exception as e:                   # a failure is recorded, never fatal — TZ level 3
        text = ""
        print(f"   !! текст не витягнувся: {e!r}", file=sys.stderr)
    cached.write_text(text, encoding="utf-8")
    return text


def _office(path: Path) -> str:
    r = subprocess.run([sys.executable, str(REPO / "tools" / "docx_text.py"), str(path)],
                       capture_output=True, timeout=900)
    return r.stdout.decode("utf-8", "replace")


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def scan() -> int:
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS digests (sha TEXT PRIMARY KEY, file TEXT)")
    known = {r["file"] for r in con.execute("SELECT file FROM sources")}
    added = 0
    for f in sorted(RAW.iterdir()):
        if not f.is_file() or f.name in known:
            continue
        digest = sha(f)
        same = con.execute("SELECT file FROM digests WHERE sha=?", (digest,)).fetchone()
        head = extract_text(f)[:4000]
        kind = classify(f.name, head)
        status = f"duplicate_of:{same['file']}" if same else "pending"
        con.execute("INSERT INTO sources(file, caption, kind, status) VALUES (?,?,?,?)",
                    (f.name, None, kind, status))
        if not same:
            con.execute("INSERT OR IGNORE INTO digests(sha, file) VALUES (?,?)", (digest, f.name))
        added += 1
        print(f"  + {kind:<18} {status:<14} {f.name}")
    con.commit()
    return added


# ---------------------------------------------------------------- level 1: depth

DEPTH = {"pow_debrief": "FULL", "code_table": "EYES", "net_log": "FULL", "roster": "STRUCTURAL",
         "manual": "SKIM", "recognition_sheet": "SKIM", "register": "SKIM", "other": "SKIM"}


def plan() -> None:
    con = db()
    for r in con.execute("SELECT * FROM sources WHERE status='pending' ORDER BY file"):
        n = len(extract_text(RAW / r["file"]))
        print(f"  {DEPTH.get(r['kind'], 'SKIM'):<11} {r['kind']:<18} {n:>8} знаків  {r['file']}")


# ---------------------------------------------------------------- levels 2-3: read and write

def chunks(text: str) -> list[str]:
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + CHUNK])
        i += CHUNK - OVERLAP
    return out


def _json_array(raw: str) -> list[dict]:
    """Take what parses. A whole batch used to be dropped because one object in it was malformed —
    so on a parse failure the objects are recovered one at a time."""
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        out = []
        for obj in re.findall(r"\{[^{}]*\}", m.group(0), re.S):
            try:
                out.append(json.loads(obj))
            except json.JSONDecodeError:
                continue
        return out


def write_entries(con: sqlite3.Connection, rows: list[dict], source: str) -> int:
    n = 0
    for e in rows:
        kind = (e.get("kind") or "").strip()
        term = (e.get("term") or "").strip()
        if kind not in ENTRY_KINDS or not term:
            continue
        meaning = (e.get("meaning") or "").strip() or None
        unit = (e.get("unit") or None)
        # Same term, same unit, same meaning, already in the base — nothing gained by a second row.
        # A DIFFERENT meaning is kept on purpose: that is a contradiction, and contradictions are
        # evidence about rotation, not noise to be cleaned.
        dup = con.execute("SELECT 1 FROM entries WHERE kind=? AND term=? AND IFNULL(unit,'')=? "
                          "AND IFNULL(meaning,'')=?",
                          (kind, term, unit or "", meaning or "")).fetchone()
        if dup:
            continue
        cur = con.execute(
            "INSERT INTO entries(kind, term, meaning, unit, dated, note, marked, source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (kind, term, meaning, unit, e.get("dated") or None, (e.get("note") or "").strip() or None,
             1 if e.get("marked") in (1, True, "1", "true") else 0, source))
        con.execute("INSERT INTO entries_fts(rowid, term, meaning, note, unit) VALUES (?,?,?,?,?)",
                    (cur.lastrowid, term, meaning or "", e.get("note") or "", unit or ""))
        n += 1
    return n


ROSTER_COLS = {"должность": "post", "фио": "name", "позывной": "callsign", "в/звание": "rank",
               "воинское звание": "rank", "звание": "rank", "подразделение": "sub"}


def read_roster(text: str, source: str, con: sqlite3.Connection) -> tuple[int, str]:
    """A штатка is a TABLE: code reads it, no model. One person per row, and only when the row
    carries both a name and something that identifies him (callsign or post)."""
    lines = [l for l in text.split("\n") if l.strip()]
    header, cols = None, {}
    for i, l in enumerate(lines[:60]):
        cells = [c.strip().lower() for c in l.split("|")]
        hit = {ROSTER_COLS[c]: j for j, c in enumerate(cells) if c in ROSTER_COLS}
        if len(hit) >= 3:
            header, cols = i, hit
            break
    if header is None:
        return 0, "шапку таблиці не знайдено — потрібен ручний розбір"
    rows = []
    for l in lines[header + 1:]:
        cells = [c.strip() for c in l.split("|")]
        def cell(k):
            j = cols.get(k)
            return cells[j] if j is not None and j < len(cells) else ""
        name, cs, post = cell("name"), cell("callsign"), cell("post")
        if not name or len(name) < 4 or not (cs or post):
            continue
        meaning = ", ".join(x for x in (cell("rank"), post, name if cs else "") if x)
        rows.append({"kind": "person", "term": cs or name, "meaning": meaning or None,
                     "unit": cell("sub") or None, "dated": None, "marked": 1,
                     "note": f"штатка: {name}" + (f", {cs}" if cs else "")})
    n = write_entries(con, rows, source)
    return n, f"рядків у таблиці {len(rows)}, записано {n}"


def read_file(M, con: sqlite3.Connection, row: sqlite3.Row, model: str, effort: str) -> None:
    name, kind = row["file"], row["kind"]
    depth = DEPTH.get(kind, "SKIM")
    text = extract_text(RAW / name)
    print(f"\n== {name}\n   {kind} / {depth} / {len(text)} знаків", file=sys.stderr)

    if depth == "EYES" or (not text.strip() and (RAW / name).suffix.lower() in
                           (".jpg", ".jpeg", ".png", ".webp", ".gif")):
        # An image has no text layer, and guessing at one is how a code table gets corrupted.
        # It is parked for the main agent to LOOK at — that is a capability a job does not have.
        con.execute("UPDATE sources SET status='needs_eyes' WHERE file=?", (name,))
        con.commit()
        print("   -> очікує очей (зображення)", file=sys.stderr)
        return
    if not text.strip():
        con.execute("UPDATE sources SET status='failed:no_text' WHERE file=?", (name,))
        con.commit()
        return

    written, notes = 0, []
    if depth == "STRUCTURAL":
        written, why = read_roster(text, name, con)
        notes.append(why)
        if written == 0 and "шапку" in why:
            depth = "FULL"                   # fall back to reading it as prose, and SAY so
            notes.append("структурний розбір не вдався - читано моделлю")
    if depth in ("FULL", "SKIM"):
        parts = chunks(text) if depth == "FULL" else [text[:CHUNK]]
        for i, part in enumerate(parts, 1):
            try:
                raw, _ = M.call_model(SYS, f"Документ: {name}\n\n{part}", model, effort)
                got = _json_array(raw)
            except Exception as e:
                notes.append(f"шматок {i} впав: {e!r}")
                continue
            written += write_entries(con, got, name)
            print(f"   шматок {i}/{len(parts)}: +{len(got)}", file=sys.stderr)
            con.commit()

    summary = ""
    try:
        raw, _ = M.call_model(SUM_SYS, f"Документ: {name}\n\n{text[:14000]}", model, effort)
        summary = raw.strip()[:1500]
    except Exception as e:
        notes.append(f"підсумок не вийшов: {e!r}")
    status = "read" if depth == "FULL" else ("skimmed" if depth == "SKIM" else "read")
    con.execute("UPDATE sources SET status=?, summary=? WHERE file=?",
                (status, (summary + (" | " + "; ".join(notes) if notes else "")).strip(), name))
    con.commit()
    print(f"   -> {status}, записів +{written}", file=sys.stderr)


def run(limit: int, model: str, effort: str) -> None:
    con = db()
    M = _load_run()
    pend = con.execute("SELECT * FROM sources WHERE status='pending' ORDER BY "
                       "CASE kind WHEN 'pow_debrief' THEN 0 WHEN 'code_table' THEN 1 ELSE 2 END, "
                       "file").fetchall()
    if limit:
        pend = pend[:limit]
    print(f"до читання: {len(pend)} файлів   модель: {model} ({effort})", file=sys.stderr)
    t0 = datetime.now()
    for row in pend:
        try:
            read_file(M, con, row, model, effort)
        except Exception as e:               # one bad file never stops the batch
            con.execute("UPDATE sources SET status=? WHERE file=?",
                        (f"failed:{type(e).__name__}", row["file"]))
            con.commit()
            print(f"   !! {row['file']}: {e!r}", file=sys.stderr)
    print(f"\nзагалом {(datetime.now()-t0).seconds//60} хв", file=sys.stderr)
    report()


def report() -> None:
    con = db()
    print("\n=== стан бази ===")
    for r in con.execute("SELECT status, count(*) n FROM sources GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']:<24} {r['n']}")
    print(f"  записів усього: {con.execute('SELECT count(*) FROM entries').fetchone()[0]}")
    print("\n=== за типом запису ===")
    for r in con.execute("SELECT kind, count(*) n FROM entries GROUP BY kind ORDER BY n DESC"):
        print(f"  {r['kind']:<18} {r['n']}")
    eyes = con.execute("SELECT file FROM sources WHERE status='needs_eyes'").fetchall()
    if eyes:
        print("\n=== чекає, щоб я подивився очима ===")
        for r in eyes:
            print("  " + r["file"])
    # ВЕРДИКТ ОСТАННІМ РЯДКОМ. 04.10.2026 прогін завершився «успішно», поклавши три найцінніші
    # таблиці в чергу needs_eyes - і це було видно лише посеред виводу. Звіт, що закінчується
    # словом «усе прочитано», не дає прийняти чергу за зроблену роботу.
    bad = con.execute("SELECT file, status FROM sources WHERE status LIKE 'failed%'").fetchall()
    if bad:
        print("\n=== не прочитано ===")
        for r in bad:
            print(f"  {r['status']:<22} {r['file']}")

    pend = con.execute("SELECT count(*) n FROM sources WHERE status='pending'").fetchone()["n"]
    eyes_n = len(eyes)
    bad_n = len(bad)
    print("\n" + "=" * 60)
    if eyes_n or pend or bad_n:
        parts = []
        if eyes_n:
            parts.append(f"{eyes_n} чекає моїх очей")
        if pend:
            parts.append(f"{pend} не читано")
        if bad_n:
            parts.append(f"{bad_n} впало")
        print("!!! РОБОТА НЕ ЗАВЕРШЕНА: " + ", ".join(parts))
        if eyes_n:
            print("    -> python3 tools/glossary/ingest.py eyes   (шляхи, щоб відкрити й прочитати)")
    else:
        print("усе прочитано, черги нема")
    print("=" * 60)


def eyes() -> None:
    """Файли, які конвеєр читати не вміє: фотографії аркушів без текстового шару.

    Вони не «пропущені» - вони адресовані МЕНІ. Конвеєр бачить лише текст, а таблицю на фото
    читають очима; після читання запис ставиться руками й джерело переходить у `read_eyes`,
    щоб потім було видно, що саме внесла людина, а що модель."""
    con = db()
    rows = con.execute("SELECT file, kind FROM sources WHERE status='needs_eyes' ORDER BY file").fetchall()
    if not rows:
        print("черги нема - усе прочитано")
        return
    print(f"чекають очей: {len(rows)}\n")
    for r in rows:
        print(f"  {r['kind']:<14} {RAW / r['file']}")
    print("\nпрочитати очима -> внести через ingest.write_entries(..., source=<імʼя файлу>)")
    print("і поставити sources.status='read_eyes'")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("verb", choices=["scan", "plan", "run", "report", "eyes"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--effort", default="medium")
    a = ap.parse_args()
    if a.verb == "scan":
        print(f"зареєстровано нових: {scan()}")
    elif a.verb == "plan":
        plan()
    elif a.verb == "run":
        run(a.limit, a.model, a.effort)
    elif a.verb == "eyes":
        eyes()
    else:
        report()


if __name__ == "__main__":
    main()
