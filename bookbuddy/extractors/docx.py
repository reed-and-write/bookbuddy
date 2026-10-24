"""Word (.docx) via python-docx: Heading 1/2 styles are chapters, core properties give title/author.

Headers, footers, footnotes and endnotes are never read; tables are skipped unless `[extract] tables = "read"`.
The first embedded image becomes the cover when it comes before any text and is portrait-shaped.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import config
from ..book import Book
from . import common
from .common import Block, FormatInfo

extensions = (".docx",)

_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_HEADING = re.compile(r"^(?:heading|überschrift|titre|título|titolo)\s*(\d)$", re.I)


def _style_level(style) -> int:
    """0 = body, 1..6 = heading level, -1 = document title style."""
    seen = 0
    while style is not None and seen < 8:
        name = (style.name or "").strip()
        m = _HEADING.match(name)
        if m:
            return min(int(m.group(1)), 6)
        if name.lower() == "title":
            return -1
        style = style.base_style
        seen += 1
    return 0


def _outline_level(p) -> int:
    el = p._p.find(f"{{{_NS_W}}}pPr/{{{_NS_W}}}outlineLvl")
    if el is not None:
        try:
            return min(int(el.get(f"{{{_NS_W}}}val")) + 1, 6)
        except (TypeError, ValueError):
            return 0
    return 0


def _paragraph_text(p) -> str:
    return common.clean_inline(p.text.replace("\t", " "))


def _first_cover(doc, p, seen_text: bool) -> tuple[bytes, str] | None:
    """Image in paragraph `p` if it looks like a cover: before any text, taller than wide, not tiny."""
    if seen_text:
        return None
    for blip in p._p.iter(f"{{{_NS_A}}}blip"):
        rid = blip.get(f"{{{_NS_R}}}embed")
        if not rid or rid not in doc.part.related_parts:
            continue
        ext_el = next(p._p.iter(f"{{{_NS_WP}}}extent"), None)
        if ext_el is not None:
            cx, cy = int(ext_el.get("cx", 0)), int(ext_el.get("cy", 0))
            if not (cx and cy and cy >= 1.15 * cx and cx > 914400):       # portrait and wider than an inch
                continue
        part = doc.part.related_parts[rid]
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif"}.get(part.content_type)
        if ext:
            return part.blob, ext
    return None


def docx_blocks(path: Path, tables: str = "skip") -> tuple[list[Block], dict, tuple[bytes, str] | None]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        doc = docx.Document(str(path))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"could not open the Word file ({e})") from e
    cp = doc.core_properties
    meta = {"title": cp.title or "", "author": cp.author or ""}
    blocks: list[Block] = []
    cover = None
    seen_text = False
    doc_title = ""
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "tbl":
            if tables == "read":
                for row in Table(child, doc).rows:
                    cells, last = [], None
                    for c in row.cells:
                        if c._tc is not last and c.text.strip():
                            cells.append(common.clean_inline(c.text))
                        last = c._tc
                    if cells:
                        blocks.append((0, ", ".join(cells) + "."))
                seen_text = True
            continue
        if tag != "p":
            continue
        p = Paragraph(child, doc)
        if cover is None and not seen_text:
            cover = _first_cover(doc, p, seen_text)
        text = _paragraph_text(p)
        if not text:
            continue
        level = _style_level(p.style)
        if level == 0:
            level = _outline_level(p)
        if level == -1:
            doc_title = doc_title or text
            continue
        if level == 0 and (p.style.name or "").lower() in ("subtitle",) and not seen_text:
            continue
        seen_text = True
        blocks.append((level, text))
    meta["doc_title"] = doc_title
    return blocks, meta, cover


def can_handle(path) -> bool:
    return Path(path).suffix.lower() == ".docx"


def extract(path, opts=None) -> Book:
    path = Path(path)
    blocks, meta, cover = docx_blocks(path, config.get_settings().tables)
    return common.blocks_to_book(blocks, title=meta["title"] or meta["doc_title"], author=meta["author"], path=path,
                                 source="docx", cover=cover[0] if cover else None, cover_ext=cover[1] if cover else "jpg",
                                 max_chapters=getattr(opts, "max_chapters", None))


def formats() -> list[FormatInfo]:
    return [FormatInfo("Word", (".docx",), "python-docx (Heading 1/2 become chapters)")]
