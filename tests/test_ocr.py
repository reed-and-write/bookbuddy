"""Slice 3: OCR (scanned PDFs, image folders, CBZ/CBR, DJVU). Fixtures come from tests/make_ocr_fixtures.py."""
import difflib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from bookbuddy import config, ocr
from bookbuddy.extractors import extract_book, find_extractor, list_formats
from bookbuddy.extractors import pdf as P
from bookbuddy.ocr import common, postprocess, sources, tesseract

sys.path.insert(0, str(Path(__file__).parent))
from make_ocr_fixtures import ground_truth   # noqa: E402

FIX = Path(__file__).parent / "fixtures"
HAVE_TESS = shutil.which("tesseract") is not None and "eng" in common.tesseract_langs()
HAVE_OMP = HAVE_TESS and not common.ocrmypdf_missing()
needs_tess = pytest.mark.skipif(not HAVE_TESS, reason="tesseract with eng data not installed")
needs_omp = pytest.mark.skipif(not HAVE_OMP, reason="ocrmypdf + ghostscript + qpdf not installed")


@pytest.fixture(autouse=True)
def _own_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKBUDDY_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("BOOKBUDDY_CACHE", str(tmp_path / "cache"))
    config.reload_settings()


class Opts(SimpleNamespace):
    def __init__(self, **kw):
        d = dict(max_chapters=None, llm="none", on_event=None, ocr_lang=None, ocr_backend=None, ocr_workers=2)
        d.update(kw)
        super().__init__(**d)


def words(text):
    return re.findall(r"[a-z]+", text.lower())


def accuracy(book):
    got = words("\n".join(p for c in book.chapters for p in c.paragraphs))
    want = words(ground_truth())
    return difflib.SequenceMatcher(None, want, got, autojunk=False).ratio()


def alltext(book):
    return "\n".join(p for c in book.chapters for p in c.paragraphs)


def check_scanned_book(book):
    assert accuracy(book) > 0.93
    text = alltext(book)
    assert "Alice was beginning to get very tired" in text
    assert "ADVENTURES IN A STRANGE LAND" not in text.upper()           # running header removed
    assert not re.search(r"(^|\s)[123](\s|$)", text)                    # page numbers removed
    assert len(book.chapters) == 2
    assert book.chapters[0].title.endswith("Down the Rabbit-hole")
    assert book.chapters[1].title.endswith("Pool of Tears")


@pytest.fixture
def count_tesseract(monkeypatch):
    calls = []
    real = tesseract.run_tesseract
    monkeypatch.setattr(tesseract, "run_tesseract", lambda img, lang, exe: calls.append(img.name) or real(img, lang, exe))
    return calls


# ---------------------------------------------------------------- backend selection

def test_auto_prefers_ocrmypdf_for_pdf_only(monkeypatch):
    monkeypatch.setattr(ocr, "ocrmypdf_missing", lambda: "")
    monkeypatch.setattr(ocr, "require_tesseract", lambda: "/usr/bin/tesseract")
    assert ocr.select_backend("auto", is_pdf=True) == "ocrmypdf"
    assert ocr.select_backend("auto", is_pdf=False) == "tesseract"


def test_auto_falls_back_to_tesseract_when_ocrmypdf_missing(monkeypatch):
    monkeypatch.setattr(ocr, "ocrmypdf_missing", lambda: "ocrmypdf is not installed")
    monkeypatch.setattr(ocr, "require_tesseract", lambda: "/usr/bin/tesseract")
    assert ocr.select_backend("auto", is_pdf=True) == "tesseract"


def test_forced_ocrmypdf_missing_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(ocr, "ocrmypdf_missing", lambda: "ocrmypdf is not installed")
    with pytest.raises(ocr.OcrError, match="'ocrmypdf' was requested but ocrmypdf is not installed"):
        ocr.select_backend("ocrmypdf", is_pdf=True)


