#!/usr/bin/env python3
"""Print a run's alerts the way they are meant to be read: push first, with the raw intercept.

    python3 tools/alerts/show.py knowledge/upstream/alerts/runs/last24h_claude-opus-5_high.json
    python3 tools/alerts/show.py <run.json> --level log      # the quiet pile
    python3 tools/alerts/show.py <run.json> --raw            # full speech, ready to forward

`push` is what wakes a duty officer; `log` is everything on-topic that did not reach the bar and
sits in the base for the daily read. The split is the model's, and it is the thing being calibrated
— read the push list, say which line does not belong, and the doctrine in bench.py moves.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

CORPUS = "~/wa-monitor/messages.db"


def fetch(ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    con = sqlite3.connect(f"file:{CORPUS}?mode=ro", uri=True)
    q = f"SELECT id, coalesce(text,'') FROM messages WHERE id IN ({','.join('?' * len(ids))})"
    out = dict(con.execute(q, ids).fetchall())
    con.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--level", default="push", choices=["push", "log", "all"])
    ap.add_argument("--raw", action="store_true", help="full intercept text, not a one-liner")
    a = ap.parse_args()

    run = json.loads(Path(a.run).read_text(encoding="utf-8"))
    alerts = run["alerts"]
    if a.level != "all":
        alerts = [x for x in alerts if x.get("level") == a.level]
    order = {"push": 0, "log": 1}
    alerts.sort(key=lambda x: (order.get(x.get("level"), 9), x.get("hhmmss", "")))

    print(f"{run['day']} · {run['model']}{' ' + run['effort'] if run.get('effort') else ''} · "
          f"прочитано {run['intercepts_read']} з {run['intercepts_raw']} · "
          f"push {run.get('push', '?')}, log {run.get('log', '?')}\n")

    texts = fetch([x["id"] for x in alerts]) if a.raw else {}
    for x in alerts:
        mark = "‼️" if x.get("level") == "push" else "·"
        print(f"{mark} {x.get('hhmmss','?')} · {x.get('freq','?')} · {x.get('src','?')} · "
              f"[{x.get('class')}] conf {x.get('confidence')}")
        print(f"   {x.get('why')}")
        if a.raw and x["id"] in texts:
            print("   " + "\n   ".join(texts[x["id"]].strip().splitlines()[:40]))
        print(f"   id={x['id']}")
        print()


if __name__ == "__main__":
    main()
