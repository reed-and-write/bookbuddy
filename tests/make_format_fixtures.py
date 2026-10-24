"""Build the tiny per-format fixtures in tests/fixtures/ (public-domain text from Alice's Adventures in Wonderland).

    python tests/make_format_fixtures.py

Needs the dev extras: python-docx, odfpy, reportlab. Every fixture holds the same three chapters so the tests can
compare formats. Run it again to rebuild; the output is deterministic apart from zip timestamps.
"""
from __future__ import annotations

import base64
import io
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
OUT = HERE / "fixtures"

TITLE, AUTHOR = "Alice's Adventures in Wonderland", "Lewis Carroll"

CHAPTERS = [
    ("Down the Rabbit-Hole", [
        "Alice was beginning to get very tired of sitting by her sister on the bank, and of having nothing to do: "
        "once or twice she had peeped into the book her sister was reading, but it had no pictures or conversations in it, "
        "“and what is the use of a book,” thought Alice “without pictures or conversations?”",
        "So she was considering in her own mind whether the pleasure of making a daisy-chain would be worth the trouble "
        "of getting up and picking the daisies, when suddenly a White Rabbit with pink eyes ran close by her."]),
    ("The Pool of Tears", [
        "“Curiouser and curiouser!” cried Alice (she was so much surprised, that for the moment she quite forgot "
        "how to speak good English); “now I’m opening out like the largest telescope that ever was! Good-bye, feet!”",
        "Just then her head struck against the roof of the hall: in fact she was now rather more than nine feet high, "
        "and she at once took up the little golden key and hurried off to the garden door."]),
    ("A Caucus-Race and a Long Tale", [
        "They were indeed a queer-looking party that assembled on the bank—the birds with draggled feathers, "
        "the animals with their fur clinging close to them, and all dripping wet, cross, and uncomfortable.",
        "The first question of course was, how to get dry again: they had a consultation about this, and after a few "
        "minutes it seemed quite natural to Alice to find herself talking familiarly with them."]),
]

# a Windows-1252 file must not be valid UTF-8 and must keep its curly quotes
CP1252_TEXT = "\n\n".join([f"CHAPTER {r}. {t}\n\n" + "\n\n".join(ps) for r, (t, ps) in zip(("I", "II", "III"), CHAPTERS)])


def wrap(text: str, width: int = 70) -> str:
    import textwrap
    return "\n".join(textwrap.wrap(text, width))


def gutenberg_txt() -> str:
    out = [f"The Project Gutenberg eBook of {TITLE}", "",
           "This eBook is for the use of anyone anywhere in the United States and most other parts of the world at no cost.", "",
           f"Title: {TITLE}", "", f"Author: {AUTHOR}", "", "Release date: June 25, 2008 [eBook #11]", "", "Language: English", "",
           f"*** START OF THE PROJECT GUTENBERG EBOOK {TITLE.upper()} ***", "", "", f"{TITLE}", "", f"by {AUTHOR}", "", "",
           "CONTENTS", "", " CHAPTER I.     Down the Rabbit-Hole", " CHAPTER II.    The Pool of Tears",
           " CHAPTER III.   A Caucus-Race and a Long Tale", "", ""]
    for roman, (title, paras) in zip(("I", "II", "III"), CHAPTERS):
        out += [f"CHAPTER {roman}.", title, ""]
        for p in paras:
            out += [wrap(p), ""]
        out.append("")
    out += ["", "", "*** END OF THE PROJECT GUTENBERG EBOOK ALICE'S ADVENTURES IN WONDERLAND ***", "",
            "Updated editions will replace the previous one--the old editions will be renamed.", ""]
    return "\n".join(out)


def markdown() -> str:
    out = ["---", f'title: "{TITLE}"', "author: Lewis Carroll", "---", "", f"# {TITLE}", ""]
    for i, (title, paras) in enumerate(CHAPTERS):
        out += [f"## {title}", ""]
        out += [paras[0].replace("Alice", "**Alice**", 1) + " See the [original text](https://www.gutenberg.org/ebooks/11).", ""]
        out += ["![an illustration](alice.png)", ""]
        if i == 0:
            out += ["```python", "print('not for the ears')", "```", "",
                    "| Name | Role |", "| --- | --- |", "| Alice | Heroine |", ""]
        out += [paras[1] + "[^1]", ""]
    out += ["[^1]: A footnote that should never be read aloud.", ""]
    return "\n".join(out)


