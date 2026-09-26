#!/usr/bin/env python3
"""Put the owner's own footer block into a finished report, in place of the `---` marker.

    python3 tools/analytics2/attach_footer.py REPORT.docx FOOTER.docx [-o OUT.docx]

The report ends with five blank lines and `---`, a landing strip for a block the owner keeps
elsewhere and pastes in by hand (see `build_tail` in run.py). This does that paste for him without
opening Word.

The footer is not plain text — it carries a real Word table — so it is spliced as XML rather than
re-typed. Four things have to be repaired on the way, and all four are why this is a script and not
a one-liner:

  * the table hangs off a style (`aff2`) that lives in the footer file's styles.xml, which our report
    does not have; without it the borders vanish. The style reference is replaced with explicit
    borders so the table carries its own appearance;
  * line spacing has to be stated on every paragraph. Our report has no styles.xml at all, so a
    paragraph that does not say otherwise inherits Word's built-in defaults (8 pt after, 1.08 line)
    — and the block itself carries `w:line="480"` in places. Both are invisible in the source file
    and both inflate the pasted block: taller rows, wider gaps. See `_tighten`;
  * its columns are sized for the landscape sheet it was cut from (5 x 4501 twips = 22505) and do
    not fit our portrait A4. Rescaling them was the first attempt and it looked wrong — a full-width
    table became a narrow one. Instead the document is split into two sections: the report keeps its
    own page, the footer gets the landscape page it belongs to, and the table is untouched;
  * the report's own page setup, fonts and margins must survive untouched — only the `---` paragraph
    is removed and only the footer's body content is inserted.
"""
from __future__ import annotations

import argparse
import re
import shutil
import zipfile
from pathlib import Path

BORDERS = ('<w:tblBorders>'
           + "".join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
                     for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
           + '</w:tblBorders>')


def _body(xml: str) -> str:
    """Everything between <w:body> and the closing section properties."""
    start = xml.index("<w:body>") + len("<w:body>")
    end = xml.rindex("<w:sectPr")
    return xml[start:end]


def _sect(xml: str) -> str:
    """The document's final section properties — page size, orientation, margins."""
    i = xml.rindex("<w:sectPr")
    j = xml.index("</w:sectPr>", i) + len("</w:sectPr>")
    return re.sub(r'\s+w:rsid\w*="[^"]*"', "", xml[i:j])


# Word litters its XML with revision ids from namespaces our minimal document root never declares
# (`w14:paraId`, `w15:*`, `mc:*`). Pasted across, they make the file unparseable — "unbound prefix".
# They carry no formatting, so they are dropped rather than declared.
RE_FOREIGN = re.compile(r'\s+(?:w14|w15|w16[a-z]*|mc|wp14|wps|wpg|a14|o|v)\:[\w:-]+="[^"]*"')
RE_FOREIGN_TAG = re.compile(r'</?(?:w14|w15|w16[a-z]*|mc|wp14|wps|wpg|a14)\:[^>]*>')


def _sanitize(frag: str) -> str:
    return RE_FOREIGN_TAG.sub("", RE_FOREIGN.sub("", frag))


# Single spacing, no space before or after. Forced on EVERY paragraph of the block, for two reasons.
# Our report has no styles.xml, so any paragraph that does not state its spacing inherits Word's
# built-in defaults — 8 pt after and 1.08 line — which quietly inflated the table rows and the gaps.
# And the source block itself carries `w:line="480"` (double) on some paragraphs, which is invisible
# in its own file but stands out here. Stating it everywhere removes the dependence on defaults.
SPACING = '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/>'
RE_P_OPEN = re.compile(r"<w:p(?:\s[^>]*)?>")


def _tighten(frag: str) -> str:
    frag = re.sub(r'<w:spacing[^/>]*/>', "", frag)
    frag = re.sub(r'\s+w:rsid\w*="[^"]*"', "", frag)
    frag = frag.replace("<w:pPr>", "<w:pPr>" + SPACING)

    def add(m: re.Match) -> str:
        tail = frag[m.end():m.end() + 8]
        return m.group(0) if tail.startswith("<w:pPr>") else m.group(0) + f"<w:pPr>{SPACING}</w:pPr>"

    return RE_P_OPEN.sub(add, frag)


