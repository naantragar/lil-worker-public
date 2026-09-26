#!/usr/bin/env python3
"""Our own per-day callsign file, in the analyst's shape.

    python3 tools/identity/callsign_file.py --day 2026-08-05

The window is the report's: 24 hours ending at 15:00 of the named day. Output is his layout — the
frequency line, the composed net header, then `ПОЗИВНИЙ - роль` — because he will compare it with
his own file and anything unfamiliar reads as an error even when it is more precise
(`knowledge/upstream/callsign-register-tz.md`).

**His per-day file is the ACCUMULATED REGISTER printed per net.** Being on the air that day is a
STATUS on those lines, not what decides membership. That cost the first build a rewrite.

Three marks, not one (fixed 12.09.2026 after measuring the first build):

    (нове)          the archive has never heard of this man anywhere
    (інша мережа)   he IS in the archive, registered under a different net — 50 of the first
                    build's 277 `(нове)` lines were this, and the word on the page was telling the
                    analyst we had never heard of БАЛАБОЛ, whom he registered himself
    <nothing>       his own entry, untouched

**Not every new voice earns a line.** One day holds ~226 voices the archive does not know, and ~200
of them are persistent, so a "is he real" threshold thins nothing. Selection is by DIRECTION instead:
a new voice is promoted into the file when `guide_layer.py` has him leading people more than being
led. Everyone else goes to the technical file — nothing is lost, it is simply not printed at a live
analyst, who would read two hundred bare names as a refusal to do the work.

ISOLATION (`knowledge/upstream/identity-graph-tz.md`): a CONSUMER of the report's machinery. Imports
`tools/analytics2/run.py` read-only, edits nothing there, never writes into `reports_out/`.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT_DIR = REPO / "knowledge" / "upstream" / "callsigns"
REPORTS_DB = REPO / "knowledge" / "upstream" / "reports.db"
LAYER = OUT_DIR / "guide_layer.json"
BINDING = OUT_DIR / "unit_binding.json"
UNIT_RE = re.compile(r"\b(\d{1,4})\s*(омсбр|омбр|мсбр|мсп|мсд|тп|оп|бр|пдп|дшб)\b", re.I)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def archive_everywhere() -> dict[str, str]:
    """Every callsign the analyst ever registered, with the net he registered it under."""
    if not REPORTS_DB.exists():
        return {}
    con = sqlite3.connect(f"file:{REPORTS_DB}?mode=ro", uri=True)
    out: dict[str, str] = {}
    for cs, hdr in con.execute("SELECT r.callsign, n.header FROM roster r "
                               "JOIN networks n ON n.id = r.network_id"):
        if cs and cs.strip().upper() not in out:
            out[cs.strip().upper()] = (hdr or "").strip()
    return out


def bindings() -> dict[str, dict]:
    """Which formation each man's air says he belongs to (unit_binding.py). Absent is fine."""
    if not BINDING.exists():
        return {}
    return {r["callsign"]: r for r in json.loads(BINDING.read_text()).get("people", [])}


def unit_note(name: str, net_header: str, bind: dict) -> str:
    """What to print about a man's formation — and usually the answer is NOTHING.

    The block header already names the net's formation, so repeating it on every line is noise. The
    information is in the EXCEPTIONS: a man whose own air says another formation (heard here, but
    belongs elsewhere), and a man whose air cannot decide (the same word used by several people).
    """
    b = bind.get(name)
    if not b:
        return ""
    if b["state"] == "disputed":
        return f"   [підрозділ спірний: {len(b['all_units'])} формувань]"
    here = {f"{n} {u.lower()}" for n, u in UNIT_RE.findall(net_header or "")}
    if b["unit"] in here:
        return ""                      # same as the block says — nothing to add
    mark = "" if b["state"] == "bound" else " ?"
    return f"   [{b['unit']}{mark}, {int(b['share'] * 100)}% ефіру]"


def guiding() -> dict[str, dict]:
    """Direction scores, if the layer has been built. Absent is fine — then nobody is promoted."""
    if not LAYER.exists():
        return {}
    data = json.loads(LAYER.read_text())
    return {r["callsign"]: r for r in data.get("rank", []) if r["asym"] > 0}