def html_page() -> str:
    body = "".join(f"<h2>{t}</h2>" + "".join(f"<p>{p}</p>" for p in ps) for t, ps in CHAPTERS)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{TITLE} | Example Library</title>
<meta name="author" content="{AUTHOR}"><style>body{{font-family:serif}}</style>
<script>var tracking = "SCRIPT-NOISE";</script></head>
<body>
<header><nav><a href="/">Home</a> <a href="/books">Books</a> NAV-NOISE</nav></header>
<div class="cookie-banner">We use cookies. COOKIE-NOISE</div>
<main><article><h1>{TITLE}</h1>{body}</article></main>
<aside class="sidebar"><h3>Related</h3><p>SIDEBAR-NOISE read more books</p></aside>
<footer><p>Copyright Example Library. FOOTER-NOISE</p></footer>
</body></html>"""


def make_docx(path: Path) -> None:
    import docx
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches
    from helpers import _png

    d = docx.Document()
    d.core_properties.title, d.core_properties.author = TITLE, AUTHOR
    d.sections[0].header.paragraphs[0].text = "RUNNING-HEADER-NOISE"
    d.sections[0].footer.paragraphs[0].text = "RUNNING-FOOTER-NOISE"
    d.add_picture(io.BytesIO(_png(20, 30)), width=Inches(2), height=Inches(3))     # portrait image before any text = cover
    d.add_paragraph(TITLE, style="Title")
    for i, (title, paras) in enumerate(CHAPTERS):
        d.add_heading(title, level=1)
        p = d.add_paragraph(paras[0])
        if i == 0:                                          # a footnote reference after the first paragraph
            run = p.add_run()
            ref = OxmlElement("w:footnoteReference")
            ref.set(qn("w:id"), "1")
            run._r.append(ref)
            t = d.add_table(rows=2, cols=2)
            for r, row in enumerate((("Name", "Role"), ("TABLE-NOISE", "Heroine"))):
                for c, val in enumerate(row):
                    t.cell(r, c).text = val
        d.add_paragraph(paras[1])
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:footnote w:id="1"><w:p><w:r><w:t>FOOTNOTE-NOISE</w:t></w:r></w:p></w:footnote></w:footnotes>').encode()
    part = Part(PackURI("/word/footnotes.xml"), "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
                xml, d.part.package)
    d.part.relate_to(part, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes")
    d.save(str(path))


def make_odt(path: Path) -> None:
    from odf.opendocument import OpenDocumentText
    from odf.table import Table, TableCell, TableRow
    from odf.text import H, P, Note, NoteBody, NoteCitation
    from odf.dc import Title
    from odf.meta import InitialCreator

    d = OpenDocumentText()
    d.meta.addElement(Title(text=TITLE))
    d.meta.addElement(InitialCreator(text=AUTHOR))
    for i, (title, paras) in enumerate(CHAPTERS):
        d.text.addElement(H(outlinelevel=1, text=title))
        p = P(text=paras[0])
        if i == 0:
            note = Note(id="ftn1", noteclass="footnote")
            note.addElement(NoteCitation(text="1"))
            body = NoteBody()
            body.addElement(P(text="FOOTNOTE-NOISE"))
            note.addElement(body)
            p.addElement(note)
        d.text.addElement(p)
        if i == 0:
            t = Table(name="t1")
            row = TableRow()
            cell = TableCell()
            cell.addElement(P(text="TABLE-NOISE"))
            row.addElement(cell)
            t.addElement(row)
            d.text.addElement(t)
        d.text.addElement(P(text=paras[1]))
    d.save(str(path))


def rtf() -> str:
    def esc(s: str) -> str:
        return "".join(c if ord(c) < 128 else "\\u%d?" % (ord(c) if ord(c) < 32768 else ord(c) - 65536) for c in s)
    out = ["{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0 Times New Roman;}}",
           "{\\info{\\title " + esc(TITLE) + "}{\\author " + AUTHOR + "}}", "\\f0\\fs24"]
    for roman, (title, paras) in zip(("I", "II", "III"), CHAPTERS):
        out.append("\\pard\\b CHAPTER %s. %s\\b0\\par" % (roman, esc(title)))
        out.append("\\pard\\par")
        for p in paras:
            out.append("\\pard " + esc(p) + "\\par")
            out.append("\\pard\\par")
    out.append("}")
    return "\n".join(out)


def fb2(with_cover: bool = True) -> str:
    from helpers import _png
    sections = "".join(
        f"<section><title><p>{t}</p></title>" + "".join(f"<p>{p}</p>" for p in ps) +
        ("<p>See<a l:href=\"#n1\" type=\"note\">[1]</a> the note.</p>" if i == 0 else "") + "</section>"
        for i, (t, ps) in enumerate(CHAPTERS))
    binary = base64.b64encode(_png(20, 30)).decode()
    return f"""<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
