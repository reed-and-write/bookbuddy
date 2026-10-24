"""Slice 2 extractors: TXT, Markdown, HTML, DOCX, ODT, RTF, FB2 (+ .fb2.zip)."""
import re
from pathlib import Path

import pytest

from bookbuddy import config
from bookbuddy.extractors import extract_book, find_extractor
from bookbuddy.extractors import txt as txt_mod
from bookbuddy.extractors.markdown import parse_blocks, parse_front_matter, strip_inline

FIX = Path(__file__).parent / "fixtures"
TITLES = ["Down the Rabbit-Hole", "The Pool of Tears", "A Caucus-Race and a Long Tale"]
NOISE = ["NAV-NOISE", "FOOTER-NOISE", "SIDEBAR-NOISE", "COOKIE-NOISE", "SCRIPT-NOISE", "RUNNING-HEADER-NOISE",
         "RUNNING-FOOTER-NOISE", "TABLE-NOISE", "FOOTNOTE-NOISE", "not for the ears", "Heroine", "gutenberg.org",
         "illustration", "START OF THE PROJECT", "Updated editions", "Release date", "CONTENTS"]


def alltext(book):
    return "\n".join(p for c in book.chapters for p in c.paragraphs)


def check_alice(book, *, cover=None, numbered=False):
    assert book.title == "Alice's Adventures in Wonderland"
    assert book.author == "Lewis Carroll"
    assert len(book.chapters) == 3
    for i, (c, t) in enumerate(zip(book.chapters, TITLES), 1):
        assert c.title.endswith(t), c.title
        assert c.title.startswith("Chapter") == numbered
        assert len(c.paragraphs) >= 2
    text = alltext(book)
    assert "Alice was beginning to get very tired" in text and "queer-looking party" in text
    for n in NOISE:
        assert n not in text, n
    assert not re.search(r"[*`#|\[\]<>_]|\]\(|\\u|\\par", text), re.findall(r"[*`#|\[\]<>_].{0,20}", text)[:3]
    anchors = book.anchors()
    assert anchors and anchors[0].id == "c0.p0"
    assert book.to_dict("x")["chapters"][2]["paragraphs"][0]["id"] == "c2.p0"
    if cover is not None:
        assert bool(book.cover) is cover


# ------------------------------------------------------------------ per format on the fixtures

def test_txt_gutenberg():
    book = extract_book(FIX / "alice_gutenberg.txt")
    check_alice(book, cover=False, numbered=True)
    assert any(t.startswith("CONTENTS") or t == "CONTENTS" for t, _ in book.dropped)


def test_markdown_front_matter():
    book = extract_book(FIX / "alice.md")
    check_alice(book, cover=False)
    assert "original text" in alltext(book)                      # link keeps its text


def test_html_noise_stripped():
    check_alice(extract_book(FIX / "alice_noise.html"), cover=False)


def test_docx():
    check_alice(extract_book(FIX / "alice.docx"), cover=True)
    assert extract_book(FIX / "alice.docx").cover_ext == "png"


def test_odt():
    check_alice(extract_book(FIX / "alice.odt"), cover=False)


def test_rtf():
    check_alice(extract_book(FIX / "alice.rtf"), cover=False, numbered=True)


@pytest.mark.parametrize("name", ["alice.fb2", "alice.fb2.zip"])
def test_fb2(name):
    book = extract_book(FIX / name)
    check_alice(book, cover=True)
    assert book.cover_ext == "png"
    assert "[1]" not in alltext(book)                            # note reference dropped


def test_all_formats_agree_on_chapter_text():
    ref = extract_book(FIX / "alice.docx")
    for name in ("alice.md", "alice_noise.html", "alice.odt", "alice.fb2"):
        other = extract_book(FIX / name)
        assert other.chapters[0].paragraphs[1] == ref.chapters[0].paragraphs[1], name


def test_extractor_dispatch():
    for name in ("a.txt", "a.md", "a.markdown", "a.html", "a.htm", "a.xhtml", "a.docx", "a.odt", "a.rtf", "a.fb2",
                 "a.fb2.zip", "a.mobi", "a.azw3"):
        assert find_extractor(Path(name)) is not None, name
    assert find_extractor(Path("a.zip")) is None


def test_max_chapters():
    class O: max_chapters = 2
    assert len(extract_book(FIX / "alice.docx", O).chapters) == 2


