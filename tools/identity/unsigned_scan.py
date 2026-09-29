#!/usr/bin/env python3
"""The unsigned-net pipeline, one command. Stages A -> B -> C.

    python3 tools/identity/unsigned_scan.py --day 2026-09-26
    python3 tools/identity/unsigned_scan.py --from 2026-09-20 --to 2026-09-27 --model
    python3 tools/identity/unsigned_scan.py --all --model --out knowledge/upstream/unsigned/

An unsigned net is traffic whose owner the header does not name. This walks a day (or a range) of
it and asks one question per intercept: **is there anything here that ties it to a formation we
already know?** Three signals, cheapest first:

    A1  frequency  - this exact frequency is signed elsewhere in our corpus
    A2  callsign   - a voice here is heard on signed nets, ideally on ONE formation only
    A3  code word  - the speech uses vocabulary that, measured, only one formation uses

Stage A is pure code and runs over everything. Stage B (`--model`) runs ONLY over what stage A
raised - a day is ~40 intercepts and stage A typically leaves single digits - and its job is to
throw things out: it reads the exchange and says whether the match is a real use of the code or an
accident («взять матеріал» tripping the code «Материя»). Stage C prints what survives as raw
intercepts, ready to forward.

**Nothing here attributes anything.** Every line is «this points at X because Y» with the counts
that make Y checkable. The reading is done by people downstream - the owner's standing rule.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

from code_sieve import (Codes, corpus, formation, speech, verdict,  # noqa: E402
                        calibrate, FREQ_LINE, callsigns_of)

sys.path.insert(0, str(REPO / "tools" / "glossary"))
import beta  # noqa: E402  — the holding pen for candidate code words
import unsigned_db  # noqa: E402  — findings base; writes to its OWN file and nothing else

CACHE = REPO / "knowledge" / "upstream" / "glossary" / "code_power.json"
BARE_CWD = Path("/tmp/krevetka-unsigned-model")
NO_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "Task"]

SYSTEM = """Ти читаєш ОДНЕ радіоперехоплення і виписуєш із нього факти. Нічого не привʼязуєш до
підрозділів і нічого не переказуєш - це робить не модель, а наш індекс по корпусу.

Що виписати:

1. ПОЗИВНІ, які реально звучать у мові. Саме вони головні: оператор часто не вписує їх у шапку.
   Для кожного - як ужито: "звертання" (його кличуть), "представлення" ("я Луч"), "згадка".
   Позивний у мові пишуть із малої або з великої - бери як почулося, у називному відмінку.
   НЕ вигадуй: якщо це звичайне слово, а не імʼя - не пиши.
2. ХТО ВЕДЕ людину по місцевості - голос, що каже «лівіше», «проходь наскрізь», «починай рух»,
   «прийми вправо». Це для нас найцінніше. Дай позивний ведучого (або null) і фразу-доказ.
3. ПІДРОЗДІЛИ, названі вголос: «третя рота», «шістдесят друга», «перший батальйон».
4. КОДОВІ СЛОВА - і тут ОБЕРЕЖНО, бо це місце, де найлегше вигадати.
   ПОРОЖНІЙ СПИСОК - НОРМАЛЬНА І НАЙЧАСТІША ВІДПОВІДЬ. У більшості перехоплень кодів немає
   взагалі: люди говорять звичайними словами про звичайні речі. Не шукай код, якщо його нема.
   - слово мусить СТОЯТИ В ТЕКСТІ дослівно. Не виводь слово з іншого слова: «колесо спустило»
     не дає «велосипед». Якщо слова в мові нема - його нема, крапка.
   - побутове прочитання має перевагу. «Посилочка» - це посилка, «повербанк» - повербанк,
     «неразрыв» - те, що не розірвалось, «контроль неба» - дивіться за небом. Усе це перевірено
     людиною і НЕ є кодами.
   - слово вважається кодом тільки тоді, коли побутове прочитання у ЦЬОМУ реченні не працює:
     на нього йдуть, від нього міряють напрямок, ним називають те, чим воно бути не може.
   Краще пропустити код, ніж вигадати: вигадане слово забирає час людини, а пропущене
   повернеться завтра з наступним перехопленням.