def test_forced_ocrmypdf_on_images_uses_tesseract(monkeypatch):
    monkeypatch.setattr(ocr, "ocrmypdf_missing", lambda: "")
    monkeypatch.setattr(ocr, "require_tesseract", lambda: "/usr/bin/tesseract")
    assert ocr.select_backend("ocrmypdf", is_pdf=False) == "tesseract"


def test_no_tesseract_message(monkeypatch):
    monkeypatch.setattr(common, "tesseract_path", lambda: None)
    with pytest.raises(ocr.OcrError, match="needs Tesseract"):
        ocr.select_backend("tesseract")


def test_unknown_backend_rejected():
    with pytest.raises(ocr.OcrError, match="unknown OCR backend"):
        ocr.select_backend("magic")


def test_config_ocr_table_and_env(monkeypatch):
    s = config.parse_settings({"ocr": {"lang": "eng+deu", "backend": "tesseract", "workers": 3}}, env={})
    assert (s.ocr_lang, s.ocr_backend, s.ocr_workers) == ("eng+deu", "tesseract", 3)
    s = config.parse_settings({}, env={"BOOKBUDDY_OCR_LANG": "fra", "BOOKBUDDY_OCR_BACKEND": "ocrmypdf"})
    assert (s.ocr_lang, s.ocr_backend, s.ocr_workers) == ("fra", "ocrmypdf", 0)
    with pytest.raises(ValueError, match="backend"):
        config.parse_settings({"ocr": {"backend": "nope"}}, env={})


def test_ocrmypdf_command_options(monkeypatch):
    from bookbuddy.ocr import ocrmypdf_backend as O
    monkeypatch.setattr(O, "which", lambda n: None)
    cmd = O.build_command(Path("a.pdf"), Path("b.pdf"), "eng", 4, [0, 2, 3])
    assert ["--skip-text", "--deskew", "--output-type", "pdf"] == [c for c in cmd if c in ("--skip-text", "--deskew", "--output-type", "pdf")]
    assert cmd[cmd.index("-l") + 1] == "eng" and cmd[cmd.index("--jobs") + 1] == "4"
    assert cmd[cmd.index("--pages") + 1] == "1,3-4" and "--clean" not in cmd
    monkeypatch.setattr(O, "which", lambda n: "/usr/bin/unpaper")
    assert "--clean" in O.build_command(Path("a.pdf"), Path("b.pdf"), "eng", 4, None)


# ---------------------------------------------------------------- both backends on a scanned PDF

@needs_tess
def test_scanned_pdf_tesseract_backend():
    book = extract_book(FIX / "scanned_book.pdf", Opts(ocr_backend="tesseract"))
    check_scanned_book(book)
    assert book.title == "Sample scan" and book.author == "Test Fixture"
    assert book.cover and not book.artifacts


@needs_omp
def test_scanned_pdf_ocrmypdf_backend():
    book = extract_book(FIX / "scanned_book.pdf", Opts(ocr_backend="ocrmypdf"))
    check_scanned_book(book)
    pdf = book.artifacts["searchable_pdf"]
    assert pdf.exists() and "Alice was beginning" in subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True).stdout


@needs_omp
def test_ocrmypdf_searchable_pdf_is_reused(count_tesseract):
    opts = Opts(ocr_backend="ocrmypdf", on_event=lambda k, **i: events.append((k, i)))
    events = []
    extract_book(FIX / "scanned_book.pdf", opts)
    first = [e for e in events if e[0] == "ocr_plan"][0][1]
    events.clear()
    extract_book(FIX / "scanned_book.pdf", opts)
    again = [e for e in events if e[0] == "ocr_plan"][0][1]
    assert not first.get("cached") and again["cached"] is True


@needs_omp
def test_ocr_pages_interface_returns_pagetext_for_both_backends():
    a = ocr.ocr_pages(FIX / "scanned_book.pdf", "eng", 2, backend="tesseract")
    b = ocr.ocr_pages(FIX / "scanned_book.pdf", "eng", 2, backend="ocrmypdf")
    assert [p.index for p in a] == [p.index for p in b] == [0, 1, 2]
    for x, y in zip(a, b):
        wx, wy = words(x.text), words(y.text)
        assert difflib.SequenceMatcher(None, wx, wy, autojunk=False).ratio() > 0.95


