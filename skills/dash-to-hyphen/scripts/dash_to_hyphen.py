#!/usr/bin/env python3
"""Replace typographic dashes with the plain ASCII hyphen-minus, spacing-aware.

The naive fix (str.replace) is wrong in both directions: an em dash glued between two
letters ("word<em>word") becomes "word-word", inventing a compound word that was never
there; while a real hyphen inside a word must NOT gain spaces. So the order matters:
decide the spacing from the CONTEXT of each dash first, then emit the hyphen.

Rules
  em / en / figure dash, horizontal bar
        between letters   -> " - "  (one space each side, so no fake compound word)
        between digits    -> "-"    (a range stays tight: 2020-2024)
        already spaced    -> "-"    (spacing left as it was)
        at line start     -> "- "   (dialogue marker)
  unicode hyphen U+2010, U+2011  -> "-" always, never spaced (it already IS a hyphen)
  minus sign U+2212              -> "-" always, never spaced (it already IS a minus)

Dry run by default: prints what would change and touches nothing. Add --write to apply.

NOTE for whoever edits this file: every dash character here is written as a \\u escape on
purpose. If they were literal, running this script over its own directory would rewrite
its own tables and quietly break it.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# dashes proper - these may need spaces around them
# em, en, figure dash, horizontal bar, two-em, three-em
DASHES = "\u2014\u2013\u2012\u2015\u2e3a\u2e3b"
# already a hyphen/minus in meaning - only the codepoint is wrong
# unicode hyphen, non-breaking hyphen, minus sign
PLAIN = "\u2010\u2011\u2212"

_DASH_RUN = re.compile(f"[{DASHES}]+")
_PLAIN = re.compile(f"[{PLAIN}]")
_WORD = re.compile(r"\w")
_DIGIT = re.compile(r"\d")

DEFAULT_EXT = ".py,.md,.txt,.js,.ts,.tsx,.jsx,.html,.css,.json,.yml,.yaml,.sh,.sql"


def convert_line(line: str) -> str:
    line = _PLAIN.sub("-", line)

    def repl(m: re.Match) -> str:
        prev_ch = line[m.start() - 1] if m.start() else ""
        next_ch = line[m.end()] if m.end() < len(line) else ""

        # a range between digits keeps its tightness: 2020-2024
        if prev_ch and next_ch and _DIGIT.match(prev_ch) and _DIGIT.match(next_ch):
            return "-"

        left = " " if prev_ch and _WORD.match(prev_ch) else ""
        right = " " if next_ch and _WORD.match(next_ch) else ""
        return f"{left}-{right}"

    return _DASH_RUN.sub(repl, line)


def convert(text: str) -> str:
    return "\n".join(convert_line(l) for l in text.split("\n"))


def iter_files(paths: list[str], exts: set[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in exts and ".git" not in f.parts:
                    out.append(f)
        else:
            print(f"skip (not found): {p}", file=sys.stderr)
    return out


def self_test() -> int:
    EM, EN, HY, MINUS = "\u2014", "\u2013", "\u2011", "\u2212"
    cases = [
        (f"word{EM}word", "word - word"),
        (f"word {EM} word", "word - word"),
        (f"word{EM} word", "word - word"),
        (f"word {EM}word", "word - word"),
        (f"2020{EN}2024", "2020-2024"),
        (f"{EM} Dialogue line", "- Dialogue line"),
        ("well-known stays", "well-known stays"),
        (f"e{HY}mail", "e-mail"),
        (f"temp {MINUS}5", "temp -5"),
        (f"a{EM}b{EM}c", "a - b - c"),
        ("---", "---"),
        (f"| col {EM} col |", "| col - col |"),
        (f"\u0442\u0440\u044e\u043a{EM}\u0432\u043e\u0442", "\u0442\u0440\u044e\u043a - \u0432\u043e\u0442"),
        (f"({EM})", "(-)"),
        (f"line ends{EM}", "line ends -"),
    ]
    bad = 0
    for src, want in cases:
        got = convert(src)
        if got != want:
            bad += 1
            print(f"FAIL  {src!r}\n  want {want!r}\n  got  {got!r}")
    print(f"self-test: {len(cases) - bad}/{len(cases)} passed")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Replace typographic dashes with plain hyphens.")
    ap.add_argument("paths", nargs="*", help="files and/or directories")
    ap.add_argument("--write", action="store_true", help="apply (default: dry run)")
    ap.add_argument("--ext", default=DEFAULT_EXT, help="extensions to scan inside directories")
    ap.add_argument("--self-test", action="store_true", help="run the built-in cases and exit")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not a.paths:
        ap.error("give at least one file or directory (or --self-test)")

    exts = {e if e.startswith(".") else "." + e for e in a.ext.split(",") if e.strip()}
    touched = 0
    for f in iter_files(a.paths, exts):
        try:
            src = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        out = convert(src)
        if out == src:
            continue
        touched += 1
        pairs = [(x, y) for x, y in zip(src.split("\n"), out.split("\n")) if x != y]
        print(f"{'wrote' if a.write else 'would change'}: {f}  ({len(pairs)} line(s))")
        if a.write:
            f.write_text(out, encoding="utf-8")
        else:
            for x, y in pairs:
                print(f"    - {x.strip()[:100]}")
                print(f"    + {y.strip()[:100]}")

    tail = "" if a.write else "  (re-run with --write to apply)"
    print(f"{touched} file(s) {'changed' if a.write else 'would change'}{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
