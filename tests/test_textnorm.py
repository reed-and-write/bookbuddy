import re

from bookbuddy import config
from bookbuddy.textnorm import (chunk_chapter, chunk_paragraph, display_title, normalize_text, split_sentences,
                                year_to_words)


def test_abbreviations():
    assert normalize_text("Mr. and Mrs. Smith met Dr. Jones.") == "Mister and Missus Smith met Doctor Jones."
    assert "Saint Paul" in normalize_text("We went to St. Paul.")
    assert "Main Street" in normalize_text("He lives on Main St.")
    assert normalize_text("apples, pears, etc. and more") == "apples, pears, et cetera and more"


def test_numbers_years_ordinals():
    assert year_to_words(1984) == "nineteen eighty-four"
    assert year_to_words(1905) == "nineteen oh five"
    assert year_to_words(2000) == "two thousand"
    assert year_to_words(2010) == "twenty ten"
    assert normalize_text("In 1984 it began.") == "In nineteen eighty-four it began."
    assert normalize_text("the 3rd of May") == "the third of May"
    assert normalize_text("He had 42 cats") == "He had forty-two cats"
    assert normalize_text("It cost $5.50.") == "It cost five dollars and fifty cents."
    assert normalize_text("about 1,200 people") == "about one thousand two hundred people"
    assert normalize_text("3.5 miles") == "three point five miles"
    assert normalize_text("the 1990s") == "the nineteen nineties"
    assert normalize_text("at 3:30 sharp") == "at three thirty sharp"
    assert normalize_text("50%") == "fifty percent"


def test_footnote_markers_and_symbols():
    assert normalize_text("It was so.[12] Really.[a]") == "It was so. Really."
    assert normalize_text("Truth¹ and lies².") == "Truth and lies."
    assert normalize_text("a _very_ good day") == "a very good day"
    assert normalize_text("* * *") == ""


def test_whitespace_quotes_dashes():
    assert normalize_text("  a \n\t b   c ") == "a b c"
    assert normalize_text("“Hi,” she said — quietly.") == '"Hi," she said, quietly.'


def test_display_title():
    assert display_title("CHAPTER IV. A MAD TEA-PARTY") == "Chapter 4. A Mad Tea-Party" or display_title("CHAPTER IV.").startswith("Chapter 4")
    assert display_title("CHAPTER I.\n Down the Rabbit-Hole") == "Chapter 1. Down the Rabbit-Hole"
    assert display_title("Mix of Roman") == "Mix of Roman"


def test_sentence_split_basic():
    s = split_sentences('Hello there. How are you? "I am fine!" she said. Then left.')
    assert s == ['Hello there.', 'How are you?', '"I am fine!" she said.', 'Then left.']


def test_sentence_split_initials_not_split():
    s = split_sentences("J. R. Smith arrived. He sat down.")
    assert s == ["J. R. Smith arrived.", "He sat down."]


def _long_text(n_sent=300):
    import random
    rnd = random.Random(1)
    words = "the quick brown fox jumps over a lazy dog while singing softly in the rain".split()
    out = []
    for _ in range(n_sent):
        k = rnd.randint(3, 60)
        out.append(" ".join(rnd.choice(words) for _ in range(k)).capitalize() + rnd.choice([".", "!", "?"]))
    return " ".join(out)


def test_chunks_respect_cap_and_never_split_sentences():
    para = _long_text()
    sents = split_sentences(para)
    pieces = [t for t, _ in chunk_paragraph(para)]
    assert pieces
    assert all(len(p) <= config.MAX_CHUNK_CHARS for p in pieces)
    # every chunk is made of whole sentences (short sentences may merge, none are cut)
    joined = " ".join(pieces)
    assert joined == " ".join(sents)
    for p in pieces:
        parts = split_sentences(p)
        assert all(s in sents for s in parts), p


def test_overlong_sentence_is_split_under_cap():
    sentence = ("word, " * 200).strip().rstrip(",") + "."
    pieces = [t for t, _ in chunk_paragraph(sentence)]
    assert len(pieces) > 1
    assert all(len(p) <= config.MAX_CHUNK_CHARS for p in pieces)
    assert re.sub(r"\s+", " ", " ".join(pieces)) == sentence


def test_chunk_chapter_pauses_and_title():
    chunks = chunk_chapter(0, "Chapter 1. Start", ["First sentence is here. Second sentence follows it.", "Next paragraph is this one."])
    assert chunks[0].kind == "title" and chunks[0].pause == config.PAUSE_AFTER_TITLE
    assert chunks[1].pause == config.PAUSE_SENTENCE
    assert chunks[2].pause == config.PAUSE_PARAGRAPH
    assert chunks[-1].pause == config.PAUSE_CHAPTER_END
    assert [c.index for c in chunks] == list(range(len(chunks)))
