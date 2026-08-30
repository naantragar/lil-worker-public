#!/usr/bin/env python3
"""Flush the collector's SQLite WAL so freshly collected messages become visible downstream.

The chain is: WhatsApp -> collector SQLite (instant) -> upstream worker -> Postgres (every 60s) -> us.
The collector writes in WAL mode, and upstream's worker has ONLY `messages.db` mounted (no -wal/-shm,
deliberately: least privilege). So the worker sees the last CHECKPOINTED state, and the checkpoint
is triggered by VOLUME (`wal_autocheckpoint = 1000` pages = ~4 MB), not by time. At this group's
traffic that is one flush every 40-60 minutes, which is exactly the delay we were seeing: the
collector had the intercept within a second, and we saw it an hour later.

This runs as a separate process ON PURPOSE. A `PRAGMA wal_checkpoint` can be issued by ANY
connection, so the fix needs no change to the collector and no restart of it — and the collector is
under a hard "systemd only, never touch by hand" rule.

PASSIVE is the safe mode: it never blocks a writer and never waits. If the collector is mid-write,
the checkpoint simply does less work this minute and catches up on the next tick.
"""
import os
import sqlite3
import sys
import time
from datetime import datetime

DB = "~/wa-monitor/messages.db"
LOG = "~/lil_worker/watch/wal_checkpoint.log"
# Only log when something actually moved, or on trouble — a per-minute "nothing to do" line would
# bury the interesting ones.
QUIET = True


def main() -> int:
    if not os.path.exists(DB):
        print(f"no db: {DB}", file=sys.stderr)
        return 1
    t0 = time.time()
    try:
        con = sqlite3.connect(DB, timeout=10)
        busy, log_pages, done_pages = con.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        con.close()
    except sqlite3.Error as e:
        with open(LOG, "a") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} ERROR {e!r}\n")
        return 1
    if busy or done_pages or not QUIET:
        with open(LOG, "a") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} busy={busy} "
                     f"log_pages={log_pages} checkpointed={done_pages} "
                     f"({time.time() - t0:.2f}s)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
