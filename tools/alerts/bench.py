#!/usr/bin/env python3
"""Benchmark a model as the critical-event alerter over one day of intercepts.

    python3 tools/alerts/bench.py --day 2026-09-27 --model claude-haiku-4-5
    python3 tools/alerts/bench.py --day 2026-09-27 --model claude-sonnet-5
    python3 tools/alerts/bench.py --day 2026-09-27 --model claude-opus-5 --effort high

One run = one model reading the whole day and saying which intercepts a duty officer must be woken
for. Results go to `knowledge/upstream/alerts/runs/<day>_<model>.json`; score them against the hand
written ground truth with `score.py`.

Two design choices that are also the production design:

**Windows, not single intercepts.** The model reads 10 consecutive intercepts of ONE net in one
call and answers about all of them. That is ~10x cheaper than a call per intercept AND strictly
better, because the day's decisive event (a five-hour interrogation) is only legible as a thread.
Windows overlap by 2 so nothing sits alone at a seam.

**Priority of sources.** Invisible Hand and AllInARow are read first; a Patagonia intercept whose
speech duplicates one already taken is dropped, but Patagonia-only traffic is kept. The owner's
rule: IH and AllInARow first, Patagonia second, dedup at the level of text.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import difflib
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CORPUS = "~/wa-monitor/messages.db"
TRANSFER_JID = "120363408965106490@g.us"
OUT = REPO / "knowledge" / "upstream" / "alerts" / "runs"
BARE_CWD = Path("/tmp/krevetka-alerts")
NO_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "Task"]

PRIORITY = {"Invisible Hand": 0, "AllInARow": 1, "All in a Row": 1}
# ❗ і ‼️ - червоні; ❕ білий і не рахується
OPERATOR_MARK = re.compile("[\u2757\u203c]")
OPERATOR_TRUSTED = {"Invisible Hand", "AllInARow", "All in a Row"}
FREQ_LINE = re.compile(r"^\d{2,4}[.,]\d{2,4}$")
SPEECH_LINE = re.compile(r"^\s*(\d+\s*)?[–—-]\s")
RE_AIR_DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4},")

DOCTRINE = Path(__file__).resolve().parent / "doctrine"


def doctrine(name: str) -> str:
    """The rules are versioned files, not a constant, so a threshold experiment is reversible.
    v1 - strict; gave 6 push on a live day and the owner liked that shape.
    v2 - loosened after his calibration set; blew out to 370 push, unusable, kept for the record.
    v3 - v2 with hard bars on the three new classes and a narrow doubt rule."""
    f = DOCTRINE / f"{name}.md"
    if not f.exists():
        sys.exit(f"нема доктрини {name}: {f}")
    return f.read_text(encoding="utf-8").strip()


# ── corpus ───────────────────────────────────────────────────────────────────────────────────────

def parse_intercept(text: str) -> dict:
    """Розібрати перехоплення на блоки за стандартом, який описав власник 01.10.2026.

        [КОМЕНТАР згори - один-два рядки, інколи з «Коментар:», інколи просто текст]
        (порожній рядок)
        ТЕХНІЧНИЙ БЛОК: дата й час · частота · шапка мережі · позивні (або НВ)
        (порожній рядок)
        ДІАЛОГ - рядки з тире
        (порожній рядок)
        [КОМЕНТАР знизу - так само, один-два рядки]

    Коментар стоїть АБО над усім, АБО під усім, і трапляється і так, і так. Це важливо
    двічі: він буває єдиним місцем, де взагалі сказано, про що перехоплення, і червоний знак
    оклику має значення ЛИШЕ в ньому. Знак у шапці мережі («ЦФП63❗❗❗») - це позначка на
    частоту, а не вердикт про цю розмову, і піднімати по ньому в червоний канал не можна."""
    lines = text.splitlines()
    date_i = next((i for i, l in enumerate(lines) if RE_AIR_DATE.match(l.strip())), None)
    sp_idx = [i for i, l in enumerate(lines) if SPEECH_LINE.match(l)]
    first_sp = sp_idx[0] if sp_idx else len(lines)
    last_sp = sp_idx[-1] if sp_idx else -1

    if date_i is None:
        top, tech_lo = [], 0
    else:
        top, tech_lo = lines[:date_i], date_i
    tech = lines[tech_lo:first_sp]
    bottom = lines[last_sp + 1:] if last_sp >= 0 else []

    def clean(ls):
        return [l.strip() for l in ls if l.strip()]

    return {"comment": clean(top) + clean(bottom),
            "tech": clean(tech),
            "speech": "\n".join(lines[i] for i in sp_idx).strip()}


def operator_comment(text: str) -> str:
    return " / ".join(parse_intercept(text)["comment"])


def speech(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if SPEECH_LINE.match(l)).strip()


def freq_of(text: str) -> str:
    for l in (x.strip() for x in text.splitlines()):
        if FREQ_LINE.match(l):
            return l.replace(",", ".")
    return "?"


def norm(s: str) -> str:
    return re.sub(r"[^а-яіїєґa-z0-9]+", "", s.lower())


def load_day(day: str, hours: int = 0, minutes: int = 0) -> list[dict]:
    """A calendar day, or — with `hours` — the last N hours ending now. The alerting system runs on
    fresh traffic, so the rolling window is the real shape; the calendar day exists for the
    benchmark, where the ground truth is written against a fixed day."""
    if hours or minutes:
        end = datetime.now()
        span = timedelta(hours=hours, minutes=minutes)
        lo = int((end - span).timestamp() * 1000)
        hi = int(end.timestamp() * 1000)
    else:
        d = datetime.strptime(day, "%Y-%m-%d")
        lo = int(d.timestamp() * 1000)
        hi = int((d + timedelta(days=1)).timestamp() * 1000)
    con = sqlite3.connect(f"file:{CORPUS}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT id, timestamp, group_name, coalesce(text,'') FROM messages "
        "WHERE timestamp BETWEEN ? AND ? AND coalesce(group_jid,'') != ? "
        "AND coalesce(category,'') != 'transfer' ORDER BY timestamp", (lo, hi, TRANSFER_JID)).fetchall()
    con.close()
    out = []
    for mid, ts, g, t in rows:
        sp = speech(t)
        if not sp:
            continue
        out.append({"id": mid, "ts": ts, "src": g, "text": t,
                    "hhmmss": datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S"),
                    "freq": freq_of(t), "speech": sp})
    return out


def dedup(rows: list[dict]) -> tuple[list[dict], int]:
    """IH and AllInARow win; a Patagonia row whose speech repeats one already taken is dropped.
    Comparison is fuzzy - the two transcriptions of one transmission differ by a few words."""
    rows = sorted(rows, key=lambda r: (PRIORITY.get(r["src"], 9), r["ts"]))
    kept: list[dict] = []
    by_freq: dict[str, list[tuple[int, str, dict]]] = {}
    dropped = 0
    for r in rows:
        n = norm(r["speech"])
        bucket = by_freq.setdefault(r["freq"], [])
        dup = None
        for ts, other, orig in bucket:
            if abs(ts - r["ts"]) > 15 * 60 * 1000:
                continue
            if difflib.SequenceMatcher(None, n[:600], other[:600]).ratio() > 0.82:
                dup = orig
                break
        if dup is not None:
            dup.setdefault("also", []).append(r["id"])
            dropped += 1
            continue
        bucket.append((r["ts"], n, r))
        kept.append(r)
    return sorted(kept, key=lambda r: r["ts"]), dropped


def seen_filter(wins: list[list[dict]], state: str) -> tuple[list[list[dict]], int]:
    """Keep only windows that contain at least one intercept the watcher has never read.

    This is what lets the cadence drop to minutes without the work growing: a 45-minute slice is
    ~24 windows, but three minutes of traffic touches only ~4 nets, so ~4 windows actually changed.

    Tracked by ID, deliberately NOT by a time cursor: an analyst can post an intercept twelve
    minutes after the air, so a row with an OLD timestamp can arrive AFTER the cursor moved past
    it, and a cursor would silently skip it forever."""
    con = sqlite3.connect(state)
    con.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, at TEXT)")
    con.commit()
    known = {r[0] for r in con.execute("SELECT id FROM seen")}
    con.close()
    fresh = [w for w in wins if any(r["id"] not in known for r in w)]
    return fresh, len(wins) - len(fresh)


def mark_seen(wins: list[list[dict]], state: str) -> None:
    con = sqlite3.connect(state)
    con.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, at TEXT)")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    con.executemany("INSERT OR IGNORE INTO seen VALUES (?,?)",
                    [(r["id"], now) for w in wins for r in w])
    con.commit()
    con.close()


def windows(rows: list[dict], size: int, overlap: int) -> list[list[dict]]:
    """Consecutive intercepts of one net. A net is a frequency; nets with a handful of intercepts
    a day become one short window each."""
    out = []
    by_freq: dict[str, list[dict]] = {}
    for r in rows:
        by_freq.setdefault(r["freq"], []).append(r)
    for f, rs in by_freq.items():
        rs.sort(key=lambda r: r["ts"])
        step = max(1, size - overlap)
        for i in range(0, len(rs), step):
            chunk = rs[i:i + size]
            if chunk:
                out.append(chunk)
            if i + size >= len(rs):
                break
    return out


# ── the model ────────────────────────────────────────────────────────────────────────────────────

CODES = Path(__file__).resolve().parent / "codes.json"
# 200/300/55/11/13/202 are excluded on purpose: they are the routine chatter of every net and their
# meaning is already in the doctrine, so a hint on them would fire on nearly every intercept and
# drown the ones that matter. 99 is kept - it is rare and it means «heavy UAV activity».
CODE_SKIP = {"200", "300", "55", "11", "13", "202", "100", "000"}
CODE_KEEP_SHORT = {"99"}
# ВИНЯТОК ІЗ ПРОПУСКУ, ПАРАМИ «формація + код». Навмисне не прапорець і не правило, а список:
# 114 мсп кодує втрати як 202 (загиблий) і 303 (поранений), і це протилежне до решти корпусу,
# де 202 - буденне «прийом». Пара прив'язує виняток до ОДНІЄЇ формації, тож він фізично не може
# перетекти на 38 омсбр чи 1198 мсп, хоч би що потім дописали в глосарій.
CODE_SKIP_EXCEPT = {("114", "202"), ("114", "303")}
# Підказка для них несе попередження: таблиця каже одне, більшість мереж - інше, вирішує зміст.
CODE_WARN = {("114", "202"): "УВАГА: у більшості мереж 202 - звичайне «прийом/норма», тут "
                             "за таблицею це ВТРАТА. Вирішуй за змістом розмови.",
             ("114", "303"): "УВАГА: 303 поза 114 мсп нічого не означає; тут за таблицею це 300-й."}
RE_DIGIT_GROUP = re.compile(r"(?<!\d)\d(?:\s*[.,]?\s*\d){1,2}(?!\d)")
_codes: dict | None = None


def code_table() -> dict:
    global _codes
    if _codes is None:
        try:
            _codes = json.loads(CODES.read_text(encoding="utf-8"))["codes"]
        except Exception:  # noqa: BLE001 — no table is survivable, a crash every 3 min is not
            _codes = {}
    return _codes


def code_hints(speech_text: str, header: str) -> list[str]:
    """What the numbers in this exchange mean IN THIS UNIT.

    The nets speak in codes and the codes are unit-local: «4 1 3» is a Vampire here and something
    else two frequencies away. Without this the model reads «отработали по одному из 4 1 3, цель
    уничтожена, в квадрате 61 12» as a strike with a place and a result - which is exactly what it
    did on 29.09.2026, and put a downed drone in the red channel.

    This ONLY adds a line of text to the prompt. It never decides a level."""
    table = code_table()
    if not table:
        return []
    mine = {m.group(1) for m in re.finditer(r"(\d{1,4})\s*(?:омсбр|мсбр|мсп|мсд|абр|бр|мбр|полк)",
                                           header, re.I)}
    out, seen = [], set()
    for m in RE_DIGIT_GROUP.finditer(speech_text):
        key = re.sub(r"\D", "", m.group(0))
        excepted = {f for f in mine if (f, key) in CODE_SKIP_EXCEPT}
        if key in seen or (key in CODE_SKIP and not excepted):
            continue
        if not excepted and not (len(key) == 3 or key in CODE_KEEP_SHORT):
            continue
        entries = table.get(key)
        if not entries:
            continue
        seen.add(key)
        same = [e for e in entries if mine & set(e["formations"])]
        if same:
            line = f"{key} = " + "; ".join(dict.fromkeys(e["meaning"] for e in same))
            for f in excepted:
                warn = CODE_WARN.get((f, key))
                if warn:
                    line += f" [{warn}]"
            out.append(line)
        else:
            other = "; ".join(dict.fromkeys(e["meaning"] for e in entries))
            out.append(f"{key} = {other} (записано в ІНШИХ підрозділів, тут не підтверджено)")
    return out


def render(win: list[dict]) -> str:
    parts = []
    for r in win:
        head = f"id: {r['id']}\nчас: {r['hhmmss']}  частота: {r['freq']}"
        # Коментар - це ВСЕ, що стоїть ПЕРЕД рядком з датою ефіру. Він далеко не завжди
        # починається словом «Коментар»: «доповідь про збиття БПЛА СОУ вампір» - теж коментар,
        # і саме він пояснює перехоплення, у якому інакше видно лише числа.
        com = parse_intercept(r["text"])["comment"]
        net = [l.strip() for l in r["text"].splitlines() if "р/м" in l or "УКХ" in l]
        if com:
            head += "\nкоментар оператора (гіпотеза): " + " / ".join(com)[:400]
        if net:
            head += f"\nмережа: {net[0]}"
        hints = code_hints(r["speech"], net[0] if net else "")
        if hints:
            head += "\nкоди цієї мережі: " + " · ".join(hints)
        parts.append(head + "\n" + r["speech"][:2500])
    return "ВІКНО ПЕРЕХОПЛЕНЬ:\n\n" + "\n\n---\n\n".join(parts)


def ask(material: str, model: str, effort: str, rules: str) -> dict:
    BARE_CWD.mkdir(parents=True, exist_ok=True)
    cmd = ["claude", "-p", "--model", model, "--system-prompt", rules,
           "--output-format", "json", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--disallowedTools", *NO_TOOLS]
    if effort:
        cmd += ["--effort", effort]
    try:
        p = subprocess.run(cmd, input=material, capture_output=True, text=True,
                           timeout=600, cwd=str(BARE_CWD))
        env = json.loads(p.stdout)
        body = env.get("result", "")
        cost = env.get("total_cost_usd", 0.0)
        usage = env.get("usage", {})
        m = re.search(r"\{.*\}", body, re.S)
        data = json.loads(m.group(0)) if m else {"alerts": [], "error": "не JSON"}
        data["_cost"] = cost
        data["_usage"] = {k: usage.get(k) for k in
                          ("input_tokens", "output_tokens", "cache_read_input_tokens")}
        return data
    except Exception as exc:  # noqa: BLE001 — a failed window must not kill the run
        return {"alerts": [], "error": f"{type(exc).__name__}: {exc}", "_cost": 0.0, "_usage": {}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="")
    ap.add_argument("--minutes", type=int, default=0,
                    help="last N minutes ending now — the watcher's window")
    ap.add_argument("--hours", type=int, default=0,
                    help="last N hours ending now, instead of a calendar day")
    ap.add_argument("--model", required=True)
    ap.add_argument("--effort", default="", help="high/medium/low; empty = model default")
    ap.add_argument("--size", type=int, default=10)
    ap.add_argument("--overlap", type=int, default=2)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="first N windows only — for a smoke test")
    ap.add_argument("--only-new", default="",
                    help="sqlite стану: читати лише вікна з непрочитаним")
    ap.add_argument("--doctrine", default="v7", help="версія правил: v1 | v2 | v3")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    if not a.day and not a.hours and not a.minutes:
        sys.exit("потрібен --day, --hours або --minutes")
    label = a.day or (f"last{a.hours}h" if a.hours else f"last{a.minutes}m")
    rows = load_day(a.day, a.hours, a.minutes)
    kept, dropped = dedup(rows)
    wins = windows(kept, a.size, a.overlap)
    skipped = 0
    if a.only_new:
        wins, skipped = seen_filter(wins, a.only_new)
    if a.limit:
        wins = wins[:a.limit]
    print(f"{label}: {len(rows)} перехоплень з мовою -> {len(kept)} після дедупу "
          f"(знято {dropped}) -> {len(wins)} вікон по {a.size}"
          + (f", пропущено без нового {skipped}" if a.only_new else ""), file=sys.stderr)
    print(f"модель {a.model}{' effort ' + a.effort if a.effort else ''}, "
          f"{a.workers} паралельно, правила {a.doctrine}", file=sys.stderr)

    rules = doctrine(a.doctrine)
    t0 = time.time()
    if not wins:
        print("нового нема - нічого читати", file=sys.stderr)
    results: list[dict | None] = [None] * len(wins)
    done = 0

    def one(i: int) -> None:
        nonlocal done
        results[i] = ask(render(wins[i]), a.model, a.effort, rules)
        done += 1
        if done % 10 == 0 or done == len(wins):
            spent = sum(r.get("_cost", 0.0) for r in results if r)
            print(f"   {done}/{len(wins)} вікон, ${spent:.2f}, {time.time()-t0:.0f}с",
                  file=sys.stderr)

    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(one, range(len(wins))))

    # one alert per intercept — the highest confidence wins when windows overlap
    best: dict[str, dict] = {}
    errors = 0
    for res in results:
        if not res:
            continue
        if res.get("error"):
            errors += 1
        for al in res.get("alerts", []):
            mid = str(al.get("id", "")).strip()
            if not mid:
                continue
            c = float(al.get("confidence", 0.5) or 0.5)
            lvl = al.get("level") if al.get("level") in ("red", "yellow", "log") else "log"
            # overlapping windows see the same intercept twice; the louder verdict wins, and
            # within a level the more confident one
            rank = ({"log": 0, "yellow": 1, "red": 2}[lvl], c)
            if mid not in best or rank > best[mid]["_rank"]:
                best[mid] = {"id": mid, "class": al.get("class"), "level": lvl,
                             "why": al.get("why"), "confidence": c, "_rank": rank}

    by_id = {r["id"]: r for r in kept}
    for mid, al in best.items():
        r = by_id.get(mid)
        if r:
            al["hhmmss"], al["freq"], al["src"] = r["hhmmss"], r["freq"], r["src"]
            # ЗНАК ОПЕРАТОРА ПЕРЕВАЖАЄ ВЕРДИКТ МОДЕЛІ. Червоний знак оклику, який аналітик
            # ставить у коментарі, - це людина, що знає контекст, і вона помиляється рідко.
            # 30.09.2026 перехоплення з ❗❗❗ пішло в жовтий канал за нашою оцінкою; власник
            # вирішив не чіпати оцінку, а додати це просте тверде правило поверх неї.
            # Тільки НАШІ джерела: довіра адресована нашому операторові, не чужому.
            if r["src"] in OPERATOR_TRUSTED and OPERATOR_MARK.search(operator_comment(r["text"])):
                if al["level"] != "red":
                    al["escalated"] = "знак оператора"
                al["level"] = "red"

    if a.only_new:
        mark_seen(wins, a.only_new)
    for al in best.values():
        al.pop("_rank", None)
    red = [a for a in best.values() if a["level"] == "red"]
    yellow = [a for a in best.values() if a["level"] == "yellow"]
    cost = sum(r.get("_cost", 0.0) for r in results if r)
    OUT.mkdir(parents=True, exist_ok=True)
    name = f"{label}_{a.doctrine}_{a.model}{('_' + a.effort) if a.effort else ''}{('_' + a.tag) if a.tag else ''}.json"
    payload = {"day": label, "model": a.model, "effort": a.effort,
               "doctrine": a.doctrine, "window": a.size, "overlap": a.overlap,
               "intercepts_raw": len(rows), "intercepts_read": len(kept), "deduped": dropped,
               "windows": len(wins), "window_errors": errors,
               "cost_usd": round(cost, 4), "seconds": round(time.time() - t0),
               "red": len(red), "yellow": len(yellow),
               "log": len(best) - len(red) - len(yellow),
               "alerts": sorted(best.values(), key=lambda x: x.get("hhmmss", ""))}
    (OUT / name).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{len(red)} red + {len(yellow)} yellow + "
          f"{len(best)-len(red)-len(yellow)} log, {time.time()-t0:.0f}с, "
          f"помилок вікон {errors}", file=sys.stderr)
    print(OUT / name)


if __name__ == "__main__":
    main()
