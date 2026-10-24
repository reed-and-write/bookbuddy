"""PDF author fallback: metadata -> title-page text -> file name -> 'Unknown'."""
from pathlib import Path

import pytest

from bookbuddy.extractors import pdf as P

FIX = Path(__file__).parent / "fixtures"


def pick(info, text, name="x.pdf"):
    return P.pick_title_author(info, text, Path(name))


def test_metadata_author_wins():
    assert pick({"Title": "T", "Author": "Jane Doe"}, "by Someone Else", "a - B C.pdf") == ("T", "Jane Doe")


@pytest.mark.parametrize("junk", ["Unknown", "anonymous", "admin", "Microsoft Word", "", "user", "draft.docx"])
def test_junk_metadata_author_is_ignored(junk):
    assert pick({"Title": "T", "Author": junk}, "") == ("T", "Unknown")
    assert pick({"Title": "T", "Author": junk}, "A Tale\nby Jane Doe\n")[1] == "Jane Doe"


def test_by_line_on_title_page():
    assert pick({}, "A Grand Tale\nby Jane Doe\n", "my_book.pdf") == ("A Grand Tale", "Jane Doe")


def test_by_line_on_later_title_page():
    text = "Some Title\n\nFor my family\n\n\f\nTHE BOOK OF DAYS\nBy Mary Ann Smith\n"
    assert pick({"Title": "The Book of Days"}, text)[1] == "Mary Ann Smith"


def test_gutenberg_author_label_and_running_header():
    text = "The Art of War, by Sun Tzu     1\nTitle: The Art of War\n\nAuthor: Sun Tzu\n\nTranslator: Lionel Giles\n"
    assert pick({"Title": "The Art of War"}, text)[1] == "Sun Tzu"
    assert pick({}, "The Art of War, by Sun Tzu   1\nThe Project Gutenberg eBook\n")[1] == "Sun Tzu"


def test_translator_is_not_the_author():
    text = "Translated from the Chinese with Introduction\nand Critical Notes\nBY\nLIONEL GILES, M.A.\n"
    assert pick({"Title": "X"}, text)[1] == "Unknown"
    assert pick({"Title": "X"}, "Translated by Lionel Giles\n")[1] == "Unknown"


def test_name_line_under_the_title():
    assert pick({"Title": "The Long Road"}, "The Long Road\nJohn R. Smith\nA Novel\n")[1] == "John R. Smith"
    assert pick({"Title": "The Long Road"}, "The Long Road\nA Novel of the West\n")[1] == "Unknown"


def test_filename_title_dash_author():
    assert pick({}, "", "The Great Tale - Jane Doe.pdf") == ("The Great Tale", "Jane Doe")
    assert pick({}, "", "the_great_tale_-_jane_doe.pdf") == ("The Great Tale", "Jane Doe")
    assert pick({"Title": "Real Title"}, "", "whatever - Jane Doe.pdf") == ("Real Title", "Jane Doe")


def test_filename_author_dash_title_when_only_left_is_a_name():
    assert pick({}, "", "Jane Doe - The Great Tale.pdf") == ("The Great Tale", "Jane Doe")


def test_filename_without_a_dash_gives_unknown():
    assert pick({}, "", "some_great_book.pdf") == ("Some Great Book", "Unknown")
    assert pick({}, "", "Part 1 - Chapter 2.pdf")[1] == "Unknown"


def test_text_beats_filename():
    assert pick({}, "Real Title\nby Ann Lee\n", "Other - Bob Roe.pdf")[1] == "Ann Lee"


def test_real_pdf_without_metadata_author(tmp_path):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    pdf = tmp_path / "untitled.pdf"
    c = canvas.Canvas(str(pdf), pagesize=letter)
    c.setTitle("")
    c.setAuthor("")
    c.drawString(72, 700, "The Lantern Keeper")
    c.drawString(72, 680, "by Ruth Evans")
    c.showPage()
    for i in range(3):
        for n in range(30):
            c.drawString(72, 720 - n * 20, "The keeper climbed the long stair each evening to light the lamp again.")
        c.showPage()
    c.save()
    book = P.extract_pdf(pdf, llm="none")
    assert book.author == "Ruth Evans" and book.title == "The Lantern Keeper"


def test_extractor_interface_uses_it(tmp_path):
    from bookbuddy.pipeline import Options
    assert P.extract(FIX / "sample_book.pdf", Options(llm="none")).author == "Lewis Carroll"