@needs_tess
def test_ocrmypdf_failure_falls_back_in_auto_mode(monkeypatch):
    from bookbuddy.ocr import ocrmypdf_backend as O

    def boom(*a, **k):
        raise ocr.OcrError("ocrmypdf failed (exit 7)")
    monkeypatch.setattr(O, "make_searchable", boom)
    monkeypatch.setattr(ocr, "ocrmypdf_missing", lambda: "")
    res = ocr.run_ocr(FIX / "scanned_book.pdf", "eng", 2, backend="auto")
    assert res.backend == "tesseract" and len(res.pages) == 3


# ---------------------------------------------------------------- mixed PDFs, cache, events

@needs_tess
def test_mixed_pdf_only_ocrs_image_pages(count_tesseract):
    pages = P.pdf_pages(FIX / "mixed_book.pdf")
    assert P.ocr_candidates(FIX / "mixed_book.pdf", pages) == [1]
    book = extract_book(FIX / "mixed_book.pdf", Opts(ocr_backend="tesseract"))
    assert len(count_tesseract) == 1
    assert len(book.chapters) == 2 and accuracy(book) > 0.93
    assert "Either the well was very deep" in alltext(book)       # page 2 came from OCR


@needs_tess
def test_page_cache_skips_pages_already_done(count_tesseract):
    extract_book(FIX / "scanned_book.pdf", Opts(ocr_backend="tesseract"))
    assert len(count_tesseract) == 3
    ev = []
    extract_book(FIX / "scanned_book.pdf", Opts(ocr_backend="tesseract", on_event=lambda k, **i: ev.append(k)))
    assert len(count_tesseract) == 3          # nothing re-OCR'd on the second run
    assert "ocr_plan" in ev


@needs_tess
def test_partial_cache_resume(count_tesseract, tmp_path):
    ocr.ocr_pages(FIX / "scanned_book.pdf", "eng", 1, backend="tesseract", pages=[0, 1])
    assert len(count_tesseract) == 2
    res = ocr.run_ocr(FIX / "scanned_book.pdf", "eng", 1, backend="tesseract")
    assert len(count_tesseract) == 3 and res.cached == 2


@needs_tess
def test_events_emitted():
    ev = []
    extract_book(FIX / "scanned_book.pdf", Opts(ocr_backend="tesseract", on_event=lambda k, **i: ev.append((k, i))))
    kinds = [k for k, _ in ev]
    assert kinds[0] == "ocr_plan" and kinds.count("ocr_progress") >= 1
    plan = ev[0][1]
    assert plan["pages"] == 3 and plan["total_pages"] == 3 and plan["backend"] == "tesseract" and plan["est_seconds"] > 0
    last = [i for k, i in ev if k == "ocr_progress"][-1]
    assert last["done"] == 3 and last["percent"] == 100 and last["pages_per_second"] > 0


def test_notifier_ocr_messages(capsys):
    from bookbuddy.notify import Notifier
    n = Notifier(dry_run=True, enabled=["desktop"])
    n.ocr_plan(300, 320, 900, "tesseract")
    n.ocr_plan(300, 320, 0, "ocrmypdf", cached=True)       # nothing to do: silent
    n.ocr_progress(150, 300, 50, 450)
    n.ocr_progress(300, 300, 100, 0)                       # done: silent (voicing announces itself)
    out = capsys.readouterr().out
    assert out.count("[dry-run]") == 2
    assert "OCR via tesseract" in out and "300 of 320 pages" in out and "15 min" in out and "150 of 300 pages read" in out


