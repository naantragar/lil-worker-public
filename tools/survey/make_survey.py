#!/usr/bin/env python3
"""Turn a batch of work into a survey the bot can walk the owner through.

    python3 tools/survey/make_survey.py readings --src <SPOST_*.json> --title "Розбір прочитань 11.09"
    python3 tools/survey/make_survey.py sheet    --src <лист.txt>     --title "…" [--sep '--- ']

`readings` builds one question per extracted observation: how we read it, the operator's header, the
verbatim quote. `sheet` splits an existing text sheet on its separator, for anything already written
as a flat file (the «хто водить» key, for instance).

The survey is just JSON under `knowledge/upstream/surveys/`; the bot picks it up with no restart.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "knowledge" / "upstream" / "surveys"

NAME = {"order_move": "НАКАЗ РУХАТИСЬ", "guide": "ВЕДЕ ГОЛОСОМ", "report": "ДОПОВІДЬ",
        "place_named": "НАЗВАНО ТОЧКУ", "meet": "ЗУСТРІЧ", "supply": "ПОСТАЧАННЯ",
        "relay": "ПЕРЕДАВ ВКАЗІВКУ ДАЛІ", "comms": "ПРО ЗВ'ЯЗОК"}


def load_run():
    spec = importlib.util.spec_from_file_location(
        "analytics2_run", REPO / "tools" / "analytics2" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def from_readings(src: Path) -> list[dict]:
    d = json.loads(src.read_text(encoding="utf-8"))
    obs = d["observations"]
    day = datetime.strptime(d["day"], "%Y-%m-%d")
    M = load_run()
    need = {o["msg_id"] for o in obs}
    rec = {r["msg_id"]: r for r in M.fetch(f"{day - timedelta(days=1):%Y-%m-%d} 15:00",
                                           f"{day:%Y-%m-%d} 15:00")
           if r.get("msg_id") in need}
    items = []
    for i, x in enumerate(obs, 1):
        r = rec.get(x["msg_id"])
        tail = []
        if x.get("to_whom"):
            tail.append(f"кому: {x['to_whom']}")
        if x.get("to"):
            tail.append(f"місце: {x['to']}")
        if x.get("to_person"):
            tail.append(f"до: {x['to_person']}")
        hdr = " → ".join(s for s in ((r.get("stations") or [])[:2] if r else []) if s) or "—"
        items.append({"id": i, "text":
                      f"{NAME.get(x['type'], x['type'])}   говорить: {x.get('who') or '?'}"
                      + (("\n" + " · ".join(tail)) if tail else "")
                      + f"\nшапка: {hdr}"
                      + f"\n\n«{(x.get('quote') or '').strip()}»"})
    return items


CHECKBOX = re.compile(r"^\s*\[\s*[xX ]?\s*\].*$", re.M)


def from_sheet(src: Path, sep: str) -> list[dict]:
    """Split a paper sheet into questions.

    Two things the first version got wrong, both visible in the result: splitting on a bare `--- `
    left the trailing `N ---` glued to the first line of every block, and the paper CHECKBOXES
    («[ ] ВОДИТЬ  [ ] НЕ ВОДИТЬ») came through into a bot message that has real buttons under it —
    text contradicting the controls beneath it.
    """
    text = src.read_text(encoding="utf-8")
    blocks = re.split(rf"^{re.escape(sep)}\s*\d+\s*-*\s*$|^{re.escape(sep)}", text, flags=re.M)
    out = []
    for b in blocks[1:]:
        b = re.sub(r"^\s*\d+\s*-{2,}\s*", "", b)      # the «N ---» left over by the split
        b = CHECKBOX.sub("", b).strip("=\n \t")
        b = re.sub(r"\n{3,}", "\n\n", b).strip()
        if b:
            out.append({"id": len(out) + 1, "text": b[:3400]})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["readings", "sheet"])
    ap.add_argument("--src", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--name", default=None, help="file name of the survey, default from --src")
    ap.add_argument("--sep", default="--- ")
    ap.add_argument("--options", default=None,
                    help="JSON list of answer buttons, e.g. "
                         "'[{\"key\":\"vodyt\",\"label\":\"✅ водить\"}]'. "
                         "Omit for the default вірно / не так / не видно.")
    a = ap.parse_args()

    src = Path(a.src)
    items = from_readings(src) if a.kind == "readings" else from_sheet(src, a.sep)
    if not items:
        sys.exit("нічого не зібрано — перевір --src / --sep")
    name = a.name or src.stem.lower().replace(".", "_")
    DIR.mkdir(parents=True, exist_ok=True)
    out = DIR / f"{name}.json"
    body = {"title": a.title, "source": str(src),
            "created": datetime.now().strftime("%Y-%m-%d %H:%M"), "items": items}
    if a.options:
        body["options"] = json.loads(a.options)
    out.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{out}   {len(items)} питань", file=sys.stderr)


if __name__ == "__main__":
    main()