def age_words(days: float | None, limit: int) -> str:
    if days is None:
        return f"не чути {limit}+ діб"
    if days < 1:
        return "чути сьогодні"
    if days < 2:
        return "чути вчора"
    return f"останнє чути {int(days)} діб тому"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="YYYY-MM-DD — window is 24h ending 15:00 that day")
    ap.add_argument("--days-back", type=int, default=14)
    ap.add_argument("--group", help="група корпусу (PATAGONIA_GP, Invisible Hand)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-docs", dest="docs", action="store_false",
                    help="не питати захоплені документи (за замовчуванням питаємо)")
    ap.add_argument("--no-doc-mark", dest="doc_mark", action="store_false",
                    help="не ставити «(док)» у чистовику біля ролі, взятої з документа")
    a = ap.parse_args()

    if a.group:
        import os
        os.environ["UPSTREAM_CORPUS_GROUP"] = a.group
    M = _load(REPO / "tools" / "analytics2" / "run.py", "analytics2_run")
    G = _load(HERE / "guide_layer.py", "identity_guide_layer")     # reuse the station hygiene
    D = _load(HERE / "doc_layer.py", "identity_doc_layer")          # captured documents, gated

    day = datetime.strptime(a.day, "%Y-%m-%d")
    dt_to, dt_from = f"{day:%Y-%m-%d} 15:00", f"{day - timedelta(days=1):%Y-%m-%d} 15:00"
    print(f"файл позивних на {day:%d.%m.%Y}   вікно {dt_from} - {dt_to}", file=sys.stderr)

    recs = M.fetch(dt_from, dt_to)
    if not recs:
        sys.exit(f"у вікні {dt_from} - {dt_to} немає жодного перехоплення")
    units = M.units_of_work(recs)

    freqs_of: dict[str, set] = defaultdict(set)
    rows_of: dict[str, list] = defaultdict(list)
    for u in units:
        for r in u["items"]:
            if r.get("freq"):
                freqs_of[u["net"]].add(r["freq"])
            rows_of[u["net"]].append(r)
    freqs_of = {k: sorted(v) for k, v in freqs_of.items()}
    registers = M.assign_registers(freqs_of)
    known = archive_everywhere()
    leads = guiding()
    bind = bindings()
    wide = M.fetch(f"{day - timedelta(days=a.days_back):%Y-%m-%d} 15:00", dt_to)
    print(f"перехоплень: {len(recs)}   мереж: {len(freqs_of)}   "
          f"архів знає {len(known)} позивних, зі слоя ведення {len(leads)}", file=sys.stderr)

    # No title line inside the file: the date is on the filename, and his own file starts straight
    # with a frequency row (owner, 26.09.2026 — «в файле буквально подписаны эти позывные и дата»).
    out: list[str] = []
    tech_rows: list[tuple] = []
    tech = [f"Технічний файл: всі голоси у вікні {dt_from} - {dt_to}",
            "довідка для нас, у чистовик не йде: свіжість, лічильники, джерело ролі, підрозділ", ""]
    n_arch = n_new = n_other = n_rest = n_doc = 0
    # The documents are loaded ONCE per run: 714 callsigns out of the debriefs and rosters, with
    # the analyst's own files excluded so the file cannot confirm itself.
    docs = D.load() if a.docs else {}

    # CUMULATIVE, like his own file (owner's decision 26.09.2026). His 05.08 file carries nets and
    # people who did not speak that day at all — a register, not a roll call. So the silent nets of
    # the wide window are printed too; what makes the file useful is the accumulated picture, and a
    # net that was loud all week and quiet today has not stopped existing.
    wide_freqs: dict[str, set] = defaultdict(set)
    wide_rows: dict[str, list] = defaultdict(list)
    for u in M.units_of_work(wide):
        for r in u["items"]:
            if r.get("freq"):
                wide_freqs[u["net"]].add(r["freq"])
            wide_rows[u["net"]].append(r)
    for net, fs in wide_freqs.items():
        freqs_of.setdefault(net, sorted(fs))
        rows_of.setdefault(net, [])
    registers = M.assign_registers(freqs_of)
    silent = {n for n in freqs_of if not rows_of.get(n)}
    print(f"мереж у вікні доби: {len(freqs_of) - len(silent)}, "
          f"мовчали сьогодні але є в реєстрі: {len(silent)}", file=sys.stderr)

    for net in M.order_networks({k: (rows_of[k] or wide_rows.get(k, [])) for k in freqs_of}):
        fr = freqs_of[net]
        roster, _legend, _notes = (registers.get(net) or ([], [], []))
        role_of = {c: r for c, r, *_ in roster}

        spoke: dict[str, int] = defaultdict(int)
        for r in rows_of[net]:
            for raw in r.get("stations", []) or []:
                for n in G.clean_slot(raw, M):
                    spoke[n] += 1
        ages = M.last_heard_map(wide, fr, list(role_of), dt_to)

        def from_docs(name: str, role: str) -> tuple[str, str]:
            """(role for the clean copy, note for the technical copy). The document side is
            allowed to ADD a level or a role and never to overwrite one — the gate lives in
            doc_layer.verdict(), which refuses on a unit mismatch or any contradiction."""
            if not docs:
                return role, ""
            v = D.verdict(name, role, net, docs)
            note = v["status"] + (f": {v['why']}" if v["why"] else "")
            if not v["add"]:
                return role, note
            mark = " (док)" if a.doc_mark else ""
            return ((f"{role}, {v['add']}{mark}" if role else f"{v['add']}{mark}"), note)

        # CLEAN COPY vs TECHNICAL COPY (owner's rule, 26.09.2026, after reading his own
        # `позивні 05 08 26.docx`). His file carries ONE thing per line: callsign - role. No
        # freshness, no provenance, no counters — he keeps that in his head. Ours cannot, so all of
        # it goes to the technical file and NONE of it to the clean one: "если спросят, поднимем из
        # технического". The clean copy has to be as light as his.
        if not any(role_of.values()):
            continue                        # nobody in this net has a role — no block at all                       # silent AND nobody in the register — nothing to print
        out += ["/".join(fr), net]
        for name, role in role_of.items():
            # NO BARE CALLSIGNS IN THE CLEAN COPY (owner's decision 26.09.2026). The file exists to
            # answer «хто це» in one line; a name with nothing after it answers nothing and is dead
            # weight on the page. His own file carries 27 such lines — his private "I see this man,
            # role unknown yet" note — and that job is done here by the technical copy instead.
            # Reversible: if the desk says a bare name IS useful ("so at least I know he is on this
            # net"), drop this `if` and they come back.
            role, dnote = from_docs(name, role)
            src = "архів аналітика" + (f" · документи: {dnote}" if dnote else "")
            if not role:
                tech_rows.append((name, "", unit_note(name, net, bind).strip(),
                                  "" if name in spoke else age_words(ages.get(name), a.days_back),
                                  spoke.get(name, 0), src + " · БЕЗ РОЛІ - у чистовик не йде"))
                continue
            out.append(f"{name} - {role}")
            n_arch += 1
            if "(док)" in role:
                n_doc += 1
            tech_rows.append((name, role, unit_note(name, net, bind).strip(),
                              "" if name in spoke else age_words(ages.get(name), a.days_back),
                              spoke.get(name, 0), src))

        fresh = [n for n in sorted(spoke, key=lambda n: -spoke[n]) if n not in role_of]
        promoted = [n for n in fresh if n in leads and n not in known]
        elsewhere = [n for n in fresh if n in known]
        rest = [n for n in fresh if n not in leads and n not in known]

        for n in promoted:
            g = leads[n]
            out.append(f"{n} - веде о\\с")          # role-LABEL, derived from behaviour in the air
            n_new += 1
            tech_rows.append((n, "веде о\\с", unit_note(n, net, bind).strip(), "",
                              spoke.get(n, 0),
                              f"шар ведення: веде {g['guides']}, ведуть {g['guided']}, "
                              f"{g['led']} людей, {g['days']} діб"))
        for n in elsewhere:
            n_other += 1                    # known elsewhere but roleless here
            drole, dnote = from_docs(n, "")
            if drole:                       # ...unless a document names him under the same gate
                out.append(f"{n} - {drole}")
                n_doc += 1
            tech_rows.append((n, drole, unit_note(n, net, bind).strip(), "", spoke.get(n, 0),
                              f"архів, інша мережа: {known[n][:60]}"
                              + (f" · документи: {dnote}" if dnote else "")))
        n_rest += len(rest)
        for n in rest:
            # A voice heard in the window with no role of its own: a document may still name him,
            # under the same gate as everybody else.
            drole, dnote = from_docs(n, "")
            if drole:
                out.append(f"{n} - {drole}")
                n_doc += 1
            tech_rows.append((n, drole, unit_note(n, net, bind).strip(), "", spoke.get(n, 0),
                              "чути у вікні, ролі немає" + (f" · документи: {dnote}" if dnote else "")))
        out.append("")

        tech += ["/".join(fr), net]
        for name, role, unit, age, outs, srcinfo in tech_rows:
            bits = [f"{name}"]
            if role: bits.append(f"- {role}")
            if unit: bits.append(unit)
            if age:  bits.append(f"[{age}]")
            bits.append(f"[{outs} виходів]")
            bits.append(f"[джерело: {srcinfo}]")
            tech.append("   " + " ".join(bits))
        tech.append("")
        tech_rows = []

    tech += [f"разом: {n_arch} з архіву, {n_new} з ознакою ведення, "
             f"{n_other} з іншої мережі, {n_rest} без ролі, {n_doc} з документів"]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = Path(a.out) if a.out else OUT_DIR / f"POZYVNI_{day:%d.%m.%Y}.txt"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    tech_p = path.with_name(path.stem + "_tech.txt")
    tech_p.write_text("\n".join(tech) + "\n", encoding="utf-8")

    # The clean copy also ships as .docx, in HIS styling, because that is the form the file is
    # handed over in and compared against. Measured out of `позивні 05.08.2026.docx`: Times New
    # Roman 14 pt, whole document ITALIC, the two opening rows of every block (frequencies, net
    # header) additionally BOLD, single spacing, no space after a paragraph.
    sys.path.insert(0, str(REPO / "tools"))
    from text_to_docx import write as write_docx
    styled, head = [], 2                      # first two rows of a block are the bold pair
    for line in out:
        if not line.strip():
            styled.append(("", False, False, True)); head = 2
        else:
            styled.append((line, head > 0, False, True)); head -= 1
    docx_p = path.with_suffix(".docx")
    write_docx(styled, docx_p)
    print(f"{path}\n{tech_p}\n{docx_p}", file=sys.stderr)
    print(f"з архіву {n_arch}, нових з ознакою ведення {n_new}, "
          f"з іншої мережі {n_other}, решта {n_rest}, з документів {n_doc}", file=sys.stderr)


if __name__ == "__main__":
    main()
