#!/usr/bin/env python3
"""report_pow — the captivity pass: the fourth step of the report cycle, after the cleanup.

Owner, 23.11.2026. Every time the day carried a story about captives, a re-read of the WHOLE story
changed lines that had passed all three passes (18-20.11 twice, 22.11 the «неросійських» pair).
The audit cannot see these errors by construction: it checks one line against its own intercepts,
while a captivity story is spread over hours and nets, and its hard part is who speaks, to whom and
ABOUT WHOM when nobody says a name («одного убирайте», «он уже готов работать»). A captive may be
our soldier, their own deserter, an «ally» of another unit - telling those apart is the point.

    python3 tools/upstream/report_pow.py 22.11 [--model claude-opus-5-5] [--detect-only]

1. Detection, no model, always: speech, analyst marks and report lines are searched for captivity
   words over the report window (+ the lead-in hour). Nothing found -> one line, done. One hit is
   enough to go on (owner: threshold = any mention).
2. Each hit is grown into a THREAD: the same channels three hours either side, plus every intercept
   of the window where the thread's callsigns are heard. The report lines resting on those
   intercepts, or speaking of captives, go with it. So do the standing dossiers in
   knowledge/upstream/pow/ - a story runs for days and «одного» on the third day needs the first.
3. One model call per thread with a strict contract: every captive and whose he is, every report
   line judged against the whole thread, events the report missed (owner: ADDING is allowed for
   this pass), and an updated dossier.

It only reports: POW_<day>.md next to the report, dossier drafts in knowledge/upstream/pow/drafts/.
I apply the fixes (events.json + --render-only) and promote a dossier after reading it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "tools" / "analytics2"))
sys.path.insert(0, str(REPO / "tools"))
from intercept_parse import parse_message                      # noqa: E402
import run as report                                           # noqa: E402

OUT = REPO / "knowledge" / "upstream" / "reports_out"
POW_DIR = REPO / "knowledge" / "upstream" / "pow"
DRAFTS = POW_DIR / "drafts"

# Captivity words. Wider than alerts/safety_net.CAPTURE on purpose: that one raises an alarm and must
# be precise, this one only decides whether a careful read is worth a model call.
POW = re.compile(
    r"\bполон|\bплен(?!к|ён|е?нк)|\bпленн|\bв\s+плену|\bсдал(?:ся|ись|ась)|\bсдаться|\bсдаюсь"
    r"|\bздав(?:ся|ались)|\bздатися|руки\s+(?:не\s+)?(?:связ|развяз|примота)|связанными\s+рук|зв.язан\w*\s+рук"
    r"|\bдопрос|\bдопит(?!у?в)|обменн\w*\s+фонд|на\s+обмен\b|обмін\s+полонен"
    r"|\bвзяли\s+(?:живым|живого|хохла|укропа)|\bхохла\s+взяли|\bукропа\s+взяли|полонян",
    re.I)
# NOT «обмін»/«обмен» alone: the analysts' marks say «службовий радіо обмін» on thousands of lines.
CONTEXT_HOURS = 3
MAX_INTERCEPTS = 140          # per thread; beyond that the oldest/most distant are cut

PROMPT = """Ти - аналітик, що перевіряє в розвідзвіті ВСЕ, що стосується ПОЛОНЕНИХ. Помилка тут дорожча
за будь-яку іншу: це можуть бути наші військові (СОУ), і від того, чи правильно прочитано «хто про
кого говорить», залежить, що ми про них знаємо.

Нижче - одна ІСТОРІЯ за добу: усі перехоплення її мереж за кілька годин і всі, де звучать її
позивні; рядки звіту, що на них стоять; і справи з попередніх днів (якщо є).

ПРАВИЛА
- Мовлення - перехоплення противника (РОВ). «Хохол», «укроп», «пидор/пидарас» у їхній мові = наш
  військовий (СОУ). «Союзы/союзники» = інший їхній підрозділ, не ми.
- Полонений не обов'язково наш: це може бути їхній власний дезертир чи «бегун», «союзник» з іншої
  частини, цивільний. Сторону кожного полоненого обґрунтуй цитатою. Немає опори - «н/в».
- Для кожної важливої репліки визначай: ХТО каже, КОМУ, ПРО КОГО. Займенник («он», «одного»,
  «их») розв'язуй лише тим, що реально є в розмові; не можеш - так і пиши, і рядок звіту має бути
  обережним («ім», «н/в»), а не впевненим.