@needs_tess
def test_watcher_job_forwards_ocr_events(tmp_path):
    from bookbuddy.jobs import process_file
    seen = []

    class N:
        def started(self, *a): pass
        def planned(self, *a, **k): pass
        def finished(self, *a): pass
        def failed(self, *a): seen.append(("failed", a))
        def ocr_plan(self, *a, **k): seen.append("ocr_plan")
        def ocr_progress(self, *a, **k): seen.append("ocr_progress")

    def fake_convert(src, opts):
        extract_book(src, SimpleNamespace(max_chapters=None, llm="none", on_event=opts.on_event, ocr_lang=None,
                                          ocr_backend="tesseract", ocr_workers=2))
        return SimpleNamespace(path=tmp_path / "x", title="t", author="a", duration=1, size=1, llm="none")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy(FIX / "scanned_book.pdf", inbox / "scanned_book.pdf")
    from bookbuddy.pipeline import Options
    assert process_file(inbox / "scanned_book.pdf", inbox, Options(), N(), convert_fn=fake_convert)
    assert "ocr_plan" in seen and "ocr_progress" in seen


# ---------------------------------------------------------------- other inputs

@needs_tess
def test_cbz():
    ex = find_extractor(FIX / "book_pages.cbz")
    assert ex.__name__.endswith("cbz")
    check_scanned_book(extract_book(FIX / "book_pages.cbz", Opts()))


@needs_tess
def test_image_folder_natural_order(tmp_path):
    folder = tmp_path / "My Scanned Book"
    folder.mkdir()
    with zipfile.ZipFile(FIX / "book_pages.cbz") as zf:
        names = sorted(zf.namelist())
        # names that sort wrongly as plain text: 2 < 10 only with a natural sort
        for n, new in zip(names, ["p1.png", "p2.png", "p10.png"]):
            (folder / new).write_bytes(zf.read(n))
    order = [p.name for p in sources.list_images(folder)]
    assert order == ["p1.png", "p2.png", "p10.png"]
    (folder / "notes.txt").write_text("ignore me")
    ex = find_extractor(folder)
    assert ex.__name__.endswith("images")
    book = extract_book(folder, Opts())
    assert book.title == "My Scanned Book"
    check_scanned_book(book)            # pages read 1, 2, 10 (a plain text sort would read 1, 10, 2)


@needs_tess
def test_zip_of_images(tmp_path):
    z = tmp_path / "Pages - Some Author.zip"
    shutil.copy(FIX / "book_pages.cbz", z)
    assert find_extractor(z).__name__.endswith("images")
    book = extract_book(z, Opts())
    check_scanned_book(book)
    assert book.author == "Some Author"


def test_zip_without_images_is_not_claimed(tmp_path):
    z = tmp_path / "docs.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.txt", "hello")
    assert find_extractor(z) is None


def test_zip_slip_names_never_become_paths(tmp_path):
    z = tmp_path / "evil.cbz"
    png = (FIX / "book_pages.cbz")
    with zipfile.ZipFile(png) as src, zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../../escape.png", src.read(src.namelist()[0]))
    dest = tmp_path / "out"
    dest.mkdir()
    files = sources.extract_zip(z, dest)
    assert [f.parent for f in files] == [dest] and not (tmp_path.parent / "escape.png").exists()


@pytest.mark.skipif(not (shutil.which("ddjvu") and (FIX / "book_pages.djvu").exists()), reason="DjVuLibre or fixture missing")
@needs_tess
def test_djvu():
    assert find_extractor(FIX / "book_pages.djvu").__name__.endswith("djvu")
    check_scanned_book(extract_book(FIX / "book_pages.djvu", Opts()))


def test_cbr_that_is_really_a_zip_opens(tmp_path):
    p = tmp_path / "x.cbr"
    shutil.copy(FIX / "book_pages.cbz", p)
    assert sources.sniff_archive(p) == "zip"
    files = sources.extract_archive(p, tmp_path)
    assert len(files) == 3


def test_cbr_without_unrar_gives_clear_message(tmp_path, monkeypatch):
    p = tmp_path / "real.cbr"
    p.write_bytes(b"Rar!\x1a\x07\x00" + b"\0" * 64)
    monkeypatch.setattr(sources, "which", lambda n: None)
    with pytest.raises(ValueError, match="needs a RAR extractor.*unrar"):
        extract_book(p, Opts())