# ------------------------------------------------------------------ TXT details

def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return p


BODY = ("CHAPTER I. Start\n\n" + ("It was a dark and stormy night, and the rain fell in torrents all over town. " * 3 + "\n\n") +
        "CHAPTER II. Middle\n\n" + ("She said “hello there” and then she left without another word at all. " * 3 + "\n\n"))


@pytest.mark.parametrize("enc", ["utf-8", "utf-8-sig", "cp1252", "utf-16"])
def test_txt_encodings(tmp_path, enc):
    p = write(tmp_path, "b.txt", BODY.encode(enc))
    book = extract_book(p)
    assert [c.title for c in book.chapters] == ["Chapter 1. Start", "Chapter 2. Middle"]
    assert '"hello there"' in book.chapters[1].paragraphs[0]       # curly quotes survived the decode


def test_txt_latin1_fallback(tmp_path):
    raw = ("CHAPTER I. Café\n\n" + "Le café était ouvert à minuit, et tout le monde y allait pour parler. " * 3 +
           "\n\nCHAPTER II. Fin\n\n" + "Il était tard quand ils sont enfin rentrés chez eux ce soir-là. " * 3).encode("latin-1")
    book = extract_book(write(tmp_path, "fr.txt", raw))
    assert "café" in book.chapters[0].paragraphs[0]
    assert txt_mod.decode_bytes(b"\x81\x8d abc")[1] == "latin-1"      # undefined in cp1252


def test_txt_cyrillic_detected(tmp_path):
    text = ("Глава 1. Начало\n\n" +
            "Это была тёмная и бурная ночь над городом. " * 4)
    out, enc = txt_mod.decode_bytes(text.encode("cp1251"))
    assert enc.lower() in ("cp1251", "windows-1251") and "ночь" in out


def test_txt_title_author_from_filename(tmp_path):
    book = extract_book(write(tmp_path, "Great Expectations - Charles Dickens.txt", BODY))
    assert (book.title, book.author) == ("Great Expectations", "Charles Dickens")


def test_txt_unknown_author(tmp_path):
    book = extract_book(write(tmp_path, "notes.txt", BODY))
    assert (book.title, book.author) == ("Notes", "Unknown")


def test_txt_gutenberg_last_first_author_and_subtitle(tmp_path):
    t = ("Title: Frankenstein; Or, The Modern Prometheus\nAuthor: Shelley, Mary Wollstonecraft\n\n"
         "*** START OF THE PROJECT GUTENBERG EBOOK FRANKENSTEIN ***\n\n" + BODY + "\n*** END OF THE PROJECT GUTENBERG EBOOK FRANKENSTEIN ***\nlicense text")
    book = extract_book(write(tmp_path, "pg84.txt", t))
    assert (book.title, book.author) == ("Frankenstein", "Mary Wollstonecraft Shelley")
    assert "license text" not in alltext(book)


def test_txt_no_headings_makes_parts(tmp_path):
    para = "This sentence has exactly ten words and it keeps on going. "
    text = "\n\n".join(para * 10 for _ in range(60))        # ~6000 words
    book = extract_book(write(tmp_path, "Long Book - A. Author.txt", text))
    assert [c.title for c in book.chapters] == ["Part 1", "Part 2"]
    assert all(2500 < c.words < 3600 for c in book.chapters)


def test_txt_short_without_headings_is_one_chapter(tmp_path):
    book = extract_book(write(tmp_path, "Essay - Jane Roe.txt", "A short essay with a few words in it that goes on a bit.\n\n" * 3))
    assert len(book.chapters) == 1 and book.chapters[0].title == "Essay"


def test_txt_hard_wrapped_lines_are_rejoined(tmp_path):
    text = "CHAPTER I. Wrap\n\nThis is a paragraph that was hard wrapped at about\nseventy characters by an old text editor, so the lines\nmust be joined back into one paragraph with hyphen-\nated words repaired.\n\nSecond paragraph here, also long enough to count as real text for the check.\n\nCHAPTER II. Two\n\n" + "Plenty of words follow in this closing chapter so that it is long enough. " * 2
    book = extract_book(write(tmp_path, "w.txt", text))
    assert book.chapters[0].paragraphs[0].count("\n") == 0
    assert "hyphenated" in book.chapters[0].paragraphs[0]
    assert len(book.chapters[0].paragraphs) == 2


