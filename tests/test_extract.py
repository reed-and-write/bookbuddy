from bookbuddy.extractors.epub import extract_epub, html_to_blocks
from helpers import SAMPLE_CHAPTERS, make_epub


def test_front_and_back_matter_stripped(tmp_path):
    p = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS)
    book = extract_epub(str(p))
    assert [c.title for c in book.chapters] == ["Chapter 1. The Start", "Chapter 2. The Middle", "Chapter 3. The End"]
    reasons = dict(book.dropped)
    assert "Copyright" in reasons and "Contents" in reasons and "Also by A. Writer" in reasons
    assert book.title == "Test Book" and book.author == "A. Writer"
    assert book.cover and len(book.cover) > 20


def test_text_is_normalised_and_title_not_duplicated(tmp_path):
    p = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS, front=False)
    book = extract_epub(str(p))
    first = book.chapters[0].paragraphs
    assert first[0].startswith("It was a dark night. Mister Smith")
    assert "nineteen eighty-four" in first[0]
    assert "five dollars and fifty cents" in first[1] and "[1]" not in first[1]
    assert not any("The Start" in x for x in first)       # heading removed from the body


def test_max_chapters(tmp_path):
    p = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS)
    assert len(extract_epub(str(p), max_chapters=2).chapters) == 2


def test_no_cover(tmp_path):
    p = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS, cover=False, front=False)
    assert extract_epub(str(p)).cover is None


def test_toc_anchor_splitting_and_footnote_sup():
    html = ("<html><body><div id='a'><h2>One</h2><p>Para one.<sup>1</sup></p></div>"
            "<div id='b'><h2>Two</h2><p>Para two.</p></div></body></html>")
    blocks, anchors = html_to_blocks(html)
    assert anchors["a"] == 0 and anchors["b"] == 2
    assert [t for _, t in blocks] == ["One", "Para one.", "Two", "Para two."]


def test_real_gutenberg_book():
    from pathlib import Path
    fx = Path(__file__).parent / "fixtures" / "alice.epub"
    book = extract_epub(str(fx))
    assert len(book.chapters) == 12
    assert book.chapters[0].title == "Chapter 1. Down the Rabbit-Hole"
    assert not any("Gutenberg" in p for c in book.chapters for p in c.paragraphs)
    assert 25000 < book.words < 29000