<description><title-info><genre>child_tale</genre>
<author><first-name>Lewis</first-name><last-name>Carroll</last-name></author>
<book-title>{TITLE}</book-title><lang>en</lang>
<coverpage><image l:href="#cover.png"/></coverpage></title-info></description>
<body><title><p>{TITLE}</p></title>{sections}</body>
<body name="notes"><section id="n1"><title><p>1</p></title><p>FOOTNOTE-NOISE</p></section></body>
<binary id="cover.png" content-type="image/png">{binary}</binary>
</FictionBook>"""


def make_epubs() -> None:
    from helpers import make_epub
    chapters = [(t, ps) for t, ps in CHAPTERS]
    plain = OUT / "alice_formats.epub"
    make_epub(plain, chapters, title=TITLE, author=AUTHOR, front=False, cover=False)

    def with_encryption(src: Path, dest: Path, xml: str, extra: dict[str, bytes] | None = None) -> None:
        with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zo:
            for item in zi.infolist():
                zo.writestr(item, zi.read(item.filename))
            zo.writestr("META-INF/encryption.xml", xml)
            for name, data in (extra or {}).items():
                zo.writestr(name, data)

    head = ('<?xml version="1.0"?><encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
            'xmlns:enc="http://www.w3.org/2001/04/xmlenc#">')
    def entry(algo: str, uri: str) -> str:
        return (f'<enc:EncryptedData><enc:EncryptionMethod Algorithm="{algo}"/><enc:CipherData>'
                f'<enc:CipherReference URI="{uri}"/></enc:CipherData></enc:EncryptedData>')
    with_encryption(plain, OUT / "drm_encrypted.epub",
                    head + entry("http://www.w3.org/2001/04/xmlenc#aes128-cbc", "ch0.xhtml") + "</encryption>")
    with_encryption(plain, OUT / "fonts_obfuscated.epub",
                    head + entry("http://www.idpf.org/2008/embedding", "fonts/Serif.otf")
                    + entry("http://ns.adobe.com/pdf/enc#RC", "fonts/Sans.ttf") + "</encryption>",
                    {"fonts/Serif.otf": b"not really a font", "fonts/Sans.ttf": b"not really a font"})


def make_pdfs() -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.pdfencrypt import StandardEncryption
    from reportlab.pdfgen import canvas

    def build(path: Path, enc) -> None:
        c = canvas.Canvas(str(path), pagesize=letter, encrypt=enc)
        c.setTitle(TITLE)
        c.setAuthor(AUTHOR)
        y = 720
        for t, ps in CHAPTERS:
            for line in [t] + [wrap(p, 85) for p in ps]:
                for l in line.split("\n"):
                    c.drawString(60, y, l.encode("latin-1", "replace").decode("latin-1"))
                    y -= 16
                y -= 8
        c.save()

    build(OUT / "password.pdf", StandardEncryption("open-sesame", ownerPassword="owner-secret", canCopy=1))
    build(OUT / "owner_only.pdf", StandardEncryption("", ownerPassword="owner-secret", canCopy=1, canPrint=1))


def main() -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / "alice_gutenberg.txt").write_text(gutenberg_txt(), encoding="utf-8")
    (OUT / "alice.md").write_text(markdown(), encoding="utf-8")
    (OUT / "alice_noise.html").write_text(html_page(), encoding="utf-8")
    make_docx(OUT / "alice.docx")
    make_odt(OUT / "alice.odt")
    (OUT / "alice.rtf").write_text(rtf(), encoding="ascii")
    (OUT / "alice.fb2").write_text(fb2(), encoding="utf-8")
    with zipfile.ZipFile(OUT / "alice.fb2.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("alice.fb2", fb2())
    make_epubs()
    make_pdfs()
    print("fixtures written to", OUT)


if __name__ == "__main__":
    main()