def test_cbr_extraction_uses_unrar_arguments(tmp_path, monkeypatch):
    """A stand-in `unrar` gets `x -idq -o+ -y ARCHIVE DEST/` and what it writes is read back in natural order."""
    tool = tmp_path / "unrar"
    tool.write_text('#!/bin/sh\n[ "$1 $2 $3 $4" = "x -idq -o+ -y" ] || exit 9\nunzip -qo %s -d "$6"\n' % (FIX / "book_pages.cbz"))
    tool.chmod(0o755)
    p = tmp_path / "real.cbr"
    p.write_bytes(b"Rar!\x1a\x07\x00" + b"\0" * 16)
    dest = tmp_path / "x"
    dest.mkdir()
    monkeypatch.setattr(sources, "which", lambda n: str(tool) if n == "unrar" else None)
    assert [f.name for f in sources.extract_archive(p, dest)] == ["page01.png", "page02.png", "page03.png"]


def test_image_prepare_handles_alpha_and_tiny(tmp_path):
    from PIL import Image
    im = Image.new("RGBA", (300, 200), (255, 255, 255, 0))
    f = tmp_path / "a.png"
    im.save(f)
    out = sources.prepare_image(f, tmp_path)
    assert out != f
    with Image.open(out) as o:
        assert o.mode in ("RGB", "L") and o.width >= 300 * 3 * 0.99 or o.width >= sources.MIN_WIDTH


# ---------------------------------------------------------------- language / tool messages

@needs_tess
def test_missing_language_pack_names_the_package():
    with pytest.raises(ValueError, match=r"language data for 'xyz'.*tesseract-data-xyz.*tesseract-ocr-xyz"):
        extract_book(FIX / "book_pages.cbz", Opts(ocr_lang="xyz"))
    with pytest.raises(P.ScannedPdfError, match="tesseract-data-xyz"):
        extract_book(FIX / "scanned_book.pdf", Opts(ocr_lang="eng+xyz", ocr_backend="tesseract"))


def test_scanned_pdf_without_any_ocr_tool(monkeypatch):
    monkeypatch.setattr(common, "tesseract_path", lambda: None)
    monkeypatch.setattr(ocr, "ocrmypdf_missing", lambda: "ocrmypdf is not installed")
    with pytest.raises(P.ScannedPdfError, match="needs OCR.*Tesseract"):
        extract_book(FIX / "scanned_book.pdf", Opts())


def test_text_pdfs_never_touch_ocr(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("OCR must not run for a text PDF")
    monkeypatch.setattr(ocr, "run_ocr", boom)
    assert extract_book(FIX / "sample_book.pdf", Opts()).chapters


# ---------------------------------------------------------------- formats

def test_formats_lists_ocr_formats():
    names = {r.name for r in list_formats()}
    assert {"PDF", "CBZ", "CBR", "DjVu", "Image folder / ZIP"} <= names
    pdf_row = next(r for r in list_formats() if r.name == "PDF")
    assert "OCR" in pdf_row.how


def test_formats_command_shows_active_backend(capsys):
    from bookbuddy import cli
    cli.main(["formats"])
    out = capsys.readouterr().out
    assert re.search(r"OCR: (PDFs via (ocrmypdf|tesseract)|unavailable)", out) and ".cbz" in out and ".djvu" in out


# ---------------------------------------------------------------- the pipeline, fake TTS

@needs_tess
@pytest.mark.parametrize("name", ["scanned_book.pdf", "book_pages.cbz"])
def test_convert_scanned_to_m4b(tmp_path, name):
    from bookbuddy.engines.fake import FakeEngine
    from bookbuddy.pipeline import Options, convert

    res = convert(FIX / name, Options(out_dir=tmp_path / "out", work_root=tmp_path / "work", keep_work=True,
                                      engine_factory=lambda v, s, d=None: FakeEngine(v, s), ocr_workers=2))
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(res.path)],
                                     capture_output=True, text=True, check=True).stdout)
    assert len(info["chapters"]) == 2
    if name.endswith(".pdf") and HAVE_OMP:
        assert list((tmp_path / "work").glob("*/searchable.pdf"))


