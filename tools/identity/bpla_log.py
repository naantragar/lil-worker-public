#!/usr/bin/env python3
"""«БпЛА логістики» — who flies THEIR delivery drones (water, food, ammo, batteries dropped to positions).

    python3 tools/identity/bpla_log.py --since 2026-08-15      # backfill (long: run as a durable job)
    python3 tools/identity/bpla_log.py                         # daily: since the last processed intercept
    python3 tools/identity/bpla_log.py --since 2026-11-01 --limit 24 --dry-model   # look, write nothing

Owner, 03.11.2026 (variant 2 of three offered). The report events almost never name the delivery
operator — three cases in 74 reports — because the line is about WHAT was delivered to WHOM. The
station header of such an intercept mostly carries the commander who ORDERED the drop (СТРАННИК,
ЧЕБУРАШКА, БОРЕЦ), so taking callsigns from the header would only rebuild «ком склад».

Two stages, as in posady.py:
  1. a sieve, no model: speech that names a delivery (скинуть, сброс, закинуть, посылка, вода, БК,
     аккумулятор, баба-яга…) AND a drone (птичка, борт, бпла, дрон, баба). ~13 a day on our air.
  2. the model reads only those and names the OPERATOR only when the speech shows that this callsign
     flies or drops: «Ветер, скидывай», «я Бобер, скинул», «борт Чипа пошёл с водой». The quote must
     be in the speech verbatim.

Unlike a post heard on air this is an inference, so it needs the накопичувач threshold: 3 cases on
at least 2 days, per callsign + formation. Signed nets only (owner): a header with no formation is
skipped. Every intercept read is remembered in `bpla_seen`; a re-run reads only new ones.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "analytics2"))
import roles_db                                              # noqa: E402
from posady import CORPUS, GROUPS, KYIV, parse               # noqa: E402
from run import call_model, extract_json                     # noqa: E402

import sqlite3                                               # noqa: E402

MODEL, EFFORT = "claude-opus-5-5", "medium"
BATCH = 12
ROLE = "bpla_log"

# Measured 03.11 on 20.10-03.11: the first, wide sieve took 1057 intercepts, half of them on
# «бабка / Баба-Яга» — which on their air is OUR heavy bomber, a threat, not their delivery — and on
# «БпЛА», which mostly sits in the analyst's comment. Both are out; a drone word must be THEIR word
# for their own craft, and the delivery word must be a drop or a cargo.
DELIVERY = re.compile(r"скин(ь|уть|ул|ет|ем|ешь|у\b)|скидыва|закин|посылк|доставк|подвес|"
                      r"вод(у|ичк|ы\b)|провизи|сухпа|\bБК\b|аккумулятор|\bакб\b|сброс", re.I)
CARGO = re.compile(r"посылк|вод(у|ичк|ы\b)|провизи|сухпа|\bБК\b|аккумулятор|\bакб\b|еда|еду|жратв|"
                   r"медикамент|бинт|турникет|сигарет|батаре|рацию|раци[ия]|генератор|зарядк|паек|пайк", re.I)
DRONE = re.compile(r"птич|борт|дрон|крыл|вертушк|мавик|коптер|самол[её]т", re.I)


SYSTEM = """Ти читаєш радіоперехоплення противника про доставку вантажу дронами (вода, їжа, БК,
акумулятори, медикаменти скидаються на позиції). Завдання - знайти ОПЕРАТОРА дрона доставки: того,
хто сам літає / скидає вантаж. Виписуй позивний ТІЛЬКИ коли мова прямо показує, що саме він це робить:
  • до нього звертаються з вимогою злітати чи скинути: «Ветер, скидывай», «Бобер, вылетай с водой»;
  • він сам про себе: «я Чип, скинул», «Сова, борт пошёл»;
  • про нього кажуть як про того, хто доставляє: «у Шакала птичка с БК уже летит».