def test_txt_illustration_tags_dropped(tmp_path):
    text = BODY.replace("CHAPTER II.", "[Illustration: a picture]\n\nCHAPTER II.")
    assert "Illustration" not in alltext(extract_book(write(tmp_path, "i.txt", text)))


def test_empty_file_errors(tmp_path):
    with pytest.raises(ValueError):
        extract_book(write(tmp_path, "e.txt", "   \n\n"))


# ------------------------------------------------------------------ Markdown details

def test_front_matter_variants():
    meta, body = parse_front_matter("---\ntitle: 'Quoted Title'\nauthors:\n  - Ann One\n  - Bo Two\n---\n# Hi\n")
    assert meta == {"title": "Quoted Title", "authors": "Ann One, Bo Two"} and body.startswith("# Hi")
    assert parse_front_matter("no front matter\n")[0] == {}
    assert parse_front_matter("---\nauthor: [A B, C D]\n---\nx")[0]["author"] == "A B, C D"


def test_strip_inline():
    assert strip_inline("A [link](http://x.y/z) and ![img](a.png) **bold** *it* _it2_ `code` ~~gone~~ [^3] &amp; end") \
        == "A link and bold it it2 code gone & end"
    assert strip_inline("see https://example.com/page now") == "see now"
    assert strip_inline("snake_case_name stays") == "snake_case_name stays"
    assert strip_inline("<b>x</b> <https://a.b>") == "x"


MD = """# The Book

Intro line.

## One

{w}

### A sub heading

{w}

## Two

- first item
- second item with `code`

{w}
""".replace("{w}", "Some words fill this paragraph so that the chapter is long enough to keep. " * 2)


def test_markdown_picks_h2_when_h1_is_title(tmp_path):
    p = tmp_path / "b.md"
    p.write_text(MD)
    book = extract_book(p)
    assert [c.title for c in book.chapters] == ["One", "Two"]
    assert book.title == "The Book"
    assert "A sub heading." in book.chapters[0].paragraphs            # deeper heading is read as a paragraph
    assert "first item." in book.chapters[1].paragraphs


def test_markdown_h1_chapters_with_h2_inside(tmp_path):
    p = tmp_path / "b.md"
    p.write_text(MD.replace("## ", "# ").replace("# The Book", "% x").replace("### ", "## "))
    assert [c.title for c in extract_book(p).chapters] == ["One", "Two"]


def test_markdown_code_and_tables_options():
    src = "para before\n\n```\nprint(1)\n```\n\n    indented code\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nafter\n"
    assert [t for _, t in parse_blocks(src)] == ["para before", "after"]
    assert [t for _, t in parse_blocks(src, "announce", "read")] == [
        "para before", "Code block omitted.", "Code block omitted.", "a, b.", "1, 2.", "after"]


def test_markdown_setext_and_hr():
    assert parse_blocks("Title\n=====\n\ntext\n\n---\n\nSub\n---\n") == [(1, "Title"), (0, "text"), (2, "Sub")]


def test_markdown_no_headings_single_chapter(tmp_path):
    p = tmp_path / "Plain Note - Jo Doe.md"
    p.write_text("Just a paragraph of text that is long enough to count as something to read aloud.\n")
    book = extract_book(p)
    assert len(book.chapters) == 1 and book.author == "Jo Doe"