@needs_tess
def test_image_folder_through_pipeline_hash(tmp_path):
    from bookbuddy.pipeline import book_hash
    d = tmp_path / "f"
    d.mkdir()
    (d / "1.png").write_bytes(b"a")
    h1 = book_hash(d, "v", 1.0)
    (d / "1.png").write_bytes(b"b")
    assert book_hash(d, "v", 1.0) != h1


# ---------------------------------------------------------------- OCR text fixes

def test_ligatures_and_pipes():
    t = postprocess.fix_ocr_text("The ﬁnal ofﬁce was ﬂooded. |t was late and | am tired. |'ll go.")
    assert t == "The final office was flooded. It was late and I am tired. I'll go."


def test_stray_marks_and_lines_removed():
    t = postprocess.fix_ocr_text("Hello ~ there\n\n.\n\ne\n\n* * *\n\nSecond paragraph here.")
    assert "~" not in t and "Hello there" in t
    assert [l for l in t.split("\n") if l.strip()] == ["Hello there", "Second paragraph here."]


def test_common_misreadings_english_only():
    assert postprocess.fix_ocr_text("tbe man wliich lt was , said") == "the man which It was, said"
    assert postprocess.fix_ocr_text("tbe man", "fra") == "tbe man"


def test_line_end_hyphen_marks_are_joined_by_the_normal_pipeline():
    assert P.dehyphenate(postprocess.fix_ocr_text("a conver¬\nsation here")) == "a conversation here"


def test_chapter_number_repair():
    out = postprocess.repair_chapter_numbers(["CHAPTER I\nOne", "x", "CHAPTER 11\nTwo", "CHAPTER\nThree", "CHAPTER IV\nFour"])
    assert [p.split("\n")[0] for p in out if p.startswith("CHAPTER")] == ["CHAPTER I", "CHAPTER II", "CHAPTER III", "CHAPTER IV"]
    arabic = postprocess.repair_chapter_numbers(["Chapter 1. A", "Chapter 2. B", "Chapter l3. C"])
    assert arabic[2] == "Chapter 3. C"
    assert postprocess.repair_chapter_numbers(["CHAPTER 1\nA", "CHAPTER 2\nB", "x", "CHAPTER 11\nC"])[3].startswith("CHAPTER 11")


def test_running_heads_with_ocr_noise_removed():
    heads = ["THE ADVENTURES OF TOM", "THE ADVENTURES OF TOM", "THE ADVENTURFS OF TOM", "THE ADVENTURES OF T0M"]
    labels = ["12", "l3", "14", "|5"]
    bodies = ["The rabbit ran across the meadow before noon, quite late.", "Nobody answered when she knocked at the old green door.",
              "A storm gathered over the hills and the river rose fast.", "Later that evening they sat down to a quiet supper at last."]
    pages = [f"{h}\n\n{b}\n\n{l}" for h, b, l in zip(heads, bodies, labels)]
    assert postprocess.strip_running_heads(pages) == bodies


def test_garbage_pages_are_emptied():
    assert postprocess.clean_ocr_pages(["~ . ' ` ^ -- = = ", "Some real sentence that is long enough to count here."]) == \
        ["", "Some real sentence that is long enough to count here."]


def test_doubled_quote_marks_collapse():
    assert postprocess.fix_ocr_text("\'\'The story,\'\' said he, \"\"go on.\"\' It's fine.") == \
        '"The story," said he, "go on." It\'s fine.'


@needs_omp
def test_mixed_pdf_with_ocrmypdf_uses_only_the_image_page():
    book = extract_book(FIX / "mixed_book.pdf", Opts(ocr_backend="ocrmypdf"))
    assert len(book.chapters) == 2 and accuracy(book) > 0.93
    assert "Either the well was very deep" in alltext(book)
    # paragraphs stay paragraphs: no one-word blocks from the text layer
    assert min(len(p.split()) for c in book.chapters for p in c.paragraphs) > 5


