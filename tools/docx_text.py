#!/usr/bin/env python3
"""Extract readable text from a .docx — stdlib only, no python-docx, no pandoc.

A .docx is a zip whose `word/document.xml` holds the body. Paragraphs are `<w:p>`, runs of text are
`<w:t>`, a line break is `<w:br/>`, a tab `<w:tab/>`, and tables are `<w:tbl>` of `<w:tr>`/`<w:tc>`.
That is all this needs, so it works on any box with Python and nothing installed — which matters,
because the reports arrive as .docx and the alternative was installing LibreOffice to read a text
file.

Images are ignored on purpose: they carry no text and would only bloat what the model reads.

    python3 tools/docx_text.py <file.docx> [-o out.txt] [--stats]

Tables are rendered one row per line with " | " between cells — enough to keep a report's structure
readable without pretending to reconstruct layout.
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _para_text(p: ET.Element) -> str:
    out = []
    for node in p.iter():
        tag = node.tag
        if tag == W + "t":
            out.append(node.text or "")
        elif tag == W + "tab":
            out.append("\t")
        elif tag in (W + "br", W + "cr"):
            out.append("\n")
    return "".join(out)


def _walk(body: ET.Element) -> list[str]:
    """Body children in document order, so paragraphs and tables stay interleaved as written."""
    lines: list[str] = []
    for child in body:
        if child.tag == W + "p":
            lines.append(_para_text(child))
        elif child.tag == W + "tbl":
            for row in child.findall(W + "tr"):
                cells = []
                for cell in row.findall(W + "tc"):
                    cells.append(" ".join(
                        _para_text(p).strip() for p in cell.findall(W + "p")).strip())
                lines.append(" | ".join(cells))
        elif child.tag == W + "sdt":                     # content control wrapper
            content = child.find(W + "sdtContent")
            if content is not None:
                lines.extend(_walk(content))
    return lines


def extract(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        names = [n for n in ("word/document.xml",) if n in z.namelist()]
        if not names:
            raise SystemExit(f"{path.name}: no word/document.xml — is this really a .docx?")
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(W + "body")
    if body is None:
        return ""
    text = "\n".join(_walk(body))
    text = text.replace(" ", " ")                    # non-breaking spaces read as junk later
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)                # collapse the empty-paragraph padding
    return text.strip() + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract text from .docx (stdlib only)")
    ap.add_argument("file")
    ap.add_argument("-o", "--output", help="write here instead of stdout")
    ap.add_argument("--stats", action="store_true", help="print size/line/char counts to stderr")
    a = ap.parse_args()

    src = Path(a.file)
    if not src.exists():
        raise SystemExit(f"no such file: {src}")
    text = extract(src)

    if a.stats:
        print(f"{src.name}: {src.stat().st_size/1024:.0f} KB → "
              f"{len(text.splitlines())} lines, {len(text)} chars "
              f"(~{len(text)//4} tokens)", file=sys.stderr)
    if a.output:
        Path(a.output).write_text(text)
        print(a.output)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
