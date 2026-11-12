#!/usr/bin/env python3
"""«Чий це код?» — answered by PEOPLE, not by the frequency a transmission was heard on.

    python3 tools/identity/code_owner.py build                     # parse the corpus into a people graph
    python3 tools/identity/code_owner.py code "3\\s*5\\s*8"        # who uses a code, by community
    python3 tools/identity/code_owner.py who ОВИ ТИГР ЛЕОНИД       # whom these men really talk to
    python3 tools/identity/code_owner.py community БАЛТИКА         # the community a man sits in

Standalone on purpose (owner, 05.11.2026): it feeds nothing — not the report, not the register.

Why it exists. On 05.11 we tried to say whose code «358» is and leaned on the unit printed in the net
header. The analyst answered: «на наших частотах виходять другі підрозділи … до частот не
прив'язуватися сильно». A header names whoever usually works a frequency; men of another unit come
up on it all the time. So this tool never asks «on which frequency was it said». It asks:

  1. who talks to whom — «Х, я Y» and roll calls «Х, Х, Y 3 5 8» give a directed pair caller → called;
     station lists of the same intercept give a weaker co-presence pair;
  2. which men form a dense group (weighted label propagation over that graph) — a group with no name;
  3. where a code is NATIVE: many different speakers of one group, on many days, as routine;
     and who CARRIES it into other groups — those carriers' own links show where they came from.

Naming a group is a separate, weaker step: the unit labels of the headers its members speak under are
printed beside it, marked as labels, never used to build it.

Known limits, printed with every answer: one callsign can be several men (ЧИЛИ, МЕТИС) and gets glued
into one node; garbled transcripts split one man into several; phone / messenger traffic is invisible.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
from posady import CORPUS, GROUPS, parse          # noqa: E402  — one intercept parser for the project
from roles_db import formation                     # noqa: E402

CACHE = REPO / "knowledge" / "upstream" / "identity" / "people_graph.db"

# Capitalised words that open a radio phrase and are not callsigns.
STOP = set("""
ДА НЕТ ПРИЕМ ПРИЁМ ПРИНЯЛ ПРИНЯЛИ КАК КАК-ТО Я ТЫ МЫ ВЫ ОН ОНА ОНИ ВСЕ ВСЁ ТАМ ТУТ ЗДЕСЬ ЧТО ЭТО ЭТОТ
ДАВАЙ ДАВАЙТЕ ПОНЯЛ ПОНЯЛИ СЕЙЧАС ХОРОШО ТАК ВОТ НУ ИЛИ ЕСЛИ ГДЕ КОГДА ЕЩЕ ЕЩЁ УЖЕ ПОКА СНОВА ПЛЮС
МИНУС ВНИМАНИЕ СЛЫШУ СЛЫШИШЬ СМОТРИ ИДИ ДАЙ ВЫЙДИ ВЫХОДИ ПОВТОРИ ПОВТОРИТЕ КТО НА НЕ НИ ТОЖЕ ОТ ДО
ПО ЗА ИЗ БЕЗ ПРИ ПРО ДЛЯ ЖДУ ЖДИ СТОЙ СТОП ПРОДОЛЖАЙ НАЧИНАЙ ДВИГАЙСЯ ДВИЖЕНИЕ ОБСТАНОВКА РАЗБИРАЮ
ПРИНИМАЮ ДОКЛАДЫВАЮ ДОКЛАДЫВАЙ ПЕРЕДАЙ ПЕРЕДАЮ ВОПРОС ОТВЕТ НАХУЙ БЛЯДЬ БЛЯ СУКА ПИЗДЕЦ ЕБАТЬ
ПРАВИЛЬНО ПОЖАЛУЙСТА СПАСИБО МОЛОДЕЦ ТОЧНО КОНЕЧНО ЯСНО ВСЕГДА ТИХО ЧИСТО НОРМАЛЬНО БЫСТРО ОДНУ
ОДИН ДВА ТРИ НОЛЬ МАЛУЮ МАЛЫХ БОЛЬШУЮ СВЯЗЬ СВЯЗИ КАНАЛ ЛИЧКУ ЛИЧКА СИНЮЮ МНЕ ТЕБЕ ЕГО ЕЁ ЕЕ ИХ НАС
ВАС НАМ ВАМ ТЕБЯ МЕНЯ ЗАЧЕМ ПОЧЕМУ СКОЛЬКО ЧЕРЕЗ ПОСЛЕ ПЕРЕД ВЕЗДЕ ПОНЯТНО ПОНЯТЬ ОЖИДАЙ ОТБОЙ
КОНЕЦ НАЧАЛО НЕБО ПТИЧКА ПТИЧКИ БОРТ ЗАЯВКА ТОЧКА ТОЧКУ ГРУППА ГРУППЫ МЕСТО ПОЗИЦИЯ ПОЗИЦИИ
""".split())
NAME = r"[А-ЯЁІЇЄҐ][а-яёіїєґ'’-]{2,}"
# «Х, Х, я Y …» / «Х, Х, Y …» — Y calls X. Both sides captured raw, folded later.
CALL_YA = re.compile(rf"^\W*({NAME})(?:[\s,.]+\1)*[\s,.]+(?:снова,?\s+)?я\s+({NAME})", re.I)
CALL_REP = re.compile(rf"^\W*({NAME})(?:[\s,.]+\1)+[\s,.]+({NAME})\b", re.I)
YA = re.compile(rf"\bя\s+({NAME})")
SPK = re.compile(r"^\s*(?:\d+\s*)?[–—-]\s*")


def fold(s: str) -> str:
    """One spelling per man: ЛИСИЙ = ЛЫСЫЙ, ЁЖИК = ЕЖИК (same narrow fold as run.py `_fold`)."""
    t = (s or "").upper()
    for a, b in (("Ё", "Е"), ("Ы", "И"), ("І", "И"), ("Ї", "И"), ("Й", "И"), ("Є", "Е"), ("Э", "Е"), ("Ґ", "Г")):
        t = t.replace(a, b)
    return t.replace("Ь", "").replace("Ъ", "")


def good(name: str) -> str:
    n = fold(name).strip(" -'’")
    return "" if (len(n) < 3 or n in {fold(s) for s in STOP}) else n


def lines_of(r: dict) -> list[str]:
    return [SPK.sub("", l).strip() for l in r["speech"] if l.strip()]


# ── build ────────────────────────────────────────────────────────────────────────────────────────
def build() -> None:
    db = sqlite3.connect(CORPUS, uri=True)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    if CACHE.exists():
        CACHE.unlink()                         # a derived cache: rebuilt whole, never edited
    c = sqlite3.connect(CACHE)
    c.executescript("""
        CREATE TABLE icp (ref TEXT PRIMARY KEY, day TEXT, freq TEXT, label TEXT, header TEXT, speech TEXT, stations TEXT);
        CREATE TABLE edge (a TEXT, b TEXT, kind TEXT, ref TEXT, day TEXT);
    """)
    seen, n_icp, n_edge = set(), 0, 0
    q = f"SELECT text FROM messages WHERE group_name IN ({','.join('?' * len(GROUPS))}) ORDER BY timestamp"
    for (txt,) in db.execute(q, GROUPS):
        r = parse(txt or "")
        if not r or not r["speech"]:
            continue
        body = re.sub(r"\W", "", " ".join(r["speech"]))[:200]
        if body in seen:
            continue                           # one transmission delivered through two groups
        seen.add(body)
        st = sorted({good(n) for x in r["stations"] for n in re.split(r"[,/]", x)} - {"", "НВ"})
        c.execute("INSERT OR IGNORE INTO icp VALUES (?,?,?,?,?,?,?)",
                  (r["ref"], r["day"], r["ref"].split()[-1], formation(r["header"]), r["header"],
                   "\n".join(lines_of(r)), ",".join(st)))
        n_icp += 1
        pairs = set()
        for l in lines_of(r):
            m = CALL_YA.match(l) or CALL_REP.match(l)
            if m:
                a, b = good(m.group(2)), good(m.group(1))      # a = caller, b = called
                if a and b and a != b:
                    pairs.add((a, b, "call"))
        for i, a in enumerate(st):                             # weaker: listed together as stations
            for b in st[i + 1:]:
                pairs.add((a, b, "station"))
        for a, b, k in pairs:
            c.execute("INSERT INTO edge VALUES (?,?,?,?,?)", (a, b, k, r["ref"], r["day"]))
            n_edge += 1
    # Words that open a phrase («Принимайте», «Знаю», «Правее») leak in as callers. A name counts only
    # if it is SOMEBODY's callsign somewhere: listed as a station, or said as «я Y» at least twice.
    st_names = collections.Counter()
    for (stl,) in c.execute("SELECT stations FROM icp WHERE stations != ''"):
        st_names.update(stl.split(","))
    ya = collections.Counter()
    for (sp,) in c.execute("SELECT speech FROM icp"):
        ya.update(good(y) for y in YA.findall(sp))
    valid = {n for n in st_names} | {n for n, k in ya.items() if k >= 2 and n}
    allnames = {n for (n,) in c.execute("SELECT DISTINCT a FROM edge UNION SELECT DISTINCT b FROM edge")}
    bad = allnames - valid
    c.execute("CREATE TEMP TABLE ok (n TEXT PRIMARY KEY)")
    c.executemany("INSERT INTO ok VALUES (?)", [(n,) for n in valid])
    c.execute("DELETE FROM edge WHERE a NOT IN (SELECT n FROM ok) OR b NOT IN (SELECT n FROM ok)")
    print(f"відкинуто слів, що не є позивними: {len(bad)}")
    c.execute("CREATE INDEX ea ON edge(a)")
    c.execute("CREATE INDEX eb ON edge(b)")
    c.commit()
    print(f"перехоплень (без дублів): {n_icp}, зв'язків: {n_edge} → {CACHE}")


# ── graph + communities ──────────────────────────────────────────────────────────────────────────
W = {"call": 1.0, "station": 0.5}


def chan(freq: str) -> str:
    """A channel, not a unit: 146.5000 and 146.5001 are one radio channel typed two ways."""
    try:
        return f"{float(freq):.2f}"
    except ValueError:
        return freq


def resolve(c: sqlite3.Connection, since: str, until: str) -> dict[str, str]:
    """Who is who across channels. A callsign is first split per channel — АРНИ of 70 мсп and АРНИ of
    the 155 пмп roll call are different men (the first build glued them and made one 493-man «unit»).
    Two channel-copies of one callsign are then merged back only when they talk to the SAME people
    (≥2 shared partners and ≥20% overlap): a man who moves keeps his partners, a namesake does not."""
    part: dict[str, set] = collections.defaultdict(set)
    for a, b, f in c.execute("SELECT e.a, e.b, i.freq FROM edge e JOIN icp i ON i.ref = e.ref "
                             "WHERE e.day >= ? AND e.day <= ?", (since, until)):
        ch = chan(f)
        part[f"{a}@{ch}"].add(b)
        part[f"{b}@{ch}"].add(a)
    parent = {n: n for n in part}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    by_name: dict[str, list[str]] = collections.defaultdict(list)
    for n in part:
        by_name[n.split("@")[0]].append(n)
    for name, ns in by_name.items():
        for i, x in enumerate(ns):
            for y in ns[i + 1:]:
                sh = part[x] & part[y]
                if len(sh) >= 2 and len(sh) >= 0.2 * min(len(part[x]), len(part[y])):
                    parent[find(x)] = find(y)
    # a node is named by its callsign plus the channel where it is heard most
    return {n: find(n) for n in part}


def graph(c: sqlite3.Connection, since: str = "", until: str = "9999") -> dict[str, dict[str, float]]:
    rep = resolve(c, since, until)
    g: dict[str, dict[str, float]] = collections.defaultdict(lambda: collections.defaultdict(float))
    for a, b, k, f in c.execute("SELECT e.a, e.b, e.kind, i.freq FROM edge e JOIN icp i ON i.ref = e.ref "
                                "WHERE e.day >= ? AND e.day <= ?", (since, until)):
        ch = chan(f)
        x, y = rep[f"{a}@{ch}"], rep[f"{b}@{ch}"]
        g[x][y] += W[k]
        g[y][x] += W[k]
    graph.rep = rep
    return g


def node_of(name: str, freq: str) -> str:
    return graph.rep.get(f"{name}@{chan(freq)}", f"{name}@{chan(freq)}")


def nodes_named(g, name: str) -> list[str]:
    return sorted({n for n in g if n.split("@")[0] == name}, key=lambda n: -sum(g[n].values()))


def communities(g: dict[str, dict[str, float]], min_w: float = 2.0, rounds: int = 30) -> dict[str, int]:
    """Weighted label propagation, deterministic: every man takes the label his strongest ties carry.
    Ties under `min_w` (one chance encounter) are ignored — they are what glues two units into one."""
    nodes = sorted(n for n, nb in g.items() if sum(w for w in nb.values() if w >= min_w) > 0)
    lab = {n: i for i, n in enumerate(nodes)}
    order = sorted(nodes, key=lambda n: -sum(g[n].values()))
    for _ in range(rounds):
        moved = 0
        for n in order:
            score: dict[int, float] = collections.defaultdict(float)
            for m, w in g[n].items():
                if w >= min_w and m in lab:
                    score[lab[m]] += w
            if not score:
                continue
            best = max(sorted(score), key=lambda k: score[k])
            if best != lab[n]:
                lab[n] = best
                moved += 1
        if not moved:
            break
    return lab


def members(lab: dict[str, int], g) -> dict[int, list[str]]:
    out = collections.defaultdict(list)
    for n, k in lab.items():
        out[k].append(n)
    for k in out:
        out[k].sort(key=lambda n: -sum(g[n].values()))
    return out


def labels_of(c: sqlite3.Connection, names: list[str], top: int = 3) -> str:
    """The unit labels of the headers these men are heard under — printed as LABELS, never evidence."""
    cnt = collections.Counter()
    want = set(names[:60])
    rep = graph.rep
    raw = {n.split("@")[0] for n in want}
    for a, b, f, lab in c.execute(
            f"SELECT e.a, e.b, i.freq, i.label FROM edge e JOIN icp i ON i.ref = e.ref "
            f"WHERE e.a IN ({','.join('?' * len(raw))}) OR e.b IN ({','.join('?' * len(raw))})", (*raw, *raw)):
        if rep.get(f"{a}@{chan(f)}") in want or rep.get(f"{b}@{chan(f)}") in want:
            cnt[lab or "без підпису"] += 1
    tot = sum(cnt.values()) or 1
    return ", ".join(f"{k} {100 * v // tot}%" for k, v in cnt.most_common(top))


def name_of(k: int, mem: dict[int, list[str]]) -> str:
    return "гурт " + "/".join(mem[k][:3])


def short(n: str) -> str:
    return n.replace("@", " @")


# ── commands ─────────────────────────────────────────────────────────────────────────────────────
def cmd_code(c, pattern: str, since: str, until: str) -> None:
    rx = re.compile(pattern, re.I)
    g = graph(c, since, until)
    lab = communities(g)
    mem = members(lab, g)
    uses = []
    for ref, day, freq, label, speech in c.execute(
            "SELECT ref, day, freq, label, speech FROM icp WHERE day >= ? AND day <= ?", (since, until)):
        if not rx.search(speech):
            continue
        for l in speech.split("\n"):
            if not rx.search(l):
                continue
            pre = l[:rx.search(l).start()]
            m = CALL_YA.match(pre + " ") or CALL_REP.match(pre + " ")
            sayer = good(m.group(2)) if m else ""
            to = good(m.group(1)) if m else ""
            if not sayer:
                y = YA.findall(pre)
                sayer = good(y[-1]) if y else ""
            uses.append((day, ref, label, node_of(sayer, freq) if sayer else "", to))
    if not uses:
        print("код не знайдено")
        return
    known = [u for u in uses if u[3]]
    print(f"вживань: {len(uses)}, з них мовця встановлено: {len(known)} "
          f"({100 * len(known) // len(uses)}%), перше {min(u[0] for u in uses)}, останнє {max(u[0] for u in uses)}")
    by: dict[object, list] = collections.defaultdict(list)
    for u in known:
        by[lab.get(u[3], f"поза графом:{u[3]}")].append(u)
    print("\n== де код «рідний» (мовці за гуртами людей, НЕ за частотами)")
    ranked = sorted(by.items(), key=lambda kv: (-len({u[3] for u in kv[1]}), -len(kv[1])))
    for k, us in ranked:
        sayers = collections.Counter(u[3] for u in us)
        days = sorted({u[0] for u in us})
        nm = name_of(k, mem) if isinstance(k, int) else k
        print(f"  {nm}: {len(us)} вживань, {len(sayers)} різних мовців, {len(days)} днів "
              f"({days[0]} .. {days[-1]})")
        print(f"     хто: {', '.join(f'{n}({v})' for n, v in sayers.most_common(12))}")
        if isinstance(k, int):
            print(f"     склад гурту (верх): {', '.join(mem[k][:12])}  [{len(mem[k])} осіб]")
            print(f"     ярлики частот, де вони звучать (НЕ доказ): {labels_of(c, mem[k])}")
    return lab, mem, g


def cmd_who(c, names: list[str], since: str, until: str) -> None:
    g = graph(c, since, until)
    lab = communities(g)
    mem = members(lab, g)
    for raw in names:
        nodes = nodes_named(g, fold(raw))
        if not nodes:
            print(f"\n{fold(raw)}: у графі немає")
            continue
        for n in nodes[:4]:
            nb = sorted(g[n].items(), key=lambda kv: -kv[1])
            tot = sum(w for _, w in nb) or 1
            share: dict[object, float] = collections.defaultdict(float)
            for m, w in nb:
                share[lab.get(m, "поза гуртами")] += w
            print(f"\n{short(n)}: вага {tot:g}; сам у: {name_of(lab[n], mem) if n in lab else 'поза гуртами'}")
            print("   з ким говорить: " + ", ".join(f"{short(m)}({w:g})" for m, w in nb[:14]))
            print("   вага по гуртах: " + "; ".join(
                f"{name_of(k, mem) if isinstance(k, int) else k} {100 * w / tot:.0f}%"
                for k, w in sorted(share.items(), key=lambda kv: -kv[1])[:4]))


def cmd_community(c, raw: str, since: str, until: str) -> None:
    g = graph(c, since, until)
    lab = communities(g)
    mem = members(lab, g)
    ns = [x for x in nodes_named(g, fold(raw)) if x in lab]
    if not ns:
        print(f"{fold(raw)}: поза гуртами")
        return
    k = lab[ns[0]]
    print(f"{name_of(k, mem)} — {len(mem[k])} осіб")
    print("   " + ", ".join(mem[k][:60]))
    print(f"   ярлики частот (НЕ доказ): {labels_of(c, mem[k], 5)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "code", "who", "community"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--since", default="")
    ap.add_argument("--until", default="9999")
    a = ap.parse_args()
    if a.cmd == "build":
        build()
        return
    if not CACHE.exists():
        sys.exit("спершу: code_owner.py build")
    c = sqlite3.connect(CACHE)
    if a.cmd == "code":
        cmd_code(c, a.args[0], a.since, a.until)
    elif a.cmd == "who":
        cmd_who(c, a.args, a.since, a.until)
    else:
        cmd_community(c, a.args[0], a.since, a.until)
    print("\nмежі: один позивний = іноді кілька людей; криві розшифровки ділять одну людину; "
          "телефон і месенджер не видно.")


if __name__ == "__main__":
    main()
