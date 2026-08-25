#!/usr/bin/env python3
"""Pass C: the roster and the legend — the half of the report that is not events.

Checked against a real analyst's report (РЕР 21.08.2026): of its ~90 content lines, only 23 are
timestamped events. Everything else is `ПОЗИВНИЙ - роль` and `«слово» - значення`, per network. That
layer cannot be produced by the per-thread extraction pass — a callsign's function is invisible in
one exchange and obvious across a hundred — so it is built here, from the aggregate table that
`analytics_entities.py` compresses out of the whole window.

Runs AFTER selection on purpose: the real report lists a roster only for the networks that made it
into the report at all (8 of 36 that day). Building rosters for silent networks is work nobody reads.

    python3 tools/analytics_sense.py --entities entities.json --events report_selected.json \
        [--out roster.json] [--model claude-opus-5] [--effort high]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from analytics_run import call_model, extract_json  # noqa: E402

GLOSSARY = HERE.parent / "matrix" / "bot" / "prompts" / "refraz" / "10_glossary.md"
TASK = HERE / "analytics_sense_prompt.md"


def extract_obj(s: str) -> dict:
    """Pass C answers with an object, not an array — extract_json only finds arrays."""
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entities", required=True)
    ap.add_argument("--events", required=True)
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--out")
    a = ap.parse_args()

    ent = json.loads(Path(a.entities).read_text())
    events = json.loads(Path(a.events).read_text())

    # Only the networks that survived selection, and only their callsigns.
    live_nets = {e.get("_net") for e in events if e.get("_net")}
    rows = [r for r in ent["callsigns"] if any(n in live_nets for n in r["networks"])]
    print(f"мереж у звіті: {len(live_nets)}   позивних до розбору: {len(rows)} "
          f"(з {len(ent['callsigns'])})", file=sys.stderr)

    by_net = defaultdict(list)
    for e in events:
        by_net[e["_net"]].append(f"{e.get('time','')} {e.get('text','')}")

    # Networks are addressed by NUMBER. Asked to echo the header back as text, the model paraphrased
    # it («кілька мереж (416.1921, 416.1950)»), and 14 of 21 roster blocks failed to match their
    # network and were dropped — half the report silently lost.
    order = list(by_net)
    material = [
        "# Таблиця позивних\n",
        json.dumps({"callsigns": rows}, ensure_ascii=False, indent=1),
        "\n\n# Події доби, по мережах\n",
    ]
    for i, net in enumerate(order):
        material.append(f"\n## [{i}] {net}\n" + "\n".join(by_net[net]))

    rules = TASK.read_text() + "\n\n---\n\n# Глосарій\n\n" + GLOSSARY.read_text()
    raw, dt = call_model(rules, "".join(material), a.model, a.effort)
    data = extract_obj(raw)

    if not data:
        print(f"модель не повернула JSON за {dt:.0f}s; сирий вивід:\n{raw[:600]}", file=sys.stderr)
        sys.exit(1)

    # Resolve the number back to the header, so downstream code keeps working with names.
    unresolved = 0
    for key in ("callsign_roles", "codes", "positions", "groups"):
        for row in data.get(key, []):
            n = row.pop("net", None)
            if isinstance(n, int) and 0 <= n < len(order):
                row["network"] = order[n]
            else:
                unresolved += 1
    if unresolved:
        print(f"увага: {unresolved} рядків без придатного номера мережі", file=sys.stderr)

    for key in ("callsign_roles", "positions", "codes", "groups", "network_links"):
        print(f"   {key:16} {len(data.get(key, []))}", file=sys.stderr)
    print(f"час моделі: {dt:.0f}s", file=sys.stderr)

    out = Path(a.out) if a.out else None
    if out:
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        print(f"\n{out}", file=sys.stderr)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