- Нічого не домислюй з власних знань. Кожне твердження - з дослівною цитатою і адресою перехоплення
  (дата час частота).
- Звіт пишеться українською, мова цитат - як у перехопленні (російською), дослівно.
- Форма рядка звіту: «наказ X в\\с Y …», «переміщення в\\с …», «зазначено про …»; позивні ВЕЛИКИМИ;
  наші - «в\\с СОУ», їхні - «о\\с РОВ»/«в\\с X».

ЩО ПОВЕРНУТИ - лише JSON, без вступу:
{{
 "title": "<коротко: про що історія>",
 "key": "<латиницею, 2-3 головні позивні через дефіс, напр. BOB-CHAKI>",
 "captives": [{{"who": "<позивний/ім'я/опис>", "side": "СОУ|власний (дезертир/бегун)|союзник|цивільний|н/в",
               "status": "<де, у кого, що з ним>", "basis": "<цитата> [<адреса>]"}}],
 "lines": [{{"time": "<час рядка як у звіті>", "verdict": "вірно|змінити|н/в",
            "new_text": "<повний новий текст рядка, якщо змінити>", "why": "<коротко>",
            "quotes": ["<цитата> [<адреса>]"]}}],
 "missing": [{{"time": "<ДД.ММ.РРРР, ГГ:ХХ>", "text": "<рядок звіту>", "refs": ["<адреса>"],
              "quotes": ["<цитата> [<адреса>]"]}}],
 "speakers": [{{"ref": "<адреса>", "line": "<цитата>", "who": "<хто каже>", "to": "<кому>",
               "about": "<про кого>", "sure": true}}],
 "dossier": "<справа по історії, markdown: хто полонені і чиї, де і в кого, що вирішено, хронологія з адресами; лише встановлене>"
}}

«lines» - КОЖЕН рядок звіту з розділу РЯДКИ ЗВІТУ. «missing» - лише важливе про полонених, чого у
звіті нема (наказ щодо полоненого, його стан, переміщення, допит, обмін, загроза). «speakers» -
репліки, від яких залежить зміст (до 15).

СПРАВИ З ПОПЕРЕДНІХ ДНІВ:
{dossiers}

РЯДКИ ЗВІТУ:
{lines}

