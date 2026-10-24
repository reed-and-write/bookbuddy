"""Calibre-backed formats (fake ebook-convert), DRM detection, `bookbuddy formats`, watcher extensions."""
import json
import os
import shutil
import stat
import struct
import sys
import zipfile
from pathlib import Path

import pytest

from bookbuddy import cli, config
from bookbuddy.extractors import calibre, drm, extract_book, list_formats, supported_extensions
from bookbuddy.watcher import is_candidate, scan

FIX = Path(__file__).parent / "fixtures"
DRM_TEXT = "This book is DRM-protected. Book Buddy can't read DRM-protected files."
CALIBRE_TEXT = "This format needs Calibre (free). Install it from calibre-ebook.com, then try again."

FAKE = """#!{py}
import os, shutil, sys
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_FAIL") == "drm":
    sys.stderr.write("calibre.ebooks.DRMError: This book is protected by DRM\\n"); sys.exit(1)
if os.environ.get("FAKE_FAIL") == "other":
    sys.stderr.write("Traceback: something broke\\n"); sys.exit(1)
shutil.copyfile(os.environ["FAKE_EPUB"], sys.argv[2])
"""


@pytest.fixture
def fake_calibre(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "ebook-convert"
    exe.write_text(FAKE.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_EPUB", str(FIX / "alice_formats.epub"))
    monkeypatch.setenv("BOOKBUDDY_CACHE", str(tmp_path / "cache"))      # the conversion cache is keyed by file content
    config.reload_settings()
    return log


@pytest.fixture
def no_calibre(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))          # nothing on PATH
    monkeypatch.setattr(calibre, "_EXTRA_LOCATIONS", ())


def mobi_bytes(encryption=0, magic=b"BOOKMOBI") -> bytes:
    head = bytearray(b"Test Book".ljust(32, b"\0"))
    head += b"\0" * 28 + magic                      # type/creator at 60..68
    head = bytearray(head.ljust(76, b"\0"))
    head += struct.pack(">H", 1)                    # one record
    head += struct.pack(">I", 100) + b"\0\0\0\0"    # record 0 at offset 100
    head = head.ljust(100, b"\0")
    rec0 = struct.pack(">HHIHHH", 2, 0, 1000, 5, 4096, encryption)   # compression, _, length, records, size, encryption
    return bytes(head + rec0 + b"\0" * 200)


# ------------------------------------------------------------------ Calibre path

@pytest.mark.parametrize("ext", [".mobi", ".azw3", ".lit", ".pdb", ".lrf", ".snb", ".tcr", ".cbc", ".azw", ".kfx"])
def test_calibre_conversion_with_fake_converter(tmp_path, fake_calibre, ext):
    src = tmp_path / f"My Book - Some Author{ext}"
    src.write_bytes(mobi_bytes() if ext in calibre.KINDLE else b"some bytes " + ext.encode())
    book = extract_book(src)
    assert book.title == "Alice's Adventures in Wonderland" and len(book.chapters) == 3
    call = fake_calibre.read_text().split()
    assert call[0].endswith(ext) and call[1].endswith(".epub")


def test_calibre_conversion_is_cached_by_source_hash(tmp_path, fake_calibre):
    a, b = tmp_path / "a.mobi", tmp_path / "copy-of-a.mobi"
    a.write_bytes(mobi_bytes())
    shutil.copy(a, b)
    extract_book(a)
    extract_book(a)
    extract_book(b)                                         # same bytes, different name: still cached
    assert len(fake_calibre.read_text().splitlines()) == 1
    c = tmp_path / "c.mobi"
    c.write_bytes(mobi_bytes() + b"different")
    extract_book(c)
    assert len(fake_calibre.read_text().splitlines()) == 2


def test_calibre_missing_message(tmp_path, no_calibre):
    src = tmp_path / "a.lit"
    src.write_bytes(b"x")
    with pytest.raises(calibre.CalibreMissing) as e:
        extract_book(src)
    assert str(e.value) == CALIBRE_TEXT


def test_calibre_failure_reports_tail(tmp_path, fake_calibre, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "other")
    src = tmp_path / "a.mobi"
    src.write_bytes(mobi_bytes())
    with pytest.raises(RuntimeError, match="something broke"):
        extract_book(src)


def test_calibre_drm_failure_message(tmp_path, fake_calibre, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "drm")
    src = tmp_path / "a.lit"
    src.write_bytes(b"x")
    with pytest.raises(drm.DRMError) as e:
        extract_book(src)
    assert str(e.value).startswith(DRM_TEXT)


def test_calibre_title_fallback_uses_source_name(tmp_path, fake_calibre, monkeypatch):
    # an EPUB with no usable metadata title gets the source file's name
    from helpers import make_epub
    epub = make_epub(tmp_path / "nometa.epub", [("Chapter I. A", ["Word " * 60]), ("Chapter II. B", ["Word " * 60])],
                     title="", author="", front=False, cover=False)
    monkeypatch.setenv("FAKE_EPUB", str(epub))
    src = tmp_path / "Lost Title - Jo Writer.azw3"
    src.write_bytes(mobi_bytes())
    book = extract_book(src)
    assert (book.title, book.author) == ("Lost Title", "Jo Writer")


@pytest.mark.skipif(shutil.which("ebook-convert") is None, reason="Calibre (ebook-convert) is not installed")
def test_calibre_integration_real(tmp_path):
    """Convert a real EPUB to MOBI with Calibre, then read the MOBI back through the Calibre extractor."""
    import subprocess
    mobi = tmp_path / "alice.mobi"
    subprocess.run(["ebook-convert", str(FIX / "alice_formats.epub"), str(mobi)], check=True, capture_output=True)
    book = extract_book(mobi)
    assert len(book.chapters) >= 3 and "Alice was beginning" in " ".join(book.chapters[0].paragraphs)


# ------------------------------------------------------------------ DRM

def test_drm_encrypted_epub():
    with pytest.raises(drm.DRMError) as e:
        extract_book(FIX / "drm_encrypted.epub")
    assert str(e.value).startswith(DRM_TEXT) and "ch0.xhtml" in e.value.detail


def test_font_obfuscated_epub_still_works():
    book = extract_book(FIX / "fonts_obfuscated.epub")
    assert len(book.chapters) == 3


def _epub_with(tmp_path, name, data):
    out = tmp_path / "x.epub"
    with zipfile.ZipFile(FIX / "alice_formats.epub") as zi, zipfile.ZipFile(out, "w") as zo:
        for item in zi.infolist():
            zo.writestr(item, zi.read(item.filename))
        zo.writestr(name, data)
    return out


@pytest.mark.parametrize("marker", ["META-INF/rights.xml", "META-INF/sinf.xml"])
def test_epub_adept_and_fairplay_markers(tmp_path, marker):
    with pytest.raises(drm.DRMError):
        extract_book(_epub_with(tmp_path, marker, "<rights/>"))


def test_epub_image_encryption_is_drm_but_fonts_only_is_not(tmp_path):
    def enc(uri, algo):
        return ('<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
                f'<enc:EncryptedData><enc:EncryptionMethod Algorithm="{algo}"/><enc:CipherData><enc:CipherReference URI="{uri}"/>'
                '</enc:CipherData></enc:EncryptedData></encryption>')
    aes = "http://www.w3.org/2001/04/xmlenc#aes128-cbc"
    with pytest.raises(drm.DRMError):
        extract_book(_epub_with(tmp_path, "META-INF/encryption.xml", enc("images/c.jpg", aes)))
    # a font encrypted with any algorithm is not what DRM is about; a font obfuscated by IDPF is plainly fine
    assert extract_book(_epub_with(tmp_path, "META-INF/encryption.xml", enc("f.woff", aes)))
    # an empty encryption.xml is fine
    assert extract_book(_epub_with(tmp_path, "META-INF/encryption.xml", "<encryption xmlns='urn:oasis:names:tc:opendocument:xmlns:container'/>"))


def test_pdf_password_is_drm():
    with pytest.raises(drm.DRMError) as e:
        extract_book(FIX / "password.pdf")
    assert str(e.value).startswith(DRM_TEXT)


def test_pdf_owner_password_only_is_fine():
    book = extract_book(FIX / "owner_only.pdf")
    assert "Alice was beginning" in " ".join(p for c in book.chapters for p in c.paragraphs)


def test_pdf_error_text_detection():
    drm.check_pdf_error("Syntax Error: whatever")                      # no raise
    for msg in ("Command Line Error: Incorrect password", "Error: Copying of text from this document is not allowed.",
                "Unknown security handler 'Adobe.PubSec'"):
        with pytest.raises(drm.DRMError):
            drm.check_pdf_error(msg)


@pytest.mark.parametrize("data", [mobi_bytes(encryption=2), mobi_bytes(encryption=1), b"TPZ0" + b"\0" * 200,
                                  b"\xeaDRMION\xee" + b"\0" * 200, b"\0" * 100 + b"\xeaDRMION\xee"])
def test_kindle_drm_markers_checked_before_calibre(tmp_path, no_calibre, data):
    src = tmp_path / "locked.azw3"
    src.write_bytes(data)
    with pytest.raises(drm.DRMError) as e:                              # DRM wins even when Calibre is missing
        extract_book(src)
    assert str(e.value).startswith(DRM_TEXT)


def test_unprotected_mobi_reaches_calibre_check(tmp_path, no_calibre):
    src = tmp_path / "open.mobi"
    src.write_bytes(mobi_bytes(encryption=0))
    with pytest.raises(calibre.CalibreMissing):
        extract_book(src)


# ------------------------------------------------------------------ formats list, CLI, watcher

def test_list_formats_marks_calibre_unavailable(no_calibre):
    rows = {r.name: r for r in list_formats()}
    assert rows["EPUB"].available and rows["Plain text"].available
    k = rows["Kindle (MOBI, AZW, AZW3, KFX)"]
    assert not k.available and "Calibre" in k.note and ".azw3" in k.extensions


def test_list_formats_available_with_calibre(fake_calibre):
    assert all(r.available for r in list_formats())


def test_formats_cli_text_and_json(no_calibre, capsys):
    assert cli.main(["formats"]) == 0
    out = capsys.readouterr().out
    for word in ("EPUB", "PDF", "Markdown", "FictionBook 2", ".fb2.zip", "UNAVAILABLE", "calibre-ebook.com"):
        assert word in out
    assert cli.main(["formats", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {"name", "extensions", "how", "needs", "available", "note"} <= set(rows[0])
    assert any(r["needs"] == "Calibre" and not r["available"] for r in rows)


def test_watcher_accepts_all_new_extensions(tmp_path):
    names = ["a.txt", "b.md", "c.markdown", "d.html", "e.htm", "f.xhtml", "g.docx", "h.odt", "i.rtf", "j.fb2", "k.fb2.zip",
             "l.mobi", "m.azw", "n.azw3", "o.kfx", "p.lit", "q.pdb", "r.lrf", "s.snb", "t.tcr", "u.cbc", "V.DOCX", "w.zip", "x.cbz", "y.cbr", "z.djvu"]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    for n in ["x.jpg", "y.fb2.zip.part", "z.docx.tmp", ".h.docx"]:
        (tmp_path / n).write_bytes(b"x")
    assert sorted(p.name for p in scan(tmp_path)) == sorted(names)
    assert ".fb2.zip" in supported_extensions() and not is_candidate(tmp_path / "x.jpg")
