#!/usr/bin/env python3
"""Render an office document to PNG pages so it can actually be LOOKED at.

Reading a .xlsx with openpyxl tells you what is in the cells; it tells you nothing about
whether a label is clipped, a column is too narrow or a row too short.  This converts the
document with headless LibreOffice and rasterises the pages, so the result can be opened
with the image tools instead of guessed at from font metrics.

    python3 tools/office_preview.py <file> [-o OUTDIR] [--pages 1-3] [--dpi 130]
    python3 tools/office_preview.py <file.xlsx> --fit-width      # one page wide
    python3 tools/office_preview.py <file.xlsx> --range A1:M40   # just that block

Works for .xlsx .xlsm .ods .docx .odt .doc .rtf .csv .pptx.  Prints the PNG paths it
produced, one per line, last.

Fonts: Microsoft fonts are not installed (not redistributable); the metric-compatible
substitutes are — Times New Roman -> Liberation Serif, Arial -> Liberation Sans,
Calibri -> Carlito, Cambria -> Caladea.  Character widths and therefore line breaks and
column fit match; the glyph shapes differ slightly from a real Office render.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")
PDFTOPPM = shutil.which("pdftoppm")

SPREADSHEET = {".xlsx", ".xlsm", ".xls", ".ods", ".csv"}


def die(msg, hint=""):
    print(f"office_preview: {msg}", file=sys.stderr)
    if hint:
        print(f"  {hint}", file=sys.stderr)
    raise SystemExit(2)


def prepare_spreadsheet(src, workdir, cell_range=None, fit_width=False, sheet=None):
    """Copy the sheet and set print options so the PDF shows what we want to see.

    LibreOffice prints a spreadsheet the way Excel would: sliced into paper pages.  For
    a wide sheet that scatters the table over a dozen pages.  Scaling it to one page
    wide (or printing only a chosen range) is what makes the render readable.
    """
    if not (cell_range or fit_width or sheet):
        return src
    try:
        import openpyxl
    except ImportError:
        die("openpyxl is needed for --range/--fit-width")

    out = workdir / ("prepared" + src.suffix)
    wb = openpyxl.load_workbook(src)
    if sheet is not None:
        # LibreOffice renders every sheet; drop the others so the pages are the ones asked
        # for. Cross-sheet formulas would break, so this is a preview copy only.
        keep = wb[sheet] if sheet in wb.sheetnames else wb.worksheets[int(sheet) - 1]
        for other in [w for w in wb.worksheets if w is not keep]:
            wb.remove(other)
    ws = wb.worksheets[0]
    if cell_range:
        ws.print_area = cell_range
    if fit_width:
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
    wb.save(out)
    return out


def to_pdf(src, workdir):
    profile = workdir / "profile"
    cmd = [SOFFICE, "--headless", "--norestore", "--nolockcheck",
           f"-env:UserInstallation=file://{profile}",
           "--convert-to", "pdf", "--outdir", str(workdir), str(src)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    pdf = workdir / (src.stem + ".pdf")
    if not pdf.exists():
        die("LibreOffice produced no PDF",
            (proc.stdout + proc.stderr).strip()[:400])
    return pdf


def to_png(pdf, outdir, stem, dpi, pages):
    prefix = outdir / stem
    cmd = [PDFTOPPM, "-png", "-r", str(dpi)]
    if pages:
        first, _, last = pages.partition("-")
        cmd += ["-f", first, "-l", last or first]
    cmd += [str(pdf), str(prefix)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    return sorted(outdir.glob(stem + "-*.png"))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("-o", "--outdir", help="where to put the PNGs (default: alongside)")
    ap.add_argument("--pages", help="page range, e.g. 1 or 1-3 (default: all)")
    ap.add_argument("--dpi", type=int, default=130)
    ap.add_argument("--range", dest="cell_range",
                    help="spreadsheets: print only this range, e.g. A1:M40")
    ap.add_argument("--fit-width", action="store_true",
                    help="spreadsheets: scale to one page wide")
    ap.add_argument("--sheet",
                    help="spreadsheets: render only this sheet (name or 1-based number)")
    ap.add_argument("--keep-pdf", action="store_true")
    args = ap.parse_args()

    if not SOFFICE:
        die("LibreOffice is not installed",
            "apt-get install -y --no-install-recommends libreoffice-calc")
    if not PDFTOPPM:
        die("pdftoppm is not installed", "apt-get install -y poppler-utils")

    src = Path(args.file).expanduser().resolve()
    if not src.is_file():
        die(f"no such file: {src}")
    outdir = Path(args.outdir).expanduser().resolve() if args.outdir else src.parent
    outdir.mkdir(parents=True, exist_ok=True)

    # LibreOffice chokes on some non-ASCII names in a headless profile; work on a copy
    # with a plain name and rename the results afterwards.
    with tempfile.TemporaryDirectory(prefix="office_preview_") as tmp:
        workdir = Path(tmp)
        staged = workdir / ("doc" + src.suffix)
        shutil.copy2(src, staged)
        if src.suffix.lower() in SPREADSHEET:
            staged = prepare_spreadsheet(staged, workdir, args.cell_range,
                                         args.fit_width, args.sheet)
        pdf = to_pdf(staged, workdir)
        stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in src.stem)[:40]
        pngs = to_png(pdf, outdir, stem + "_preview", args.dpi, args.pages)
        if args.keep_pdf:
            shutil.copy2(pdf, outdir / (stem + "_preview.pdf"))
            print(f"pdf: {outdir / (stem + '_preview.pdf')}")

    if not pngs:
        die("no pages were rendered")
    print(f"{len(pngs)} page(s) at {args.dpi} dpi:")
    for p in pngs:
        print(p)


if __name__ == "__main__":
    main()