ПЕРЕХОПЛЕННЯ ІСТОРІЇ ({n} шт.):
{speech}
"""


def day_of(s: str) -> datetime:
    for fmt in ("%d.%m", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            d = datetime.strptime(s, fmt)
            return d.replace(year=datetime.now().year) if d.year == 1900 else d
        except ValueError:
            continue
    sys.exit(f"не розібрав день: {s!r}")


def window(d: datetime) -> tuple[datetime, datetime]:
    h, m = (int(x) for x in report.WINDOW_CUT.split(":"))
    hi = d.replace(hour=h, minute=m, second=0, microsecond=0)
    return hi - timedelta(days=1, minutes=report.WINDOW_LEAD_IN_MIN), hi


def air(rec: dict) -> datetime | None:
    try:
        return datetime.strptime(f"{rec.get('date')} {rec.get('time')}", "%d.%m.%Y %H:%M:%S")
    except (TypeError, ValueError):
        return None


def load_intercepts(lo: datetime, hi: datetime) -> dict[str, dict]:
    """All intercepts of the window by their stable address, one copy each (both groups merge)."""
    con = sqlite3.connect(f"file:{report.CORPUS_DB}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT group_name, coalesce(text,'') FROM messages WHERE timestamp >= ? "
                           "AND timestamp < ?", (int((lo - timedelta(hours=2)).timestamp() * 1000),
                                                  int((hi + timedelta(hours=2)).timestamp() * 1000))).fetchall()
    finally:
        con.close()
    out: dict[str, dict] = {}
    for group, body in rows:
        for rec in parse_message(body):
            t = air(rec)
            if not t or not (lo <= t < hi):
                continue
            ref = report.ref_of(rec)
            if ref not in out or len(" ".join(rec.get("speech") or [])) > len(" ".join(out[ref].get("speech") or [])):
                out[ref] = dict(rec, _group=group, _air=t)
    return out


def callsigns(rec: dict) -> set[str]:
    out = set()
    for s in rec.get("stations") or []:
        for c in re.split(r"[,;]", s):
            c = re.sub(r"\(.*?\)", "", c).strip().upper()
            if len(c) > 2 and c not in ("НВ", "Н/В") and not re.search(r"\d", c):
                out.add(c)
    return out


def marks_text(rec: dict) -> str:
    return " ".join([rec.get("comment_above") or "", rec.get("comment_below") or ""] + (rec.get("marks") or []))


def is_pow(rec: dict) -> bool:
    return bool(POW.search(" ".join(rec.get("speech") or [])) or POW.search(marks_text(rec)))


def freq_key(rec: dict) -> str:
    f = str(rec.get("freq") or "")
    return f[:6]          # 411.96 - the same channel and its x.xx50/x.xx52 siblings


def threads(recs: dict[str, dict], events: list[dict]) -> list[dict]:
    seeds = {r for r, x in recs.items() if is_pow(x)}
    for e in events:
        if POW.search(e.get("text") or ""):
            seeds |= {r for r in e.get("_src_ref") or [] if r in recs}
    # union seeds sharing a channel family or a callsign
    parent = {s: s for s in seeds}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    sl = sorted(seeds)
    for i, a in enumerate(sl):
        for b in sl[i + 1:]:
            if freq_key(recs[a]) == freq_key(recs[b]) or (callsigns(recs[a]) & callsigns(recs[b])):
                parent[find(a)] = find(b)
    groups: dict[str, set] = {}
    for s in seeds:
        groups.setdefault(find(s), set()).add(s)
    out = []
    for g in groups.values():
        fams = {freq_key(recs[s]) for s in g}
        names = set().union(*(callsigns(recs[s]) for s in g))
        t0 = min(recs[s]["_air"] for s in g) - timedelta(hours=CONTEXT_HOURS)
        t1 = max(recs[s]["_air"] for s in g) + timedelta(hours=CONTEXT_HOURS)
        ctx = set(g)
        for r, x in recs.items():
            if freq_key(x) in fams and t0 <= x["_air"] <= t1:
                ctx.add(r)
            elif names & callsigns(x):
                ctx.add(r)
        if len(ctx) > MAX_INTERCEPTS:                 # keep the seeds and the nearest to them
            mid = [recs[s]["_air"] for s in g]
            ctx = set(sorted(ctx, key=lambda r: min(abs((recs[r]["_air"] - m).total_seconds()) for m in mid))
                      [:MAX_INTERCEPTS]) | g
        lines = [e for e in events if (set(e.get("_src_ref") or []) & ctx)
                 and (POW.search(e.get("text") or "") or set(e.get("_src_ref") or []) & g
                      or names & set(re.findall(r"[А-ЯЁІЇЄҐ][А-ЯЁІЇЄҐ\-]{2,}", e.get("text") or "")))]
        out.append({"seeds": g, "ctx": ctx, "names": names, "lines": lines})
    out.sort(key=lambda t: -len(t["seeds"]))
    return out


def render_speech(recs: dict, refs) -> str:
    out = []
    for r in sorted(refs, key=lambda r: recs[r]["_air"]):
        x = recs[r]
        out.append(f"--- [{r}] · {x.get('network') or x.get('net') or ''} · станції: {', '.join(x.get('stations') or [])}")
        m = marks_text(x).strip()
        if m:
            out.append(f"    мітка аналітика: {m[:300]}")
        for l in x.get("speech") or []:
            out.append(f"    {l}")
    return "\n".join(out)


def dossiers_for(names: set[str]) -> str:
    if not POW_DIR.exists():
        return "(немає)"
    out = []
    for p in sorted(POW_DIR.glob("*.md")):
        t = p.read_text(encoding="utf-8")
        if any(n in t.upper() for n in names):
            out.append(f"### {p.name}\n{t.strip()}")
    return "\n\n".join(out) or "(немає)"


def ask(model: str, prompt: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    try:
        p = subprocess.run(["claude", "-p", "--model", model, "--effort", "medium"], input=prompt,
                           capture_output=True, text=True, timeout=1500, env=env)
        return (p.stdout or p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return ""


def parse_json(s: str) -> dict | None:
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="captivity pass over a finished report")
    ap.add_argument("day")
    ap.add_argument("--events")
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--detect-only", action="store_true", help="no model: show the threads and stop")
    a = ap.parse_args()

    d = day_of(a.day)
    tag = f"{d:%d.%m}"
    lo, hi = window(d)
    ev_path = Path(a.events) if a.events else OUT / f"ZVIT_{tag}_events.json"
    if not ev_path.exists():
        print(f"no report for {tag} yet ({ev_path.name}) - step 4 runs after run.py --day", file=sys.stderr)
        return 2
    events = [e for e in json.loads(ev_path.read_text(encoding="utf-8")) if not e.get("_carryover_dropped")]
    recs = load_intercepts(lo, hi)
    th = threads(recs, events)

    head = [f"# крок 4. полон - ZVIT_{tag}", "",
            f"вікно {lo:%d.%m %H:%M} - {hi:%d.%m %H:%M}, перехоплень {len(recs)}, історій про полон: {len(th)}", ""]
    if not th:
        head.append("Полону за добу нема - ні в мові, ні в мітках, ні в рядках звіту.")
        (OUT / f"POW_{tag}.md").write_text("\n".join(head) + "\n", encoding="utf-8")
        print("\n".join(head))
        return 0
    for i, t in enumerate(th, 1):
        head.append(f"- історія {i}: зачіпок {len(t['seeds'])}, перехоплень у контексті {len(t['ctx'])}, "
                    f"рядків звіту {len(t['lines'])}; позивні: {', '.join(sorted(t['names']))[:200]}")
        for s in sorted(t["seeds"], key=lambda r: recs[r]["_air"])[:12]:
            hit = POW.search(" ".join(recs[s].get("speech") or [])) or POW.search(marks_text(recs[s]))
            head.append(f"    {s} - " + (f"«{hit.group(0)}»" if hit else "з рядка звіту"))
    print("\n".join(head))
    if a.detect_only:
        return 0

    body = []
    DRAFTS.mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(th, 1):
        lines = "\n".join(f"- {e.get('time')} | {e.get('text')} | {', '.join(e.get('_src_ref') or [])}"
                          for e in t["lines"]) or "(жодного рядка)"
        prompt = PROMPT.format(dossiers=dossiers_for(t["names"]), lines=lines, n=len(t["ctx"]),
                               speech=render_speech(recs, t["ctx"]))
        print(f"історія {i}: {len(prompt)} знаків до моделі", file=sys.stderr)
        res = parse_json(ask(a.model, prompt))
        if not res:
            body += [f"## історія {i}", "", "(модель не повернула JSON - перевірити вручну)", ""]
            continue
        key = re.sub(r"[^A-Za-z0-9\-]", "", str(res.get("key") or f"thread{i}"))[:40] or f"thread{i}"
        body += [f"## історія {i}: {res.get('title', '')}  [{key}]", "", "### полонені"]
        for c in res.get("captives") or []:
            body.append(f"- {c.get('who')} - **{c.get('side')}** - {c.get('status')}  \n  {c.get('basis')}")
        body += ["", "### рядки звіту"]
        for ln in res.get("lines") or []:
            body.append(f"- [{ln.get('verdict')}] {ln.get('time')} - {ln.get('why', '')}")
            if ln.get("new_text"):
                body.append(f"  НОВИЙ: {ln['new_text']}")
            for q in ln.get("quotes") or []:
                body.append(f"  > {q}")
        body += ["", "### пропущене (можна додавати)"]
        for m in res.get("missing") or []:
            body.append(f"- {m.get('time')} {m.get('text')}  [{', '.join(m.get('refs') or [])}]")
            for q in m.get("quotes") or []:
                body.append(f"  > {q}")
        body += ["", "### хто кому про кого"]
        for s in res.get("speakers") or []:
            body.append(f"- [{s.get('ref')}] «{s.get('line')}» - {s.get('who')} → {s.get('to')}, "
                        f"про {s.get('about')}{'' if s.get('sure') else ' (НЕПЕВНО)'}")
        body.append("")
        (DRAFTS / f"{tag}_{key}.md").write_text(str(res.get("dossier") or "").strip() + "\n", encoding="utf-8")
        (DRAFTS / f"{tag}_{key}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    dest = OUT / f"POW_{tag}.md"
    dest.write_text("\n".join(head + [""] + body) + "\n", encoding="utf-8")
    print(f"\nзбережено: {dest.relative_to(REPO)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
