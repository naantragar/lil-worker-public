#!/usr/bin/env python3
"""«Посади» — posts NAMED in the air: «Манул, это ком роты», «ты командир взвода», «НШ Коса».

    python3 tools/identity/posady.py --since 2026-08-15      # backfill
    python3 tools/identity/posady.py                         # since the last processed intercept

Owner, 03.11.2026: a post somebody SAYS on the air is not inferred, it is heard, so one explicit
statement is enough (the behavioural roles in roles_db.py need three). It goes into the same
`roles.db` as role `posada`, with the verbatim quote, and he confirms or removes it in the bot.

Two stages, the usual split:
  1. a sieve, no model: intercepts whose SPEECH names a post (комбат, командир роты/взвода/…, НШ,
     замком, ротный, взводный). ~116 in 2.5 months — cheap to read all of them.
  2. the model reads ONLY those and writes down a pair «callsign — post» when the speech itself ties
     them. «Комбат дал добро» with no name ties nothing and yields nothing. It never guesses.

Every intercept handed to the model is remembered in `posada_seen`, so a re-run reads only new ones.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "analytics2"))
import roles_db                                              # noqa: E402
from run import call_model, extract_json                     # noqa: E402

CORPUS = "file:~/wa-monitor/messages.db?mode=ro"
GROUPS = ("Invisible Hand", "AllInARow", "PATAGONIA_GP")
KYIV = ZoneInfo("Europe/Kyiv")
MODEL, EFFORT = "claude-opus-5-5", "medium"
BATCH = 12

TITLE = re.compile(r"ком(?:андир)?\.?\s*(?:роты|взвода|батальона|бата|отделения|группы|полка|бригады|"
                   r"расчета|расчёта)|ротн(?:ый|ого|ому)|взводн(?:ый|ого|ому)|комбат\w*|начальник\w* штаба|"
                   r"\bНШ\b|замком\w*|зам(?:еститель)?\s+командира|старш\w+ групп\w*", re.I)

SYSTEM = """Ти читаєш радіоперехоплення противника. У кожному з них хтось вимовляє ПОСАДУ
(комбат, командир роти/взводу/відділення/групи/полку, начальник штабу, заступник командира, ротний,
взводний). Твоє завдання - виписати пару «позивний - посада» ТІЛЬКИ коли сама мова прямо пов'язує
посаду з позивним:
  • «Позывной Манул, это ком роты» → МАНУЛ - командир роти
  • «Сварщик ещё командир, командир взвода» → СВАРЩИК - командир взводу
  • ДОК каже ТУЧІ: «Туча, ты командир взвода или кто?» → ТУЧА - командир взводу
    (лише якщо зі звертання в цій же репліці або з шапки станцій однозначно видно, до кого звертаються)
НЕ виписуй:
  • посаду без імені: «комбат дал добро», «доложи комбату» - нічого;
  • здогадки з поведінки: хто наказує чи веде - це НЕ посада;
  • посаду «союзника» без позивного.