def _drop_stray_number(frag: str) -> str:
    """Drop a paragraph that is nothing but a bare number, above the table.

    The owner's footer carries a stray `1` between the "за перiод …" line and the table — a leftover
    of whatever produces the block, not something he types. He asked for it gone (12.09.2026), and it
    arrives with every footer, so it is removed here rather than by hand each day.

    Deliberately narrow: only paragraphs whose ENTIRE text is 1-3 digits, and only ABOVE the first
    table. A number standing alone on its own line there carries no meaning; inside the table the
    same digits are the statistics themselves and are never touched.
    """
    cut = frag.find("<w:tbl")
    if cut < 0:
        return frag
    head, tail = frag[:cut], frag[cut:]

    def strip(m: re.Match) -> str:
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", m.group(0), re.S)).strip()
        return "" if re.fullmatch(r"\d{1,3}", text) else m.group(0)

    return re.sub(r"<w:p\b.*?</w:p>", strip, head, flags=re.S) + tail


def _prepare(frag: str) -> str:
    """Make the block stand on its own, changing nothing about how it looks.

    Column widths are left EXACTLY as the owner's file has them. Rescaling them to our portrait page
    was the first attempt and it was wrong: the block is cut from a landscape sheet, and squeezing it
    into A4 turned a full-width table into a narrow one with the text bunched in the middle. The
    block keeps its own geometry and gets its own page instead — see the two-section split in main().

    The only thing replaced is the table style reference, which points into the source file's
    styles.xml and would leave the table borderless here; explicit borders draw the same frame.
    """
    frag = _tighten(_sanitize(frag))
    frag = _drop_stray_number(frag)
    frag = re.sub(r'<w:tblStyle[^/]*/>', "", frag)
    frag = frag.replace("<w:tblPr>", "<w:tblPr>" + BORDERS, 1)
    # One blank line between the "Статистика…" heading and the table. The source block runs them
    # together; the owner asked for a single empty line there, as between the title and the heading.
    blank = f"<w:p><w:pPr>{SPACING}</w:pPr></w:p>"
    return frag.replace("<w:tbl>", blank + "<w:tbl>", 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("footer")
    ap.add_argument("-o", "--output")
    a = ap.parse_args()

    rep_path, out_path = Path(a.report), Path(a.output or a.report)
    with zipfile.ZipFile(rep_path) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    doc = parts["word/document.xml"].decode()
    with zipfile.ZipFile(a.footer) as z:
        foot = z.read("word/document.xml").decode()

    # Remove the `---` paragraph — the marker the footer replaces.
    marker = re.search(r'<w:p\b(?:(?!</w:p>).)*<w:t[^>]*>---</w:t>.*?</w:p>', doc, re.S)
    if not marker:
        raise SystemExit("у звіті немає рядка `---` - нема що замінювати")
    before, after = doc[:marker.start()], doc[marker.end():]

    # Two sections. Everything up to here keeps the report's own page (portrait A4) by carrying its
    # sectPr on a paragraph of its own; the footer then gets the page it was cut from — landscape,
    # with the source's margins — as the document's final section. That is what makes the table come
    # out full-width and identical to the file the owner pastes from, instead of squeezed.
    body_before = _body(doc)
    frag = _prepare(_body(foot))
    keep = f'<w:p><w:pPr>{_sect(doc)}</w:pPr></w:p>'
    # The tail after the marker holds the document's own final sectPr; swap it for the footer's, so
    # the last section is the landscape sheet the block belongs on.
    i = after.rindex("<w:sectPr")
    j = after.index("</w:sectPr>", i) + len("</w:sectPr>")
    after = after[:i] + _sect(foot) + after[j:]
    doc = before + keep + frag + after

    # The report itself must come through untouched: everything except the marker paragraph.
    kept = _body(doc).replace(frag, "").replace(keep, "")
    if kept != body_before.replace(marker.group(0), ""):
        raise SystemExit("тіло звіту змінилося - зупиняюсь")

    parts["word/document.xml"] = doc.encode()
    tmp = out_path.with_suffix(".tmp.docx")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.create_system = 0
            z.writestr(zi, data)
    shutil.move(tmp, out_path)
    try:                                   # keep the empty-metadata guarantee of the writer
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from text_to_docx import _scrub_attrs
        _scrub_attrs(out_path)
    except Exception:
        pass
    print(f"{out_path}  ({out_path.stat().st_size/1024:.0f} КБ)")


if __name__ == "__main__":
    main()
