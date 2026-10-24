"""Build tiny EPUBs for tests."""
from pathlib import Path

from ebooklib import epub

def _png(w=8, h=8):
    import struct, zlib
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes([200, 30, 30]) * w for _ in range(h))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


PNG = _png()


def make_epub(path: Path, chapters, title="Test Book", author="A. Writer", front=True, cover=True, with_toc=True):
    """chapters: list of (title, [paragraphs])"""
    b = epub.EpubBook()
    b.set_identifier("id123")
    b.set_title(title)
    b.set_language("en")
    b.add_author(author)
    if cover:
        b.set_cover("cover.png", PNG)
    items = []
    if front:
        c = epub.EpubHtml(title="Copyright", file_name="copyright.xhtml", lang="en")
        c.content = "<h1>Copyright</h1><p>Copyright 2020. All rights reserved. ISBN 978-3-16-148410-0.</p>"
        b.add_item(c); items.append(c)
        t = epub.EpubHtml(title="Contents", file_name="toc.xhtml", lang="en")
        t.content = "<h1>Contents</h1>" + "".join(f"<p>{n}</p>" for n, _ in chapters)
        b.add_item(t); items.append(t)
    for i, (name, paras) in enumerate(chapters):
        c = epub.EpubHtml(title=name, file_name=f"ch{i}.xhtml", lang="en")
        c.content = f"<h2>{name}</h2>" + "".join(f"<p>{p}</p>" for p in paras)
        b.add_item(c); items.append(c)
    if front:
        a = epub.EpubHtml(title="Also by A. Writer", file_name="also.xhtml", lang="en")
        a.content = "<h1>Also by A. Writer</h1><p>Another Book</p>"
        b.add_item(a); items.append(a)
    b.toc = tuple(items) if with_toc else ()
    b.add_item(epub.EpubNcx())
    b.add_item(epub.EpubNav())
    b.spine = ["nav"] + items if False else items
    epub.write_epub(str(path), b)
    return path


SAMPLE_CHAPTERS = [
    ("Chapter I. The Start", ["It was a dark night. Mr. Smith walked home in 1984.", "He had $5.50 and 3 apples.[1]"]),
    ("Chapter II. The Middle", ["She said, “Hello there!” Then she left. " * 3, "A final paragraph here, with enough words to matter."]),
    ("Chapter III. The End", ["The end came quickly. Nobody was surprised by it at all."]),
]
