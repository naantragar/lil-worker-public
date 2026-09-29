#!/usr/bin/env python3
"""The LOBBY: the transfer group lives in the corpus, in a room of its own.

    python3 tools/corpus/transfer_lobby.py            # what is waiting, newest first
    python3 tools/corpus/transfer_lobby.py --sync     # pull new rows into the lobby index
    python3 tools/corpus/transfer_lobby.py --note <id> "<what we decided>"

## What this is

`Transfer Krevetka` (`120363408965106490@g.us`) is the owner's personal delivery channel to me -
he forwards files and messages from his own WhatsApp because that is faster than reaching the
server. Six months of our document material came through it: debriefs, rosters, the code tables.

His rule, 29.09.2026, after we talked it through twice:

    «Всё льётся в корпус, но в отдельное место, откуда в основную систему попадает только
     по нашему решению.»

So it is NOT cut out of the corpus - that was my first, too-literal reading, and it made looking
at his material needlessly awkward. It stays in `messages.db` where everything else is, marked
`category='transfer'`, and it is kept out of ANALYSIS rather than out of storage:

1. **The mark** - every row of this group carries `category='transfer'` in the corpus itself, so
   any query can see at a glance that this is handed-over material, not air we heard.
2. **The door** - `code_sieve.corpus()`, the single door every analysis tool reads through,
   refuses this jid unconditionally. No report, sieve or index can count it, ever, including rows
   the collector wrote a second ago.
3. **This index** - `knowledge/upstream/transfer/transfer.db` mirrors the rows and adds one column
   the corpus has no business holding: `note`, what WE decided about each item. Read, understand,
   ask - and only then does anything graduate into the glossary or the corpus proper.

The lobby never empties itself. Deciding is a conversation, not a cron job.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CORPUS = Path("~/wa-monitor/messages.db")
TRANSFER = REPO / "knowledge" / "upstream" / "transfer" / "transfer.db"
TRANSFER_JID = "120363408965106490@g.us"     # «Transfer Krevetka», ex-«Main Monitor (test)»

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, group_jid TEXT, group_name TEXT,
    sender_jid TEXT, sender_name TEXT, text TEXT, msg_type TEXT,
    media_type TEXT, caption TEXT, media_path TEXT,
    timestamp INTEGER, collected_at TEXT, drained_at TEXT,
    note TEXT                                  -- my own: what we decided about this one
);
CREATE INDEX IF NOT EXISTS ix_transfer_ts ON messages(timestamp);
"""

COLS = ("id", "group_jid", "group_name", "sender_jid", "sender_name", "text",
        "msg_type", "media_type", "caption", "media_path", "timestamp", "collected_at")


def tcon() -> sqlite3.Connection:
    TRANSFER.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(TRANSFER))
    c.executescript(SCHEMA)
    c.row_factory = sqlite3.Row
    return c


def show_list(limit: int) -> None:
    if not TRANSFER.exists():
        sys.exit(f"бази переносу ще немає: {TRANSFER}")
    c = tcon()
    n = c.execute("SELECT count(*) FROM messages").fetchone()[0]
    print(f"БАЗА ПЕРЕНОСУ — {TRANSFER}\n  повідомлень: {n}\n")
    for r in c.execute("SELECT * FROM messages ORDER BY timestamp DESC LIMIT ?", (limit,)):
        when = datetime.fromtimestamp(r["timestamp"] / 1000).strftime("%d.%m %H:%M")
        body = " ".join((r["text"] or r["caption"] or "").split())[:78] or "(без тексту)"
        print(f"  {when}  {body}" + (f"   [{r['note']}]" if r["note"] else ""))
    c.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true",
                    help="pull rows the collector has added since last time into the lobby index")
    ap.add_argument("--note", nargs=2, metavar=("ID", "TEXT"),
                    help="record what we decided about one item")
    ap.add_argument("--limit", type=int, default=25)
    a = ap.parse_args()

    if a.note:
        c = tcon()
        c.execute("UPDATE messages SET note=? WHERE id=?", (a.note[1], a.note[0]))
        c.commit()
        print(f"записано рішення для {a.note[0]}: {a.note[1]}")
        c.close()
        return 0
    if not a.sync:
        show_list(a.limit)
        return 0

    ro = sqlite3.connect(f"file:{CORPUS}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    rows = [dict(r) for r in ro.execute(
        f"SELECT {','.join(COLS)} FROM messages WHERE group_jid = ?", (TRANSFER_JID,))]
    ro.close()

    print(f"у корпусі повідомлень групи переносу: {len(rows)}")
    if rows:
        ds = sorted(datetime.fromtimestamp(r["timestamp"] / 1000).date() for r in rows)
        print(f"   період: {ds[0].strftime('%d.%m.%Y')} - {ds[-1].strftime('%d.%m.%Y')}")
    t = tcon()
    have = {r[0] for r in t.execute("SELECT id FROM messages")}
    fresh = [r for r in rows if r["id"] not in have]
    print(f"   уже в базі переносу: {len(rows) - len(fresh)}, до перенесення: {len(fresh)}")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    t.executemany(
        f"INSERT OR IGNORE INTO messages ({','.join(COLS)}, drained_at) "
        f"VALUES ({','.join('?' * len(COLS))}, ?)",
        [tuple(r[c] for c in COLS) + (now,) for r in fresh])
    t.commit()
    total = t.execute("SELECT count(*) FROM messages").fetchone()[0]
    undecided = t.execute("SELECT count(*) FROM messages WHERE coalesce(note,'')=''").fetchone()[0]
    t.close()
    # The corpus is NOT touched. The rows stay where the collector put them, marked
    # `category='transfer'`; this index only mirrors them and carries our decisions.
    print(f"\nу передбаннику: {total}, без рішення: {undecided}")
    print("корпус не змінювався - рядки лишаються на місці з міткою transfer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
