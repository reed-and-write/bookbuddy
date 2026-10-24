"""Book model: paragraph anchors, chunk->paragraph references, JSON persistence."""
import json

from bookbuddy.book import Book, Chapter
from bookbuddy.extractors.epub import extract_epub
from bookbuddy.pipeline import Options, convert, plan_chunks
from bookbuddy.engines.fake import FakeEngine
from helpers import SAMPLE_CHAPTERS, make_epub


def sample():
    return Book("T", "A", [Chapter("One", ["First para.", "Second one is longer."]), Chapter("Two", ["Only para."])])


def test_paragraph_anchors_have_ids_and_offsets():
    b = sample()
    a = b.anchors()
    assert [x.id for x in a] == ["c0.p0", "c0.p1", "c1.p0"]
    ch = b.chapters[0]
    for x in a[:2]:
        assert ch.text[x.start:x.end] == ch.paragraphs[x.index]
    assert (a[1].start, a[1].end) == (len("First para.") + 2, len("First para.") + 2 + len("Second one is longer."))
    assert a[2].start == 0 and b.paragraph(1, 0) == "Only para."


def test_json_roundtrip(tmp_path):
    b = sample()
    b.cover, b.cover_ext = b"\x89PNGfake", "png"
    path = b.save(tmp_path, source="x.epub", extra={"job": {"voice": "v"}})
    d = json.loads(path.read_text())
    assert d["schema"] == 1 and d["source"] == "x.epub" and d["job"] == {"voice": "v"}
    p = d["chapters"][0]["paragraphs"][1]
    assert p["id"] == "c0.p1" and p["text"] == "Second one is longer." and p["start"] < p["end"]
    b2 = Book.load(tmp_path)
    assert b2.title == "T" and [c.paragraphs for c in b2.chapters] == [c.paragraphs for c in b.chapters]
    assert b2.cover == b.cover and b2.cover_ext == "png"


def test_chunks_reference_their_paragraph(tmp_path):
    src = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS)
    book = extract_epub(str(src))
    plan = plan_chunks(book)
    for ci, chunks in enumerate(plan):
        assert chunks[0].kind == "title" and chunks[0].paragraph is None
        for c in chunks[1:]:
            assert c.chapter == ci and c.paragraph is not None
            para = book.chapters[ci].paragraphs[c.paragraph]
            assert c.start is not None and " ".join(para[c.start:c.end].split()) == " ".join(c.text.split())
    # a long paragraph splits into several chunks that all point at the same paragraph, in order
    long_para = " ".join("This is sentence number %d, and it goes on for a while." % i for i in range(30))
    long_book = Book("T", "A", [Chapter("One", [long_para])])
    chunks = plan_chunks(long_book)[0][1:]
    assert len(chunks) > 3 and {c.paragraph for c in chunks} == {0}
    assert [c.start for c in chunks] == sorted(c.start for c in chunks)


def test_conversion_persists_book_json_and_chunk_anchors(tmp_path):
    src = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS)
    o = Options(out_dir=tmp_path / "out", work_root=tmp_path / "work", keep_work=True,
                engine_factory=lambda v, s, d=None: FakeEngine(v, s))
    convert(src, o)
    wd = next((tmp_path / "work").iterdir())
    d = json.loads((wd / "book.json").read_text())
    assert d["title"] == "Test Book" and d["job"]["voice"] == o.voice and len(d["chapters"]) == 3
    assert d["chapters"][0]["paragraphs"][0]["id"] == "c0.p0"
    chunks = json.loads((wd / "chunks.json").read_text())
    assert chunks[0]["kind"] == "title" and chunks[1]["paragraph"] == 0 and chunks[1]["start"] == 0
    assert Book.load(wd).title == "Test Book"
