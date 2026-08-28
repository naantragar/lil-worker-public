#!/usr/bin/env python3
"""Count the personnel that MOVED on a given frequency over a window.

One model call per (frequency, day) so a long window can be run in slices without ever risking a
30-minute silent turn, and so a re-run only redoes the slices that are missing. Results are cached
as JSON in /tmp/movecount/.

The counting rule is the hard part and is stated to the model verbatim: a man counts once, by
callsign; a group named only by size ("8 малых", "трое") is kept SEPARATELY as a range, because
adding it to named men double-counts whoever in that group also has a callsign.
"""
import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, "~/lil_worker/tools/analytics2")
sys.path.insert(0, "~/lil_worker/watch")
import run as R      # noqa: E402
import pull          # noqa: E402

OUT = Path("/tmp/movecount")
MODEL = "claude-opus-5"
NO_TOOLS = ["Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebFetch", "WebSearch", "Task"]

SYSTEM = """Ти аналітик радіоперехоплень. Тобі дано перехоплення ОДНІЄЇ мережі за ОДНУ добу.
Знайди все, що є ПЕРЕМІЩЕННЯМ ОСОБОВОГО СКЛАДУ, і перелічи людей, які саме рухалися.

Що є переміщенням о\\с:
- вихід/захід шг (штурмових груп), заведення о\\с на позиції, зняття з позицій, ротація;
- рух в\\с пішки або на техніці між укриттями, точками, орієнтирами;
- евакуація (300/200 везуть — ті, кого везуть, ТЕЖ рухалися; евакуаційна група теж);
- супровід: якщо БпЛА супроводжує групу, рухається ГРУПА, а не оператор дрона.

Що НЕ є переміщенням о\\с:
- доставка МТЗ/БК/води дроном або технікою без людей;
- розмова про майбутній вихід, який ще не почався (план без факту руху);
- сам оператор БпЛА, зв'язківець, ст мережі, які лишаються на місці;
- рух самої техніки без згадки людей у ній.

Для КОЖНОГО епізоду поверни:
- "time": час перехоплення (HH:MM);
- "named": список позивних тих, хто РУХАВСЯ (тільки ті, про кого прямо сказано, що вони йдуть/їдуть/
  їх ведуть/везуть). Не додавай тих, хто лише говорить по рації;
- "unnamed": число людей без позивних, якщо воно прямо названо («8 малих», «троє», «двох 300»),
  інакше null;
- "basis": ДОСЛІВНА цитата з перехоплення, з якої видно рух (коротко);
- "what": що саме за рух (вихід шг / ротація / евакуація / переміщення між укриттями …).

Правила:
- Не вигадуй позивних. Якщо в тексті імені немає — це "unnamed".
- Якщо не впевнений, що це рух людей, епізод НЕ включай.
- Якщо за добу руху не було — поверни порожній масив.

Відповідь — ТІЛЬКИ JSON-масив, без тексту навколо:
[{"time":"HH:MM","named":["..."],"unnamed":null,"basis":"...","what":"..."}]
"""


def slice_recs(freq: float, day: str) -> list[dict]:
    lo = datetime.strptime(day, "%Y-%m-%d")
    recs = R.fetch(f"{lo:%Y-%m-%d} 00:00", f"{lo + timedelta(days=1):%Y-%m-%d} 00:00")
    return [r for r in recs
            if R._is_float(r.get("freq")) and abs(float(r["freq"]) - freq) * 1000 <= R.FREQ_TOL_KHZ
            and f"{r['_dt']:%Y-%m-%d}" == day]


def judge(freq: float, day: str, recs: list[dict]) -> list[dict]:
    material = f"Частота {freq:.4f}, доба {day}, перехоплень: {len(recs)}\n\n" + pull.render(recs)
    cmd = ["claude", "-p", "--model", MODEL, "--system-prompt", SYSTEM,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--disallowedTools", *NO_TOOLS]
    p = subprocess.run(cmd, input=material, capture_output=True, text=True, timeout=2400,
                       cwd="/tmp")
    if p.returncode != 0:
        raise RuntimeError(f"claude exit {p.returncode}: {p.stderr[-200:]}")
    m = re.search(r"\[.*\]", p.stdout, re.S)
    return json.loads(m.group(0)) if m else []


def one(task):
    freq, day = task
    OUT.mkdir(parents=True, exist_ok=True)
    dst = OUT / f"{freq:.4f}_{day}.json"
    if dst.exists():
        return f"{day} {freq:.4f}: вже є"
    recs = slice_recs(freq, day)
    if not recs:
        dst.write_text("[]")
        return f"{day} {freq:.4f}: перехоплень 0"
    eps = judge(freq, day, recs)
    dst.write_text(json.dumps(eps, ensure_ascii=False, indent=2))
    named = {n for e in eps for n in (e.get("named") or [])}
    return f"{day} {freq:.4f}: {len(recs)} перехоплень -> епізодів {len(eps)}, названих {len(named)}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", type=float, required=True)
    ap.add_argument("--days", required=True, help="comma-separated YYYY-MM-DD")
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    tasks = [(a.freq, d.strip()) for d in a.days.split(",") if d.strip()]
    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        for line in pool.map(one, tasks):
            print(line, flush=True)


if __name__ == "__main__":
    main()