def test_config_code_blocks_option(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKBUDDY_CODE_BLOCKS", "announce")
    config.reload_settings()
    p = tmp_path / "c.md"
    p.write_text("# A\n\n" + "words and more words to read here in this chapter. " * 2 + "\n\n```\nx\n```\n\n# B\n\n" +
                 "other words and more words to read here in this one. " * 2)
    assert "Code block omitted." in alltext(extract_book(p))


# ------------------------------------------------------------------ HTML details

def test_html_without_main_strips_chrome(tmp_path):
    p = tmp_path / "p.html"
    p.write_text("""<html><head><title>My Page - Site</title></head><body>
      <div id="menu"><a>MENU-NOISE</a></div><nav>NAV-NOISE</nav>
      <h1>First</h1><p>%s</p><h1>Second</h1><p>%s</p>
      <table><tr><td>TABLE-NOISE</td></tr></table><pre>CODE-NOISE</pre>
      <footer>FOOTER-NOISE</footer></body></html>""" % ("Body text about the first thing, long enough to keep. " * 2,
                                                         "Body text about the second thing, long enough to keep. " * 2))
    book = extract_book(p)
    assert [c.title for c in book.chapters] == ["First", "Second"] and book.title == "My Page"
    for n in ("MENU-NOISE", "NAV-NOISE", "TABLE-NOISE", "CODE-NOISE", "FOOTER-NOISE"):
        assert n not in alltext(book)


def test_xhtml_and_meta_author(tmp_path):
    p = tmp_path / "x.xhtml"
    p.write_text('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title>'
                 '<meta name="author" content="Ann Author"/></head><body><h1>A</h1><p>%s</p><h1>B</h1><p>%s</p></body></html>'
                 % ("Words words words words words words words words. " * 3, "More words words words words words words. " * 3))
    book = extract_book(p)
    assert book.author == "Ann Author" and len(book.chapters) == 2


# ------------------------------------------------------------------ DOCX / ODT / FB2 details

def test_docx_tables_option(monkeypatch):
    monkeypatch.setenv("BOOKBUDDY_TABLES", "read")
    config.reload_settings()
    text = alltext(extract_book(FIX / "alice.docx"))
    assert "TABLE-NOISE, Heroine." in text


def test_odt_tables_option(monkeypatch):
    monkeypatch.setenv("BOOKBUDDY_TABLES", "read")
    config.reload_settings()
    assert "TABLE-NOISE" in alltext(extract_book(FIX / "alice.odt"))


def test_docx_junk_author_falls_back_to_filename(tmp_path):
    import docx
    d = docx.Document()
    d.add_heading("Alpha", 1); d.add_paragraph("Words in the first chapter go here, and they go on for a while. " * 2)
    d.add_heading("Beta", 1); d.add_paragraph("Words in the second chapter go here, and they go on for a while. " * 2)
    p = tmp_path / "Some Title - Jane Doe.docx"
    d.save(p)
    book = extract_book(p)                       # python-docx's default author is 'python-docx': junk
    assert (book.title, book.author) == ("Some Title", "Jane Doe")


def test_docx_corrupt_message(tmp_path):
    p = tmp_path / "bad.docx"
    p.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="could not open"):
        extract_book(p)


def test_fb2_not_fictionbook(tmp_path):
    p = tmp_path / "bad.fb2"
    p.write_text("<html/>")
    with pytest.raises(ValueError, match="FictionBook"):
        extract_book(p)


def test_rtf_rejects_non_rtf(tmp_path):
    p = tmp_path / "bad.rtf"
    p.write_text("plain")
    with pytest.raises(ValueError, match="RTF"):
        extract_book(p)


# ------------------------------------------------------------------ through the whole pipeline (fake TTS engine)

@pytest.mark.parametrize("name", ["alice_gutenberg.txt", "alice.md", "alice_noise.html", "alice.docx", "alice.odt", "alice.rtf",
                                  "alice.fb2", "alice.fb2.zip"])
def test_convert_each_format_to_m4b(tmp_path, name):
    import json
    import subprocess

    from bookbuddy.engines.fake import FakeEngine
    from bookbuddy.pipeline import Options, convert

    res = convert(FIX / name, Options(out_dir=tmp_path / "out", work_root=tmp_path / "work", max_chapters=2,
                                      engine_factory=lambda v, s, d=None: FakeEngine(v, s)))
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-show_format", "-of", "json", str(res.path)],
                                     capture_output=True, text=True, check=True).stdout)
    assert len(info["chapters"]) == 2
    assert info["format"]["tags"]["artist"] == "Lewis Carroll"


def test_txt_chapter_titles_lose_gutenberg_italics_marks(tmp_path):
    text = BODY.replace("CHAPTER I. Start", "CHAPTER I. _To Mrs. Saville, England._")
    assert extract_book(write(tmp_path, "i.txt", text)).chapters[0].title == "Chapter 1. To Mrs. Saville, England."


def test_rtf_info_group_variants(tmp_path):
    body = "{\\rtf1\\ansi{\\info{\\upr{\\title Caf\\'e9 Book}{\\*\\ud{\\title Caf\\'e9 Book}}}{\\upr{\\author Ana \\u8217?B}}}\\pard %s\\par}" % ("Plain words in one paragraph only. " * 6)
    p = tmp_path / "x.rtf"
    p.write_text(body, encoding="ascii")
    book = extract_book(p)
    assert (book.title, book.author) == ("Café Book", "Ana ’B")
