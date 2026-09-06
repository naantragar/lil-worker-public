#!/usr/bin/env python3
"""Extract readable text from an office document — stdlib only, no python-docx, no pandoc.

A .docx is a zip whose `word/document.xml` holds the body. Paragraphs are `<w:p>`, runs of text are
`<w:t>`, a line break is `<w:br/>`, a tab `<w:tab/>`, and tables are `<w:tbl>` of `<w:tr>`/`<w:tc>`.
That is all this needs, so it works on any box with Python and nothing installed — which matters,
because the reports arrive as .docx and the alternative was installing LibreOffice to read a text
file.

Spreadsheets read through the same door (2026-09-03), because the bot now accepts them and the
first thing anybody wants is to SEE what is inside:
  * `.xlsx` / `.xlsm` — cell text lives in `xl/sharedStrings.xml`, not in the sheet, so unzipping
    the sheet XML alone yields rows of empty cells. Both files are read here and joined. A formula
    cell prints its cached value, which is what the sender saw on screen.
  * `.ods` — `content.xml` carries the text inline; repeat counts are expanded only where there is
    something to repeat.
Reading is stdlib-only on purpose (openpyxl works too, but is not installed in every venv here).
A .docm/.xlsm macro blob is never unzipped and never executed — only the text parts are read.

Images are ignored on purpose: they carry no text and would only bloat what the model reads.

    python3 tools/docx_text.py <file.docx|.xlsx|.ods> [-o out.txt] [--stats]

Tables — a Word table, a worksheet — are rendered one row per line with " | " between cells: enough
to keep the structure readable without pretending to reconstruct layout.
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


X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DOC_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
TABLE_NS = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
TEXT_NS = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
OFFICE_NS = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"

# A run of identical cells is expanded literally, so a sheet that declares 16384 repeated empties
# cannot turn one row into a megabyte of separators.
MAX_REPEAT = 64


def _col_index(ref: str) -> int:
    """'BC12' -> 54. The letters are base-26 with no zero; digits are the row and are ignored."""
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - 64)
    return max(n - 1, 0)


def _trim(cells: list[str]) -> list[str]:
    while cells and not cells[-1].strip():
        cells.pop()
    return cells


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    out = []
    for si in root:
        # A styled string is split into several <r><t> runs; joining them restores the cell.
        out.append("".join(t.text or "" for t in si.iter(X + "t")))
    return out


def _sheet_paths(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    """(sheet name, zip path) in workbook order — the order the sender sees as tabs."""
    names = z.namelist()
    if "xl/workbook.xml" not in names:
        return [(Path(n).stem, n) for n in sorted(names) if n.startswith("xl/worksheets/sheet")]
    rels = {}
    if "xl/_rels/workbook.xml.rels" in names:
        for rel in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
            target = rel.get("Target", "")
            rels[rel.get("Id")] = target[1:] if target.startswith("/") else "xl/" + target.lstrip("./")
    out = []
    for sheet in ET.fromstring(z.read("xl/workbook.xml")).iter(X + "sheet"):
        path = rels.get(sheet.get(DOC_REL + "id"))
        if path and path in names:
            out.append((sheet.get("name") or path, path))
    return out


def _extract_xlsx(path: Path) -> str:
    lines: list[str] = []
    with zipfile.ZipFile(path) as z:
        strings = _shared_strings(z)
        sheets = _sheet_paths(z)
        if not sheets:
            raise SystemExit(f"{path.name}: no worksheets — is this really a spreadsheet?")
        for name, sheet_path in sheets:
            rows: list[str] = []
            for row in ET.fromstring(z.read(sheet_path)).iter(X + "row"):
                cells: list[str] = []
                for c in row.findall(X + "c"):
                    idx = _col_index(c.get("r") or "")
                    while len(cells) < idx:
                        cells.append("")
                    kind = c.get("t")
                    if kind == "inlineStr":
                        val = "".join(t.text or "" for t in c.iter(X + "t"))
                    else:
                        v = c.find(X + "v")            # a formula cell keeps its cached value here
                        val = v.text or "" if v is not None else ""
                        if kind == "s" and val.isdigit():
                            val = strings[int(val)] if int(val) < len(strings) else ""
                    cells.append(" ".join(val.split()))
                cells = _trim(cells)
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                lines.append(f"== {name} ==")
                lines.extend(rows)
                lines.append("")
            else:
                lines.append(f"== {name} == (empty)")
    return "\n".join(lines)


def _ods_cell_text(cell: ET.Element) -> str:
    parts = [" ".join("".join(p.itertext()).split()) for p in cell.iter(TEXT_NS + "p")]
    return " ".join(part for part in parts if part).strip()


def _extract_ods(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        if "content.xml" not in z.namelist():
            raise SystemExit(f"{path.name}: no content.xml — is this really an .ods?")
        root = ET.fromstring(z.read("content.xml"))
    lines: list[str] = []
    for table in root.iter(TABLE_NS + "table"):
        rows: list[str] = []
        for row in table.iter(TABLE_NS + "table-row"):
            cells: list[str] = []
            for cell in row.findall(TABLE_NS + "table-cell"):
                text = _ods_cell_text(cell)
                repeat = int(cell.get(TABLE_NS + "number-columns-repeated") or 1)
                cells.extend([text] * min(repeat, MAX_REPEAT if text else 1))
            cells = _trim(cells)
            if cells:
                rows.append(" | ".join(cells))
        name = table.get(TABLE_NS + "name") or "sheet"
        if rows:
            lines.append(f"== {name} ==")
            lines.extend(rows)
            lines.append("")
        else:
            lines.append(f"== {name} == (empty)")
    return "\n".join(lines)


def _extract_odt(path: Path) -> str:
    """ODF text: paragraphs and headings are inline in content.xml, tables are the same shape as
    in an .ods, so a document-order walk of the body is the whole job."""
    with zipfile.ZipFile(path) as z:
        if "content.xml" not in z.namelist():
            raise SystemExit(f"{path.name}: no content.xml — is this really an .odt?")
        root = ET.fromstring(z.read("content.xml"))
    body = root.find(OFFICE_NS + "body")
    text_body = body.find(OFFICE_NS + "text") if body is not None else None
    if text_body is None:
        return ""
    lines: list[str] = []

    def walk(node: ET.Element) -> None:
        for child in node:
            if child.tag in (TEXT_NS + "p", TEXT_NS + "h"):
                lines.append(" ".join("".join(child.itertext()).split()))
            elif child.tag == TABLE_NS + "table":
                for row in child.iter(TABLE_NS + "table-row"):
                    cells = _trim([_ods_cell_text(c)
                                   for c in row.findall(TABLE_NS + "table-cell")])
                    if cells:
                        lines.append(" | ".join(cells))
            elif child.tag in (TEXT_NS + "list", TEXT_NS + "list-item", TEXT_NS + "section"):
                walk(child)

    walk(text_body)
    return "\n".join(lines)


def extract(path: Path) -> str:
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xltx"):
        return _extract_xlsx(path).strip() + "\n"
    if path.suffix.lower() in (".ods", ".fods"):
        return _extract_ods(path).strip() + "\n"
    if path.suffix.lower() in (".odt", ".ott"):
        return _extract_odt(path).strip() + "\n"
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
    ap = argparse.ArgumentParser(
        description="Extract text from .docx/.docm/.odt/.xlsx/.xlsm/.ods (stdlib only)")
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
