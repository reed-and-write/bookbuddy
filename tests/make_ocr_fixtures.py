"""Generate the OCR test fixtures from the public-domain Alice text (tests/fixtures/alice.epub). Neutral metadata only.

    python tests/make_ocr_fixtures.py

Produces in tests/fixtures/: scanned_book.pdf (3 pages, image only, slightly rotated: running header, page numbers,
two chapter headings), mixed_book.pdf (page 1 and 3 real text, page 2 a picture of text), book_pages.cbz (the same
three pages as images) and book_pages.djvu (bilevel, if djvulibre is installed). Page images are 1-bit scans.
"""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from PIL import Image
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from make_pdf_fixtures import BOLD, FONT, H, LEAD, MX, SIZE, W, alice, wrap   # noqa: E402

DPI = 250
HEADER = "ADVENTURES IN A STRANGE LAND"
LINES_PER_PAGE = 36


def book_pages():
    """-> [(chapter heading or None, [lines], [paragraph texts on this page])] for exactly three pages."""
    chs = alice(2, 99)
    out = []
    for ci, (title, paras) in enumerate(chs):
        lines: list[str] = []
        owners: list[tuple[int, str]] = []
        for p in paras:
            for segs, _, ind in wrap(p, W - 2 * MX, indent=18, hyphenate=True):
                lines.append(("    " if ind else "") + "".join(s for s, _ in segs))
            lines.append("")
        pages = [lines[i:i + LINES_PER_PAGE - (4 if i == 0 else 0)] for i in range(0, len(lines), LINES_PER_PAGE)]
        out.append((f"CHAPTER {['I', 'II'][ci]}", title.upper(), pages))
    return out


def _to_paragraph_end(lines: list[str]) -> list[str]:
    """Cut at the last blank line, so a page never ends in the middle of a paragraph."""
    k = max(i for i, l in enumerate(lines) if not l)
    return lines[:k]


def layout():
    ch1, ch2 = book_pages()
    return [(ch1[0], ch1[1], _to_paragraph_end(ch1[2][0])), (None, None, _to_paragraph_end(ch1[2][0] + [""] + ch1[2][1])[len(_to_paragraph_end(ch1[2][0])) + 1:][:LINES_PER_PAGE]),
            (ch2[0], ch2[1], _to_paragraph_end(ch2[2][0]))]


def ground_truth() -> str:
    """The words on the three pages, in reading order, with hyphenated line ends joined."""
    import re
    text = []
    for _, _, lines in layout():
        text.extend(lines)
    t = "\n".join(text)
    t = re.sub(r"-\n\s*", "", t)
    return re.sub(r"\s+", " ", t).strip()


def draw_page(c: canvas.Canvas, n: int, head, title, lines):
    c.setFont(FONT, 9)
    if n > 1:
        c.drawCentredString(W / 2, H - 36, HEADER)
    y = H - 70
    if head:
        c.setFont(BOLD, 15)
        c.drawCentredString(W / 2, H - 110, head)
        c.setFont(BOLD, 12)
        c.drawCentredString(W / 2, H - 132, title)
        y = H - 170
    c.setFont(FONT, SIZE)
    for l in lines:
        if l:
            c.drawString(MX, y, l.replace("    ", "", 1)) if not l.startswith("    ") else c.drawString(MX + 18, y, l.strip())
        y -= LEAD
    c.setFont(FONT, 10)
    c.drawCentredString(W / 2, 30, str(n))


def text_pdf(path: Path, pages_wanted=(1, 2, 3)):
    c = canvas.Canvas(str(path), pagesize=(W, H))
    c.setTitle("Sample scan")
    c.setAuthor("Test Fixture")
    for n, (head, title, lines) in enumerate(layout(), 1):
        draw_page(c, n, head, title, lines)
        c.showPage()
    c.save()


def scan_images(src_pdf: Path, td: Path) -> list[Image.Image]:
    subprocess.run(["pdftoppm", "-r", str(DPI), "-gray", "-png", str(src_pdf), str(td / "pg")], check=True)
    imgs = []
    for i, f in enumerate(sorted(td.glob("pg*.png"))):
        im = Image.open(f).convert("L")
        im = im.rotate(0.6 if i % 2 == 0 else -0.5, resample=Image.BICUBIC, fillcolor=255, expand=False)
        imgs.append(im.point(lambda v: 255 if v > 150 else 0, mode="1"))
    return imgs


def save_pdf(imgs: list[Image.Image], path: Path):
    imgs[0].save(path, "PDF", resolution=DPI, save_all=True, append_images=imgs[1:], title="Sample scan", author="Test Fixture")


def main():
    FIX.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "text.pdf"
        text_pdf(src)
        imgs = scan_images(src, td)
        save_pdf(imgs, FIX / "scanned_book.pdf")
        # mixed: real text on pages 1 and 3, a picture of text on page 2
        from pypdf import PdfReader, PdfWriter
        save_pdf([imgs[1]], td / "p2.pdf")
        w = PdfWriter()
        w.add_page(PdfReader(str(src)).pages[0])
        w.add_page(PdfReader(str(td / "p2.pdf")).pages[0])
        w.add_page(PdfReader(str(src)).pages[2])
        w.add_metadata({"/Title": "Sample mixed", "/Author": "Test Fixture"})
        with open(FIX / "mixed_book.pdf", "wb") as f:
            w.write(f)
        with zipfile.ZipFile(FIX / "book_pages.cbz", "w", zipfile.ZIP_DEFLATED) as zf:
            for i, im in enumerate(imgs, 1):
                buf = io.BytesIO()
                im.save(buf, "PNG", dpi=(DPI, DPI))
                zf.writestr(f"page{i:02d}.png", buf.getvalue())
        try:
            for i, im in enumerate(imgs, 1):
                im.save(td / f"d{i}.pbm")
                subprocess.run(["cjb2", "-dpi", str(DPI), str(td / f"d{i}.pbm"), str(td / f"d{i}.djvu")], check=True, capture_output=True)
            subprocess.run(["djvm", "-c", str(FIX / "book_pages.djvu")] + [str(td / f"d{i}.djvu") for i in range(1, 4)],
                           check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as e:
            print("skipped DjVu:", e)
    for p in sorted(FIX.glob("*book*")) + [FIX / "book_pages.cbz"]:
        print(p.name, p.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
