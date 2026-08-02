"""One timestamped logger for the whole Matrix door.

Before this, logging was a bare `print("[mb]", …)` with NO timestamp anywhere, so the log could not
answer the only question ever asked of it — "where did those two minutes go?". Nothing could be
reconstructed after the fact: not how long a download took, not how long the model thought, not how
long a message sat queued behind another turn. Both matrix_bridge and claude_bridge log through
here so every line carries the same clock.
"""
from __future__ import annotations

import time

_T0 = time.monotonic()


def log(*a) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[mb {ts}]", *a, flush=True)


def since(t0: float) -> str:
    """Elapsed seconds since a time.monotonic() mark, formatted for a log line."""
    return f"{time.monotonic() - t0:.1f}s"


def clip(s, n: int = 80) -> str:
    """Single-line, length-capped rendering of anything — log lines must stay greppable."""
    t = " ".join(str(s).split())
    return t if len(t) <= n else t[: n - 1] + "…"
