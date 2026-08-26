#!/usr/bin/env python3
"""Write a .docx from plain text — stdlib only, no python-docx.

A .docx is a zip of XML, so producing one needs nothing installed. This is the counterpart of
`tools/docx_text.py` (which reads them), and it is the seed of the report export the analyst
actually needs: their workflow ends in Word, not in a terminal.

The layout is not invented: it is copied out of the analyst's own report (`РЕР 21.08.2026.docx`,
2026-08-22) by reading its `word/document.xml` — Times New Roman 14 pt, single line spacing, zero
space after a paragraph, no indents, A4 with the Ukrainian office margins, network headers bold.
A generated report has to be droppable into their workflow without reformatting, so these values are
the standard, not defaults.

    python3 tools/text_to_docx.py report.txt -o report.docx [--title "Звіт за 21.08.2026"]
"""
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

# Measured on the analyst's own file. Sizes are half-points (28 = 14 pt), lengths are twips
# (1134 = 2 cm). line=240 + lineRule=auto is exactly single spacing.
FONT = "Times New Roman"
SIZE = 28
SPACING = '<w:spacing w:after="0" w:line="240" w:lineRule="auto"/>'
SECT = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="850" w:bottom="1134" w:left="1701"'
        ' w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>')


def _rpr(bold: bool, size: int = SIZE) -> str:
    b = "<w:b/><w:bCs/>" if bold else ""
    return (f'<w:rPr><w:rFonts w:ascii="{FONT}" w:hAnsi="{FONT}" w:cs="{FONT}"/>{b}'
            f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/></w:rPr>')


def _para(text: str, bold: bool = False, size: int = SIZE, center: bool = False) -> str:
    rpr = _rpr(bold, size)
    jc = '<w:jc w:val="center"/>' if center else ""
    ppr = f"<w:pPr>{jc}{SPACING}{rpr}</w:pPr>"
    if not text.strip():
        return f"<w:p>{ppr}</w:p>"
    return (f"<w:p>{ppr}<w:r>{rpr}"
            f'<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>')


def build(content, title: str | None = None) -> bytes:
    """`content` is either plain text — headers detected by their shape — or an explicit list of
    (text, bold, center) triples when the caller wants to control the layout itself."""
    body = []
    if title:
        body.append(_para(title, bold=True, center=True))
        body.append(_para(""))
    if isinstance(content, str):
        for line in content.split("\n"):
            # A network header line is the only structure worth marking: it starts a section and
            # never begins with a timestamp the way an event line does.
            is_header = (line.strip().startswith(("УКХ", "КХ", "(без шапки"))
                         or line.strip().startswith("###"))
            body.append(_para(line.rstrip(), bold=is_header))
    else:
        for text, bold, center in content:
            body.append(_para(str(text).rstrip(), bold=bold, center=center))
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           f"<w:document {W}><w:body>{''.join(body)}{SECT}</w:body></w:document>")
    return doc.encode()


# Every zip entry carries a timestamp and a "made on which OS" byte, and `writestr` with a plain
# string name fills both from the machine: our files were shipping (2026, 8, 25, 14, 18, 32) and
# create_system=3 (Unix) on all three parts. The document itself has no metadata to leak — this
# writer builds the package by hand and never emits docProps/core.xml or docProps/app.xml, so there
# is no author, no company, no "created with" and no revision count anywhere. These three constants
# close the last of it: the epoch zip uses for "no date", the Windows/FAT origin byte, and no
# permission bits. Two files with the same text are now byte-identical.
_EPOCH = (1980, 1, 1, 0, 0, 0)


def _entry(name: str) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, date_time=_EPOCH)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.create_system = 0
    zi.external_attr = 0
    return zi


def _scrub_attrs(dest: Path) -> None:
    """Zero the external-attributes field of every entry, which the library will not let us leave
    empty: `_open_to_write` treats `external_attr == 0` as "unset" and substitutes `0o600 << 16`, so
    the one value we actually want is the one value unreachable through the API. It is only a Unix
    permission mask — it identifies nobody — but "empty" was the requirement, so it is emptied here.

    The central directory is walked from the end-of-central-directory record rather than by scanning
    for the `PK\\x01\\x02` signature, because that byte sequence can also occur inside compressed
    data; EOCD gives the true offset and entry count. No comment is ever written, so EOCD is the
    final 22 bytes.
    """
    raw = bytearray(dest.read_bytes())
    if len(raw) < 22 or raw[-22:-18] != b"PK\x05\x06":
        return                                    # unexpected shape — leave the file alone
    count = int.from_bytes(raw[-14:-12], "little")
    pos = int.from_bytes(raw[-6:-2], "little")    # offset of the central directory
    for _ in range(count):
        if raw[pos:pos + 4] != b"PK\x01\x02":
            return
        raw[pos + 38:pos + 42] = b"\x00\x00\x00\x00"
        n = int.from_bytes(raw[pos + 28:pos + 30], "little")
        extra = int.from_bytes(raw[pos + 30:pos + 32], "little")
        comment = int.from_bytes(raw[pos + 32:pos + 34], "little")
        pos += 46 + n + extra + comment
    dest.write_bytes(bytes(raw))


def write(content, dest: Path, title: str | None = None) -> Path:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(_entry("[Content_Types].xml"), CONTENT_TYPES)
        z.writestr(_entry("_rels/.rels"), RELS)
        z.writestr(_entry("word/document.xml"), build(content, title))
    _scrub_attrs(dest)
    return dest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--title")
    a = ap.parse_args()
    out = write(Path(a.source).read_text(), Path(a.output), a.title)
    print(f"{out}  ({out.stat().st_size/1024:.0f} КБ)")


if __name__ == "__main__":
    main()