5. НАЗВИ МІСЦЕВОСТІ та орієнтири.
6. ДЛЯ ЧОГО ця мережа: "командна" / "БпЛА" / "тил" / "евакуація" / "інше" / "невідомо".

Якщо дали список ПІДКАЗОК на перевірку - для кожної скажи, чи це справжнє вживання.
Типові хибні спрацювання: зачіп основою («взяти матеріал» не є кодом «Материя»); побутове
значення; число всередині іншого числа. АЛЕ: слово про рух чи місцевість в устах того, хто ВЕДЕ
(«далі в океан» після «лівіше візьми») - це справжнє вживання коду, поріг сумніву тут низький.
Кодове слово, ужите як звертання («Волгоград, прийом»), теж вважається справжнім.

Відповідай ЛИШЕ одним JSON-обʼєктом:
{"callsigns":[{"name":"","use":"звертання|представлення|згадка","quote":""}],
 "leading":{"who":null,"quote":""},
 "units":[],
 "codes":[{"word":"","why":""}],
 "places":[],
 "purpose":"",
 "checks":[{"hint":"","real":true,"why":""}]}"""


# ── stage A ──────────────────────────────────────────────────────────────────────────────────────

def build_index(exclude: str) -> tuple[dict, dict, dict]:
    """From the SIGNED corpus: frequency -> formations, callsign -> formations."""
    by_freq = collections.defaultdict(collections.Counter)
    by_call = collections.defaultdict(collections.Counter)
    for ts, g, net, t, _mid in corpus():
        if g == exclude:
            continue
        f = formation(net)
        if not f:
            continue
        lines = [l.strip() for l in t.splitlines()]
        for i, l in enumerate(lines):
            m = FREQ_LINE.match(l)
            if not m:
                continue
            by_freq[round(float(m.group(1).replace(",", ".")), 4)][f] += 1
            for x in callsigns_of(lines, i):
                by_call[x][f] += 1
            break
    return by_freq, by_call, {}


def code_power(codes: Codes, exclude: str, refresh: bool) -> dict[int, dict]:
    """Calibration is a full sweep of the corpus, so it is cached. The cache is keyed by the code
    table's own size — a new code table invalidates it, a new day of traffic does not (a day moves
    these counts by a fraction of a percent and never flips a verdict)."""
    key = f"{len(codes.rows)}|{exclude}"
    if CACHE.exists() and not refresh:
        try:
            blob = json.loads(CACHE.read_text(encoding="utf-8"))
            if blob.get("key") == key:
                return {int(k): v for k, v in blob["stats"].items()}
        except (json.JSONDecodeError, KeyError):
            pass
    print("міряю силу кодів на підписаному ефірі (раз на зміну таблиці кодів)...", file=sys.stderr)
    stats = calibrate(codes, exclude)
    CACHE.write_text(json.dumps({"key": key, "stats": stats}, ensure_ascii=False), encoding="utf-8")
    return stats


def scan(group: str, rows: list, codes: Codes, stats: dict,
         by_freq: dict, by_call: dict) -> list[dict]:
    out = []
    for ts, g, net, t, mid in rows:
        lines = [l.strip() for l in t.splitlines()]
        freq = None
        calls: list[str] = []
        for i, l in enumerate(lines):
            m = FREQ_LINE.match(l)
            if m:
                freq = round(float(m.group(1).replace(",", ".")), 4)
                calls = callsigns_of(lines, i)
                break
        ev = []
        if freq is not None and freq in by_freq:
            for f, c in by_freq[freq].most_common(2):
                ev.append({"kind": "частота", "points_to": f, "weight": 3 if c >= 20 else 2,
                           "detail": f"частота {freq} підписана як {f}",
                           "counts": f"{c} підписаних перехоплень"})
        for name in calls:
            if name not in by_call:
                continue
            forms = by_call[name]
            if len(forms) == 1:
                f, c = forms.most_common(1)[0]
                ev.append({"kind": "позивний", "points_to": f, "weight": 3 if c >= 20 else 2,
                           "detail": f"позивний {name} звучить ЛИШЕ на {f}",
                           "counts": f"{c} згадок", "probe": name})
            else:
                top = ", ".join(f"{f} ({c})" for f, c in forms.most_common(3))
                ev.append({"kind": "позивний", "points_to": forms.most_common(1)[0][0], "weight": 1,
                           "detail": f"позивний {name} звучить у кількох: {top}",
                           "counts": "", "probe": name})
        sp = speech(t)
        for i in codes.find(sp) if sp else set():
            r = codes.rows[i]
            if not r["form"]:
                continue
            v = verdict(stats[i])
            if v == "ШУМ":
                continue
            ev.append({"kind": "код", "points_to": r["form"],
                       "weight": 4 if v == "СИЛЬНИЙ" else 2 if v == "СЕРЕДНІЙ" else 1,
                       "detail": f"«{r['term']}» = {(r['meaning'] or '')[:48]} ({v})",
                       "counts": f"свої {stats[i]['own']}, чужі {stats[i]['other']}",
                       "probe": str(r["term"])})
        per_form = collections.Counter()
        for e in ev:
            per_form[e["points_to"]] += e["weight"]
        out.append({"ts": ts, "net": net, "text": t, "speech": sp, "freq": freq,
                    "msg_id": mid, "header": calls, "evidence": ev,
                    "score": max(per_form.values()) if per_form else 0,
                    "points_to": per_form.most_common(1)[0][0] if per_form else "",
                    "spread": len(per_form)})
    out.sort(key=lambda r: (-r["score"], r["ts"]))
    return out


# ── stage B: one extracting pass per intercept ───────────────────────────────────────────────────

def extract(sp: str, hints: list[str], model: str, effort: str,
            dismissed: str = "") -> dict:
    BARE_CWD.mkdir(parents=True, exist_ok=True)
    material = "МОВА З ПЕРЕХОПЛЕННЯ:\n" + sp[:5000]
    if dismissed:
        material += ("\n\nЦІ СЛОВА ВЖЕ РОЗІБРАНІ ЛЮДИНОЮ - НЕ ПРОПОНУЙ ЇХ ЯК КОДИ:\n"
                     + dismissed)
    if hints:
        material += "\n\nПІДКАЗКИ НА ПЕРЕВІРКУ:\n" + "\n".join(f"- {h}" for h in hints)
    cmd = ["claude", "-p", "--model", model, "--system-prompt", SYSTEM,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--disallowedTools", *NO_TOOLS, "--effort", effort]
    try:
        p = subprocess.run(cmd, input=material, capture_output=True, text=True,
                           timeout=300, cwd=str(BARE_CWD))
        m = re.search(r"\{.*\}", p.stdout, re.S)
        return json.loads(m.group(0)) if m else {"error": "не JSON"}
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"error": type(exc).__name__}


def enrich(found: list[dict], by_call: dict, codes: Codes, model: str,
           effort: str, workers: int) -> None:
    """Read every intercept once. The model EXTRACTS what is in the text; the matching against our
    corpus stays here, in code - the model does not know our corpus and must not be asked to.

    Half of this group's intercepts carry no callsign in the header at all (606 of 1157 measured
    27.09.2026), and 329 of those have a name we already know spoken in the exchange. That is the
    hole this pass fills."""
    work = [r for r in found if r["speech"]]
    print(f"стадія B: читаю {len(work)} перехоплень моделлю ({workers} паралельно)...",
          file=sys.stderr)
    done = 0

    dismissed = ", ".join(sorted(beta.blacklist())) or ""
    if dismissed:
        print(f"   (не пропонувати: {len(beta.blacklist())} вже розібраних слів)", file=sys.stderr)

    def one(r: dict) -> None:
        hints = [e["detail"] for e in r["evidence"] if e.get("probe")]
        r["x"] = extract(r["speech"], hints, model, effort, dismissed)

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in cf.as_completed([ex.submit(one, r) for r in work]):
            done += 1
            print(f"   {done}/{len(work)}", end="\r", file=sys.stderr)
    print("", file=sys.stderr)

    known_codes = {str(r["term"]).lower() for r in codes.rows}
    dismissed_set = beta.blacklist()
    for r in found:
        x = r.get("x") or {}
        if x.get("error"):
            continue
        # verification: a hint the model calls false is dropped
        killed = 0
        checks = {str(c.get("hint", ""))[:60]: c for c in x.get("checks") or []}
        keep = []
        for e in r["evidence"]:
            c = checks.get(e["detail"][:60])
            if c and c.get("real") is False:
                killed += 1
                continue
            if c and c.get("why"):
                e["check"] = c
            keep.append(e)
        r["evidence"], r["killed"] = keep, killed

        # callsigns HEARD but not written in the header
        in_header = {n.upper() for n in r["header"]}
        for cs in x.get("callsigns") or []:
            if not isinstance(cs, dict):
                continue
            name = re.sub(r"[^А-ЯЁЇІЄҐA-Z0-9 \-]", "", str(cs.get("name") or "").upper()).strip()
            if not name or name in in_header or name not in by_call:
                continue
            forms = by_call[name]
            f, c = forms.most_common(1)[0]
            sole = len(forms) == 1
            r["evidence"].append({
                "kind": "позивний з МОВИ", "points_to": f,
                "weight": (3 if c >= 20 else 2) if sole else 1,
                "detail": f"«{name}» звучить у мові ({cs.get('use') or 'згадка'}), у шапці його нема"
                          + (f"; на підписаних - ЛИШЕ {f}" if sole
                             else "; на підписаних - " + ", ".join(f"{a} ({b})"
                                                                   for a, b in forms.most_common(3))),
                "counts": f"{c} згадок", "quote": cs.get("quote") or ""})
        # a unit named out loud is its own evidence, even without a formation number.
        # The model sometimes answers with an object instead of a string ({"unit": "..."}), so the
        # value is flattened rather than str()'d - otherwise the report prints a raw dict.
        for u in x.get("units") or []:
            if isinstance(u, dict):
                u = next((str(v) for v in u.values() if v), "")
            u = re.sub(r"\s+", " ", str(u)).strip()
            if not u:
                continue
            # «пятьдесят третья» is a real thing to record, but it is not a FORMATION: it cannot
            # open a bucket of its own, or the tally grows entries like «2 номер». Only a parsed
            # formation votes; anything else is printed as a note and carries no weight.
            f = formation(u)
            r["evidence"].append({"kind": "підрозділ вголос", "points_to": f,
                                  "weight": 2 if f else 0,
                                  "detail": f"у мові назвали «{u}»"
                                            + ("" if f else " (номер без зʼєднання - лише нотатка)"),
                                  "counts": ""})
        # Code words the model saw that our tables do not have. They go into the BETA table and
        # NOWHERE else: three of the first four candidates the owner looked at were the model being
        # confidently wrong («малых» is two MINUTES, not small drones), and no calibration can catch
        # that — the word does not occur in our signed tables at all. Only a person who has listened
        # knows. They reach the code tables through his buttons, not through this script.
        # The word must be IN the speech, verbatim. «Велосипед» was reported from «колесо
        # спустило» - derived, not heard - and only a human noticed. A model asked to find codes
        # will find codes, so the mechanical check has to stand behind the instruction.
        low = r["speech"].lower()
        r["new_codes"] = []
        for c in x.get("codes") or []:
            if not isinstance(c, dict):
                continue
            w = " ".join(str(c.get("word", "")).lower().split())
            if not w or w in known_codes or w in dismissed_set:
                continue
            first = w.split()[0]
            if first[:max(4, len(first) - 2)] not in low:
                c["invented"] = True          # counted, shown, never stored
                r.setdefault("invented", []).append(w)
                continue
            r["new_codes"].append(c)
        for c in r["new_codes"]:
            beta.add(c.get("word"), c.get("why") or "",
                     {"when": datetime.fromtimestamp(r["ts"] / 1000).strftime("%d.%m %H:%M"),
                      "freq": r["freq"],
                      "quote": next((l for l in r["speech"].splitlines()
                                     if str(c.get("word", "")).lower()[:6] in l.lower()), "")})
        per = collections.Counter()
        for e in r["evidence"]:
            if e["points_to"] and e["weight"]:
                per[e["points_to"]] += e["weight"]
        r["score"] = max(per.values()) if per else 0
        r["points_to"] = per.most_common(1)[0][0] if per else ""
        r["spread"] = len(per)


# ── stage C ──────────────────────────────────────────────────────────────────────────────────────

def report(found: list[dict], group: str, span: str, total: int, checked: bool) -> str:
    live = [r for r in found if r["score"] > 0]
    killed = sum(r.get("killed", 0) for r in found)
    heard = sum(1 for r in live for e in r["evidence"] if e["kind"] == "позивний з МОВИ")
    out = [f"НЕПІДПИСАНІ МЕРЕЖІ — «{group}», {span}",
           f"перехоплень: {total} · із зачіпкою: {len(live)}"]
    if checked:
        out.append(f"модель прочитала всі, відкинула підказок: {killed}, "
                   f"дістала позивних із мови: {heard}")
    per = collections.Counter(r["points_to"] for r in live)
    if per:
        out.append("вказують на: " + ", ".join(f"{f} — {c}" for f, c in per.most_common()))

    leaders = []
    for r in found:
        lead = (r.get("x") or {}).get("leading") or {}
        if isinstance(lead, list):
            lead = lead[0] if lead and isinstance(lead[0], dict) else {}
        w = lead.get("who")
        if isinstance(w, dict):
            w = next((v for v in w.values() if v), None)
        if w and str(w).strip().lower() not in ("null", "none", "невідомо"):
            leaders.append((str(w)[:40], str(lead.get("quote") or "")[:90],
                            datetime.fromtimestamp(r["ts"] / 1000).strftime("%d.%m %H:%M"),
                            r["freq"], r["points_to"]))
    if leaders:
        out += ["", f"ХТО ВОДИТЬ ЛЮДЕЙ ПО МІСЦЕВОСТІ — {len(leaders)} випадк(ів). "
                    "Це найцінніший клас, дивитись першим:"]
        for who, q, when, fr, pt in leaders:
            out.append(f"   {who:<14} {when} · {fr}" + (f" · вказує на {pt}" if pt else ""))
            if q:
                out.append(f"      «{q}»")

    newc = collections.Counter()
    for r in found:
        for c in r.get("new_codes") or []:
            w = str(c.get("word", "")).strip()
            if w:
                newc[(w.lower(), (c.get("why") or "")[:60])] += 1
    if newc:
        inv = sum(len(r.get("invented") or []) for r in found)
        out += ["", f"НОВІ СЛОВА В БЕТА-ТАБЛИЦІ — {len(newc)} шт."
                + (f" (ще {inv} відкинуто: слова нема в мові дослівно)" if inv else "") + ". Це ЗДОГАДКИ МОДЕЛІ, не коди.",
                "У таблицю кодів вони не потрапляють. Розібрати кнопками: "
                "python3 tools/glossary/beta.py survey"]
        for (w, why), n in newc.most_common(15):
            out.append(f"   «{w}» ×{n} — {why}")

    out += ["", "ЖОДНА З ЦИХ ПРИВʼЯЗОК НЕ Є ВИСНОВКОМ. Це підказка і те, на чому вона стоїть."]
    for r in sorted(live, key=lambda r: (-r["score"], r["ts"])):
        when = datetime.fromtimestamp(r["ts"] / 1000).strftime("%d.%m.%Y, %H:%M:%S")
        out += ["", "=" * 96,
                f"{when} · {r['freq']} · {r['net'][:70]}",
                f"ВКАЗУЄ НА: {r['points_to']}   (вага {r['score']}"
                + (f", розбіжність: {r['spread']} підрозділи" if r["spread"] > 1 else "") + ")"]
        x = r.get("x") or {}
        if x.get("purpose") and x["purpose"] not in ("невідомо", ""):
            out.append(f"   мережа схожа на: {x['purpose']}")
        for e in r["evidence"]:
            line = f"   [{e['kind']}] {e['detail']}"
            if e["counts"]:
                line += f" — {e['counts']}"
            out.append(line)
            if e.get("quote"):
                out.append(f"       «{e['quote'][:110]}»")
            if e.get("check", {}).get("why"):
                out.append(f"       перевірка: {e['check']['why'][:110]}")
        out.append("")
        out += ["   " + l for l in r["text"].splitlines()]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="AllInARow")
    ap.add_argument("--day")
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--model", action="store_true", help="run stage B (reads every intercept)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--model-id", default="claude-sonnet-5")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--recalibrate", action="store_true")
    ap.add_argument("--out", help="directory to write the report into")
    a = ap.parse_args()

    rows = corpus(a.group)
    if a.day:
        d = datetime.strptime(a.day, "%Y-%m-%d").date()
        rows = [r for r in rows if datetime.fromtimestamp(r[0] / 1000).date() == d]
        span = a.day
    elif a.frm or a.to:
        lo = datetime.strptime(a.frm, "%Y-%m-%d") if a.frm else datetime(2000, 1, 1)
        hi = (datetime.strptime(a.to, "%Y-%m-%d") + timedelta(days=1)) if a.to else datetime(2100, 1, 1)
        rows = [r for r in rows if lo <= datetime.fromtimestamp(r[0] / 1000) < hi]
        span = f"{a.frm or '…'} — {a.to or '…'}"
    elif a.all:
        span = "весь період"
    else:
        sys.exit("вкажи --day, --from/--to або --all")
    if not rows:
        sys.exit(f"у групі «{a.group}» немає перехоплень за {span}")

    codes = Codes()
    stats = code_power(codes, a.group, a.recalibrate)
    by_freq, by_call, _ = build_index(a.group)
    found = scan(a.group, rows, codes, stats, by_freq, by_call)
    if a.model and found:
        enrich(found, by_call, codes, a.model_id, a.effort, a.workers)
    # Findings go into their OWN base — never into the glossary, never into the corpus. An
    # unsigned net has a future: one day the analysts sign that frequency and its traffic joins the
    # signed corpus, where the register and the dossier already know what to do. Until then what we
    # work out about it is a guess about an unknown and is kept apart on purpose (owner, 28.09.2026).
    kept = unsigned_db.ingest(found, run_label=span)
    print("збережено у базу непідписаних: "
          + (", ".join(f"{k} {v}" for k, v in kept.items()) or "нічого нового"), file=sys.stderr)

    text = report(found, a.group, span, len(rows), a.model)
    print(text)
    if a.out:
        p = Path(a.out)
        p.mkdir(parents=True, exist_ok=True)
        name = f"NEPIDPYSANI_{(a.day or span).replace(' ', '')}.txt"
        (p / name).write_text(text, encoding="utf-8")
        print(f"\n[файл: {p / name}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