Посаду пиши українською, коротко: командир батальйону, командир роти, командир взводу, командир
відділення, командир групи, командир полку, начальник штабу, заступник командира роти, тощо.
Позивний - ВЕЛИКИМИ літерами, як у шапці або в мові.
`unit` - частина, ДО ЯКОЇ НАЛЕЖИТЬ САМА ЛЮДИНА, і тільки якщо мова її називає: часто говорять про
ЧУЖИХ - союзників, сусідів, «со 155-ки», «у них», «189 полк» - тоді `unit` = саме ця частина
(«155», «104», «189 полк»), а НЕ мережа, в якій це прозвучало. Якщо людина зі своєї мережі або частину
не названо - порожньо. `ally` - true, якщо людину названо союзником / з іншої частини, інакше false.
`quote` - дослівна цитата з мови, де це сказано.
Відповідь - ТІЛЬКИ JSON-масив: [{"id": <номер перехоплення>, "callsign": "...", "title": "...",
"unit": "...", "ally": false, "quote": "..."}]. Нічого не знайдено - [].
"""


def parse(txt: str) -> dict | None:
    lines = [l.strip() for l in txt.split("\n")]
    dt = next((re.match(r"(\d{2})\.(\d{2})\.(\d{4}),\s*(\d{2}:\d{2}:\d{2})", l) for l in lines
               if re.match(r"\d{2}\.\d{2}\.\d{4},\s*\d{2}:\d{2}", l)), None)
    fr = next((l for l in lines if re.fullmatch(r"\d{3}\.\d{2,4}", l)), None)
    hi = next((i for i, l in enumerate(lines) if l.startswith(("УКХ", "КХ"))), None)
    if not dt or hi is None:
        return None
    header = lines[hi]
    stations, j = [], hi + 1
    while j < len(lines) and lines[j] and not re.match(r"^(\d+\s*[–—-]|[–—-])", lines[j]) \
            and re.fullmatch(r"[\dА-ЯЁІЇЄҐA-Z ,\-–—()/.:]+", lines[j]):
        stations.append(lines[j])
        j += 1
    speech = [l for l in lines[j:] if l]
    comment = " ".join(l for l in lines[:hi] if "Коментар" in l or "Мітки" in l)
    return {"day": f"{dt.group(3)}-{dt.group(2)}-{dt.group(1)}",
            "ref": f"{dt.group(1)}.{dt.group(2)} {dt.group(4)} {fr or '?'}",
            "time": f"{dt.group(1)}.{dt.group(2)}.{dt.group(3)}, {dt.group(4)[:5]}",
            "header": header, "stations": stations, "speech": speech, "comment": comment}


def candidates(con: sqlite3.Connection, since_ms: int) -> list[dict]:
    db = sqlite3.connect(CORPUS, uri=True)
    seen_body, out = set(), []
    q = ("SELECT timestamp, group_name, text FROM messages WHERE timestamp >= ? AND group_name IN "
         f"({','.join('?' * len(GROUPS))}) ORDER BY timestamp")
    for ts, g, txt in db.execute(q, (since_ms, *GROUPS)):
        if not txt or not TITLE.search(txt):
            continue
        r = parse(txt)
        if not r or not any(TITLE.search(s) for s in r["speech"]):
            continue
        body = re.sub(r"\W", "", " ".join(r["speech"]))[:200]
        if body in seen_body:
            continue                    # the same transmission through the other group
        seen_body.add(body)
        if con.execute("SELECT 1 FROM posada_seen WHERE ref=?", (r["ref"],)).fetchone():
            continue
        r["group"] = g
        out.append(r)
    return out


def read_batch(batch: list[dict]) -> list[dict]:
    parts = []
    for i, r in enumerate(batch):
        parts.append(f"### Перехоплення {i}\nМережа: {r['header']}\nСтанції: {', '.join(r['stations'])}\n"
                     + "\n".join(r["speech"])[:2500])
    out, _ = call_model(SYSTEM, "\n\n".join(parts), MODEL, EFFORT)
    found = []
    for x in extract_json(out):
        try:
            r = batch[int(x.get("id"))]
        except (TypeError, ValueError, IndexError):
            continue
        cs = str(x.get("callsign") or "").strip().upper()
        title = str(x.get("title") or "").strip().lower()
        quote = str(x.get("quote") or "").strip()
        if not re.fullmatch(roles_db.NAME, cs) or not title or not quote:
            continue
        # The quote must really be in the speech — a paraphrase is how an invented post would look.
        flat = re.sub(r"\W", "", " ".join(r["speech"])).lower()
        if re.sub(r"\W", "", quote).lower()[:40] not in flat:
            continue
        found.append({**r, "callsign": cs, "title": title, "unit_said": str(x.get("unit") or "").strip(),
                      "ally": bool(x.get("ally")), "quote": quote})
    return found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD; default: the day of the last processed intercept")
    ap.add_argument("--dry", action="store_true", help="show the sieve size, call no model")
    ap.add_argument("--rebuild", action="store_true",
                    help="forget every post the owner has not judged and re-read the sieve from --since")
    a = ap.parse_args()
    con = roles_db.connect()
    con.executescript("CREATE TABLE IF NOT EXISTS posada_seen (ref TEXT PRIMARY KEY, ts TEXT);")
    cols = [r[1] for r in con.execute("PRAGMA table_info(verdict)")]
    if "title" not in cols:
        con.execute("ALTER TABLE verdict ADD COLUMN title TEXT")
    if a.rebuild:
        con.execute("DELETE FROM posada_seen")
        con.execute("DELETE FROM evidence WHERE role='posada' AND callsign NOT IN "
                    "(SELECT callsign FROM verdict WHERE role='posada' AND owner IS NOT NULL)")
        con.execute("DELETE FROM verdict WHERE role='posada' AND owner IS NULL")
    since = a.since or (con.execute("SELECT MAX(ts) FROM posada_seen").fetchone()[0] or "2026-08-15")[:10]
    since_ms = int(datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=KYIV).timestamp() * 1000)
    cand = candidates(con, since_ms)
    print(f"сито: {len(cand)} перехоплень з назвою посади з {since}")
    if a.dry or not cand:
        return
    batches = [cand[i:i + BATCH] for i in range(0, len(cand), BATCH)]
    # Every batch is read TWICE and the reads are unioned: one read misses clear statements at random
    # (03.11 rebuild lost «Лазерь это командир роты», which the first run had). The quote check keeps
    # the union honest; a miss, by contrast, is permanent, since `posada_seen` never re-reads.
    with ThreadPoolExecutor(max_workers=6) as ex:
        reads = list(ex.map(read_batch, batches + batches))
    results = [reads[i] + reads[i + len(batches)] for i in range(len(batches))]
    now = datetime.now().isoformat(timespec="seconds")
    added = 0
    def secs(ref: str) -> int:
        d, t = ref.split()[:2]
        dd, mm = d.split(".")
        h, m, sec = t.split(":")
        return ((int(mm) * 31 + int(dd)) * 24 + int(h)) * 3600 + int(m) * 60 + int(sec)

    # A post the speech ties to a NAMED unit goes first, so when the other group's transcript of the
    # same transmission lacks «189 полк», that copy is the one dropped as a duplicate.
    flat = sorted((f for found in results for f in found), key=lambda f: (not f["unit_said"], f["ref"]))
    for f in flat:
        if True:
            # The man's OWN unit: a post said about allies or another unit («со 155-ки», «у них, 189
            # полк») belongs to that unit, not to the net it was heard on — first run filed MANUL
            # (189) and RUSSKIY (155) under the speaker's 127 мсд / 60 омсбр.
            said = f["unit_said"]
            unit = (roles_db.formation(said) or re.sub(r"\s+", " ", said)[:24]) if said else ""
            own = roles_db.formation(f["header"]) or ("част " + f["ref"].split()[-1])
            other = bool(unit) and unit != own
            unit = unit or own
            # One transmission arrives through two groups with a slightly different transcript, so
            # the body dedupe misses it: the same man and post within two minutes is one statement.
            near = [k for (k,) in con.execute("SELECT src_key FROM evidence WHERE role='posada' AND "
                                              "callsign=?", (f["callsign"],))
                    if abs(secs(k) - secs(f["ref"])) <= 120]
            if near:
                continue
            cur = con.execute(
                "INSERT OR IGNORE INTO evidence(role,callsign,unit,src_key,day,kind,partners,event_time,"
                "event_text,net,src_refs,report,added) VALUES('posada',?,?,?,?,?,?,?,?,?,?,?,?)",
                (f["callsign"], unit, f["ref"], f["day"], f["title"], "", f["time"],
                 f"{f['title']}{' (чужа частина)' if other else ''}: «{f['quote']}»", f["header"], json.dumps([f["ref"]], ensure_ascii=False),
                 "", now))
            added += cur.rowcount
            con.execute("INSERT OR IGNORE INTO verdict(role,callsign,unit,net_label,updated) "
                        "VALUES('posada',?,?,?,?)", (f["callsign"], unit, f["header"], now))
            # one heard statement is enough; the latest title wins if he is named twice differently
            con.execute("UPDATE verdict SET machine='active', title=?, updated=?, "
                        "activated=COALESCE(activated, ?) WHERE role='posada' AND callsign=? AND unit=?",
                        (f["title"], now, f["day"], f["callsign"], unit))
    for c in cand:
        con.execute("INSERT OR IGNORE INTO posada_seen VALUES(?,?)", (c["ref"], c["day"]))
    con.commit()
    print(f"знайдено пар «позивний - посада» (два прочитання разом): {len(flat)}, нових свідчень {added}")
    for f in flat:
        if True:
            print(f"  {f['callsign']:12} {f['title']:24} | {f['time']} | «{f['quote'][:90]}»")


if __name__ == "__main__":
    main()