@needs_omp
def test_ocrmypdf_text_comes_from_the_sidecar_not_the_text_layer():
    """pdftotext on an OCR text layer can split lines into one-word blocks; the sidecar keeps Tesseract's own lines."""
    from bookbuddy.ocr import ocrmypdf_backend as O
    res = ocr.run_ocr(FIX / "scanned_book.pdf", "eng", 2, backend="ocrmypdf")
    side = O.read_sidecar(res.searchable_pdf, 3)
    assert side is not None and [p.text for p in res.pages] == side


def test_noisy_scan_lines_are_dropped_but_prose_survives():
    t = postprocess.fix_ocr_text("Book 1. &' 5 3: \\_\\\nJ\\kl\\wv,'I'A\"II A Q(IIHH'\n\nHe said \"no, it is 3 o'clock & time to go.\"\nIt was 1905.")
    assert "Book 1" not in t and "Q(" not in t
    assert "He said" in t and "& time to go" in t and "It was 1905." in t


# ------------------------------------------------------------------ DjVu text-layer fast path

def _djvu_with_text(tmp_path, per_page):
    """A copy of the scanned fixture with a real hidden text layer (page 1..3 = the given strings)."""
    d = tmp_path / "textlayer.djvu"
    shutil.copy(FIX / "book_pages.djvu", d)
    for i, t in enumerate(per_page, 1):
        if t is None:
            continue
        txt = tmp_path / f"p{i}.txt"
        txt.write_text('(page 0 0 100 100 "' + t.replace('"', "'") + '")\n')
        subprocess.run(["djvused", str(d), "-e", f"select {i}; set-txt {txt}; save"], check=True, capture_output=True)
    return d


needs_djvu_tools = pytest.mark.skipif(not all(shutil.which(x) for x in ("djvused", "djvutxt", "ddjvu")) or not (FIX / "book_pages.djvu").exists(),
                                      reason="DjVuLibre or fixture missing")

ALICE_PARA = ("Alice was beginning to get very tired of sitting by her sister on the bank, and of having nothing to do. "
              "Once or twice she had peeped into the book her sister was reading, but it had no pictures or conversations in it.")


@needs_djvu_tools
def test_djvu_text_layer_skips_ocr(tmp_path, monkeypatch):
    d = _djvu_with_text(tmp_path, [ALICE_PARA, "The rabbit-hole went straight on like a tunnel for some way, and then dipped suddenly down, "
                                  "so suddenly that Alice had not a moment to think about stopping herself before she found herself falling.",
                                  "Down, down, down. Would the fall never come to an end? She wondered how many miles she had fallen by this time."])
    from bookbuddy import ocr

    def boom(*a, **k):
        raise AssertionError("OCR must not run when the DjVu has a text layer")
    monkeypatch.setattr(ocr, "run_ocr", boom)
    book = extract_book(d, Opts())
    assert "Alice was beginning to get very tired" in alltext(book)
    assert all(ch.source == "djvu-text" for ch in book.chapters)


@needs_djvu_tools
def test_djvu_without_text_layer_falls_back_to_ocr(tmp_path):
    from bookbuddy.extractors import djvu
    assert djvu.text_layer(FIX / "book_pages.djvu") is None            # the plain scan has no text layer
    d = _djvu_with_text(tmp_path, ["x", None, None])                    # one stub page of three is not a real layer
    assert djvu.text_layer(d) is None


def test_djvu_text_layer_pages_are_form_feed_split(tmp_path, monkeypatch):
    from bookbuddy.extractors import djvu
    f = tmp_path / "a.djvu"
    f.write_bytes(b"AT&T")
    monkeypatch.setattr(djvu.shutil, "which", lambda n: "/usr/bin/djvutxt")
    monkeypatch.setattr(djvu.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=(ALICE_PARA + "\f" + ALICE_PARA + "\f").encode()))
    assert djvu.text_layer(f) == [ALICE_PARA, ALICE_PARA]