НЕ виписуй:
  • того, хто ЗАМОВЛЯЄ доставку чи командує (просить «закиньте воды» - це не оператор);
  • того, КОМУ везуть (отримувача на позиції);
  • командира, який ОБІЦЯЄ чи ОРГАНІЗОВУЄ доставку («я вам скину воды», «сейчас закинем»), якщо з мови
    не видно, що він сам керує дроном - «скину» в їхній мові часто означає «організую»;
  • ударні дрони, FPV, скиди боєприпасів на ворога, перехоплювачі - тільки доставка СВОЇМ;
  • наші (українські) дрони, про які вони говорять як про загрозу;
  • здогадки: якщо з мови не видно, хто саме літає - нічого.
Позивний - ВЕЛИКИМИ літерами. `cargo` - що везуть, коротко (вода, БК, АКБ, їжа…) або порожньо.
`quote` - дослівна цитата з мови, де це видно.
Відповідь - ТІЛЬКИ JSON-масив: [{"id": <номер перехоплення>, "callsign": "...", "cargo": "...",
"quote": "..."}]. Нічого не знайдено - [].
"""


def candidates(con: sqlite3.Connection, since_ms: int, limit: int | None) -> list[dict]:
    db = sqlite3.connect(CORPUS, uri=True)
    seen_body, out = set(), []
    q = ("SELECT timestamp, group_name, text FROM messages WHERE timestamp >= ? AND group_name IN "
         f"({','.join('?' * len(GROUPS))}) ORDER BY timestamp")
    for _ts, g, txt in db.execute(q, (since_ms, *GROUPS)):
        if not txt or not DELIVERY.search(txt) or not DRONE.search(txt) or not CARGO.search(txt):
            continue
        r = parse(txt)
        if not r:
            continue
        sp = " ".join(r["speech"])
        if not DELIVERY.search(sp) or not DRONE.search(sp) or not CARGO.search(sp):
            continue
        if not roles_db.formation(r["header"]):
            continue                    # signed nets only (owner, 03.11.2026)
        body = re.sub(r"\W", "", sp)[:200]
        if body in seen_body:
            continue                    # the same transmission through the other group
        seen_body.add(body)
        if con.execute("SELECT 1 FROM bpla_seen WHERE ref=?", (r["ref"],)).fetchone():
            continue
        r["group"] = g
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


def read_batch(batch: list[dict]) -> list[dict]:
    parts = []
    for i, r in enumerate(batch):
        parts.append(f"### Перехоплення {i}\nМережа: {r['header']}\nСтанції: {', '.join(r['stations'])}\n"
                     + "\n".join(r["speech"])[:2500])
    try:
        out, _ = call_model(SYSTEM, "\n\n".join(parts), MODEL, EFFORT)
    except Exception as e:                                   # noqa: BLE001 — one batch, not the run
        print(f"!!! пачка не прочиталась: {e!r}", file=sys.stderr)
        return [{"_failed": [r["ref"] for r in batch]}]
    found = []
    for x in extract_json(out):
        try:
            r = batch[int(x.get("id"))]
        except (TypeError, ValueError, IndexError):
            continue
        cs = str(x.get("callsign") or "").strip().upper()
        quote = str(x.get("quote") or "").strip()
        if not re.fullmatch(roles_db.NAME, cs) or not quote:
            continue
        flat = re.sub(r"\W", "", " ".join(r["speech"])).lower()
        if re.sub(r"\W", "", quote).lower()[:40] not in flat:
            continue                    # a paraphrase is how an invented operator would look
        found.append({**r, "callsign": cs, "cargo": str(x.get("cargo") or "").strip(), "quote": quote})
    return found


def secs(ref: str) -> int:
    d, t = ref.split()[:2]
    dd, mm = d.split(".")
    h, m, s = t.split(":")
    return ((int(mm) * 31 + int(dd)) * 24 + int(h)) * 3600 + int(m) * 60 + int(s)


def promote(con: sqlite3.Connection, now: str) -> list[tuple]:
    """3 cases on 2+ days, per callsign + formation. Never demotes, never touches `owner`."""
    newly = []
    for cs, unit in con.execute("SELECT DISTINCT callsign, unit FROM evidence WHERE role=?", (ROLE,)).fetchall():
        days = [d for (d,) in con.execute("SELECT day FROM evidence WHERE role=? AND callsign=? AND unit=? "
                                          "ORDER BY day, event_time", (ROLE, cs, unit))]
        net = con.execute("SELECT net FROM evidence WHERE role=? AND callsign=? AND unit=? GROUP BY net "
                          "ORDER BY COUNT(*) DESC LIMIT 1", (ROLE, cs, unit)).fetchone()[0]
        con.execute("INSERT OR IGNORE INTO verdict(role,callsign,unit,net_label,updated) VALUES(?,?,?,?,?)",
                    (ROLE, cs, unit, net, now))
        seen, crossed = set(), ""
        for n, d in enumerate(days, 1):
            seen.add(d)
            if n >= roles_db.MIN_EPISODES and len(seen) >= roles_db.MIN_DAYS:
                crossed = d
                break
        if crossed:
            cur = con.execute("UPDATE verdict SET machine='active', activated=?, updated=? "
                              "WHERE role=? AND callsign=? AND unit=? AND machine!='active'",
                              (crossed, now, ROLE, cs, unit))
            if cur.rowcount:
                newly.append((cs, unit, len(days), len(set(days))))
    return newly


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD; default: the day of the last processed intercept")
    ap.add_argument("--limit", type=int, help="read at most N intercepts (a trial)")
    ap.add_argument("--dry", action="store_true", help="show the sieve size, call no model")
    ap.add_argument("--dry-model", action="store_true", help="call the model, print, write nothing")
    ap.add_argument("--jobs", type=int, default=6)
    a = ap.parse_args()
    con = roles_db.connect()
    con.executescript("CREATE TABLE IF NOT EXISTS bpla_seen (ref TEXT PRIMARY KEY, ts TEXT);")
    since = a.since or (con.execute("SELECT MAX(ts) FROM bpla_seen").fetchone()[0] or "2026-08-15")[:10]
    since_ms = int(datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=KYIV).timestamp() * 1000)
    cand = candidates(con, since_ms, a.limit)
    print(f"сито: {len(cand)} перехоплень про доставку дроном з {since}", flush=True)
    if a.dry or not cand:
        return
    batches = [cand[i:i + BATCH] for i in range(0, len(cand), BATCH)]
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        results = list(ex.map(read_batch, batches))
    failed = {ref for found in results for f in found for ref in f.get("_failed", [])}
    flat = sorted((f for found in results for f in found if "_failed" not in f), key=lambda f: f["ref"])
    if a.dry_model:
        for f in flat:
            print(f"  {f['callsign']:12} {f['cargo'][:14]:14} | {f['time']} | {f['header'][:40]} | «{f['quote'][:100]}»")
        print(f"знайдено {len(flat)}, не прочиталось пачок: {len(failed) // BATCH + bool(len(failed) % BATCH)}")
        return
    now = datetime.now().isoformat(timespec="seconds")
    added = 0
    for f in flat:
        unit = roles_db.formation(f["header"])
        # one transmission through two groups with slightly different transcripts = one case
        near = [k for (k,) in con.execute("SELECT src_key FROM evidence WHERE role=? AND callsign=?",
                                          (ROLE, f["callsign"])) if abs(secs(k) - secs(f["ref"])) <= 120]
        if near:
            continue
        cur = con.execute(
            "INSERT OR IGNORE INTO evidence(role,callsign,unit,src_key,day,kind,partners,event_time,"
            "event_text,net,src_refs,report,added) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ROLE, f["callsign"], unit, f["ref"], f["day"], f["cargo"] or "доставка", "", f["time"],
             f"{('доставка: ' + f['cargo']) if f['cargo'] else 'доставка'}: «{f['quote']}»", f["header"],
             json.dumps([f["ref"]], ensure_ascii=False), "", now))
        added += cur.rowcount
    # A batch the model failed on is NOT marked as read, so the next run tries it again.
    for c in cand:
        if c["ref"] not in failed:
            con.execute("INSERT OR IGNORE INTO bpla_seen VALUES(?,?)", (c["ref"], c["day"]))
    newly = promote(con, now)
    con.commit()
    print(f"знайдено операторів-згадок: {len(flat)}, нових свідчень {added}, "
          f"не прочитано (повтор наступного разу): {len(failed)}")
    print(f"нових активних «БпЛА логістики»: {len(newly)}")
    for cs, unit, n, d in newly:
        print(f"  + {cs} ({unit}) - {n} випадків за {d} діб")


if __name__ == "__main__":
    main()
