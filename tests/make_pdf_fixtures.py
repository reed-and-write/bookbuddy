"""Generate the small PDF test fixtures from the public-domain Alice text (tests/fixtures/alice.epub).

    python tests/make_pdf_fixtures.py

Produces in tests/fixtures/: sample_book.pdf (running heads, page numbers, hyphenated line breaks, footnotes,
"CHAPTER I" headings, title/copyright/contents/about pages), outline_book.pdf (bookmarks, headings are plain titles),
nochapters.pdf (continuous prose), scanned.pdf (image-only), twocol.pdf (two columns).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
sys.path.insert(0, str(HERE.parent))

W, H = 432, 648
MX = 54
FONT, BOLD, SIZE, LEAD = "Times-Roman", "Times-Bold", 11, 14
ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII"]


def alice(n_chapters: int, max_paras: int):
    from bookbuddy.extractors.epub import extract_epub
    b = extract_epub(str(FIX / "alice.epub"))
    out = []
    for ch in b.chapters[:n_chapters]:
        title = ch.title.split(". ", 1)[1]
        out.append((title, ch.paragraphs[:max_paras]))
    return out


def hyphen_split(word: str, room: float, font=FONT) -> tuple[str, str] | None:
    """Break a long word with a hyphen so that the first part fits in `room`."""
    if len(word) < 6 or not word[:3].isalpha():
        return None
    for k in range(len(word) - 3, 2, -1):
        if word[k - 1].isalpha() and word[k].isalpha() and stringWidth(word[:k] + "-", font, SIZE) <= room:
            return word[:k] + "-", word[k:]
    return None


def wrap(text: str, width: float, indent: float = 0.0, footnotes: dict | None = None, hyphenate: bool = True):
    """text -> list of lines; each line is [(segment, is_sup)]. `footnotes` maps word index -> footnote text."""
    words = text.split()
    lines, cur, used = [], [], indent
    notes_in_line: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        wd = stringWidth(w, FONT, SIZE) + (stringWidth(" ", FONT, SIZE) if cur else 0)
        if used + wd <= width:
            cur.append((w if not cur else " " + w, False))
            used += wd
            if footnotes and i in footnotes:
                cur.append((str(footnotes[i][0]), True))
                used += 4
                notes_in_line.append(footnotes[i])
            i += 1
            continue
        if hyphenate:
            sp = stringWidth(" ", FONT, SIZE) if cur else 0
            hs = hyphen_split(w, width - used - sp)
            if hs:
                cur.append((hs[0] if not cur else " " + hs[0], False))
                words[i] = hs[1]
                lines.append((cur, notes_in_line, indent))
                cur, used, notes_in_line, indent = [], 0, [], 0.0
                continue
        lines.append((cur, notes_in_line, indent))
        cur, used, notes_in_line, indent = [], 0, [], 0.0
    if cur:
        lines.append((cur, notes_in_line, indent))
    return lines


class Doc:
    def __init__(self, path: Path, title: str, author: str, running=True, bookmarks=False):
        self.c = canvas.Canvas(str(path), pagesize=(W, H))
        self.c.setTitle(title)
        self.c.setAuthor(author)
        self.title, self.author = title, author
        self.running, self.bookmarks = running, bookmarks
        self.pageno = 0           # printed body page number (0 = front matter, unnumbered)
        self.sheet = 0

    def new_page(self):
        self.c.showPage()

    def furniture(self, chapter_title: str | None, first: bool):
        c = self.c
        if self.running and not first and self.pageno:
            c.setFont(FONT, 9)
            if self.pageno % 2 == 0:
                c.drawCentredString(W / 2, H - 36, self.title.upper())
            else:
                c.drawCentredString(W / 2, H - 36, chapter_title or "")
        if self.pageno:
            c.setFont(FONT, 10)
            c.drawCentredString(W / 2, 30, str(self.pageno))

    def chapter(self, number: int, title: str, paras, caps_style=True, with_footnotes=True, hyphenate=True):
        c = self.c
        key = f"ch{number}"
        self.pageno += 1
        self.new_page()
        c.bookmarkPage(key)
        if self.bookmarks:
            c.addOutlineEntry(title, key, level=0)
        y = H - 190
        if caps_style:
            c.setFont(BOLD, 15)
            c.drawCentredString(W / 2, y, f"CHAPTER {ROMAN[number - 1]}")
            c.setFont(BOLD, 13)
            c.drawCentredString(W / 2, y - 24, title.upper())
        else:
            c.setFont(BOLD, 16)
            c.drawString(MX, y, title)
        y -= 70
        self.furniture(title, first=True)
        fn_counter = 0
        lines_all = []
        for pi, p in enumerate(paras):
            notes = None
            if with_footnotes and pi % 4 == 2 and len(p.split()) > 20:
                fn_counter += 1
                notes = {8: (fn_counter, f"Editorial note {fn_counter}: this passage is discussed at length by later commentators on the text.")}
            lines_all.extend(wrap(p, W - 2 * MX, indent=18, footnotes=notes, hyphenate=hyphenate))
        i = 0
        bottom = 60
        while i < len(lines_all):
            page_notes: list = []
            while i < len(lines_all):
                segs, notes, ind = lines_all[i]
                need = LEAD + (12 * len(notes) + (10 if notes and not page_notes else 0))
                reserve = (12 * len(page_notes) + (10 if page_notes else 0)) + need
                if y - LEAD < bottom + reserve - LEAD and i > 0 and y < H - 100:
                    break
                x = MX + ind
                for text, sup in segs:
                    if sup:
                        c.setFont(FONT, 7)
                        c.drawString(x, y + 4, text)
                        x += stringWidth(text, FONT, 7)
                    else:
                        c.setFont(FONT, SIZE)
                        c.drawString(x, y, text)
                        x += stringWidth(text, FONT, SIZE)
                page_notes.extend(notes)
                y -= LEAD
                i += 1
            if page_notes:
                fy = bottom + 12 * len(page_notes) + 2
                c.line(MX, fy + 8, MX + 60, fy + 8)
                c.setFont(FONT, 8)
                for n, txt in page_notes:
                    c.drawString(MX, fy - 4, f"{n} {txt}")
                    fy -= 12
            if i < len(lines_all):
                self.pageno += 1
                self.new_page()
                self.furniture(title, first=False)
                y = H - 70

    def front(self, with_contents: list[str] | None, copyright_page=True):
        c = self.c
        c.setFont(BOLD, 24)
        c.drawCentredString(W / 2, H - 250, self.title)
        c.setFont(FONT, 14)
        c.drawCentredString(W / 2, H - 290, self.author)
        if copyright_page:
            self.new_page()
            c.setFont(FONT, 9)
            y = H - 400
            for l in ["Copyright (c) 1865. All rights reserved.", "No part of this publication may be reproduced",
                      "without permission. Library of Congress Cataloging-in-Publication Data.", "ISBN 978-3-16-148410-0"]:
                c.drawString(MX, y, l)
                y -= 13
        if with_contents:
            self.new_page()
            c.setFont(BOLD, 14)
            c.drawCentredString(W / 2, H - 100, "CONTENTS")
            c.setFont(FONT, 11)
            y = H - 140
            for i, t in enumerate(with_contents, 1):
                label = f"Chapter {ROMAN[i - 1]}. {t}"
                num = str(i * 4 - 2)
                dots = "." * int((W - 2 * MX - stringWidth(label + num, FONT, 11)) / stringWidth(".", FONT, 11) - 2)
                c.drawString(MX, y, f"{label} {dots} {num}")
                y -= 18

    def back(self, heading: str, text: str):
        self.pageno += 1
        self.new_page()
        self.c.setFont(BOLD, 13)
        self.c.drawCentredString(W / 2, H - 150, heading)
        y = H - 190
        for line, _, _ in wrap(text, W - 2 * MX):
            x = MX
            for seg, _s in line:
                self.c.setFont(FONT, SIZE)
                self.c.drawString(x, y, seg)
                x += stringWidth(seg, FONT, SIZE)
            y -= LEAD
        self.furniture(None, first=True)

    def save(self):
        self.c.showPage()
        self.c.save()


def sample_book():
    chs = alice(3, 14)
    d = Doc(FIX / "sample_book.pdf", "Alice's Adventures in Wonderland", "Lewis Carroll")
    d.front([t for t, _ in chs])
    for i, (t, ps) in enumerate(chs, 1):
        d.chapter(i, t, ps)
    d.back("ABOUT THE AUTHOR", "Lewis Carroll was the pen name of Charles Lutwidge Dodgson, a mathematician, logician, "
           "photographer and writer who taught at Christ Church, Oxford, and told the first version of this story on a boat trip.")
    d.save()


def outline_book():
    chs = alice(4, 8)
    d = Doc(FIX / "outline_book.pdf", "The Garden Tales", "A. Gardener", running=True, bookmarks=True)
    d.front(None, copyright_page=True)
    for i, (t, ps) in enumerate(chs, 1):
        d.chapter(i, t, ps, caps_style=False, with_footnotes=False)
    d.save()


def nochapters():
    chs = alice(12, 40)
    text = [p for _, ps in chs[3:] for p in ps]
    d = Doc(FIX / "nochapters.pdf", "A Long Afternoon", "Anonymous", running=False)
    c = d.c
    lines = []
    for p in text:
        lines.extend(wrap(p, W - 2 * MX, indent=18))
    y = H - 70
    d.pageno = 1
    for segs, _, ind in lines:
        if y < 60:
            d.furniture(None, True)
            c.showPage()
            d.pageno += 1
            y = H - 70
        x = MX + ind
        for seg, _s in segs:
            c.setFont(FONT, SIZE)
            c.drawString(x, y, seg)
            x += stringWidth(seg, FONT, SIZE)
        y -= LEAD
    d.furniture(None, True)
    d.save()


def twocol():
    chs = alice(2, 10)
    c = canvas.Canvas(str(FIX / "twocol.pdf"), pagesize=(612, 792))
    c.setTitle("Two Column Study")
    colw = 240
    cols = [54, 54 + colw + 24]
    lines = []
    for _, ps in chs:
        for p in ps:
            lines.extend(wrap(p, colw, indent=14))
    ci, y = 0, 792 - 70
    page = 1
    c.setFont(BOLD, 14)
    c.drawString(54, 792 - 40, "Two Column Study")
    for segs, _, ind in lines:
        if y < 70:
            ci += 1
            y = 792 - 70
            if ci == 2:
                c.setFont(FONT, 10)
                c.drawCentredString(306, 30, str(page))
                c.showPage()
                page += 1
                ci = 0
        x = cols[ci] + ind
        for seg, _s in segs:
            c.setFont(FONT, SIZE)
            c.drawString(x, y, seg)
            x += stringWidth(seg, FONT, SIZE)
        y -= LEAD
    c.setFont(FONT, 10)
    c.drawCentredString(306, 30, str(page))
    c.showPage()
    c.save()


def scanned():
    """Image-only PDF: rasterise two text pages and embed the pictures."""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src.pdf"
        d = Doc(src, "Scan", "Nobody", running=False)
        chs = alice(1, 6)
        d.chapter(1, chs[0][0], chs[0][1], with_footnotes=False)
        d.save()
        subprocess.run(["pdftoppm", "-r", "60", "-gray", "-png", str(src), str(Path(td) / "pg")], check=True)
        imgs = sorted(Path(td).glob("pg*.png"))[:2]
        c = canvas.Canvas(str(FIX / "scanned.pdf"), pagesize=(W, H))
        for im in imgs:
            c.drawImage(ImageReader(str(im)), 0, 0, W, H)
            c.showPage()
        c.save()


if __name__ == "__main__":
    FIX.mkdir(exist_ok=True)
    sample_book(); outline_book(); nochapters(); twocol(); scanned()
    for p in sorted(FIX.glob("*.pdf")):
        print(p.name, p.stat().st_size // 1024, "KB")
