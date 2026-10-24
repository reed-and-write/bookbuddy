"""OpenDocument text (.odt): content.xml + meta.xml read directly (zip + lxml); outline levels become chapters.

Headers/footers live in styles.xml and are never read; footnotes (text:note) are dropped; tables are skipped unless
`[extract] tables = "read"`.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from lxml import etree

from .. import config
from ..book import Book
from . import common
from .common import Block, FormatInfo

extensions = (".odt",)

NS = {"text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
      "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
      "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
      "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
      "dc": "http://purl.org/dc/elements/1.1/",
      "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"}


def _q(prefix: str, name: str) -> str:
    return f"{{{NS[prefix]}}}{name}"


_SKIP = {_q("text", "note"), _q("text", "note-citation"), _q("text", "note-body"), _q("text", "bookmark"),
         _q("text", "tracked-changes"), _q("text", "sequence-decls"), _q("draw", "frame"), _q("office", "annotation")}


def inline_text(el) -> str:
    out: list[str] = []

    def walk(e) -> None:
        if e.text:
            out.append(e.text)
        for ch in e:
            tag = ch.tag
            if tag in _SKIP:
                pass
            elif tag == _q("text", "s"):
                out.append(" " * int(ch.get(_q("text", "c"), "1")))
            elif tag in (_q("text", "tab"), _q("text", "line-break")):
                out.append(" ")
            else:
                walk(ch)
            if ch.tail:
                out.append(ch.tail)

    walk(el)
    return common.clean_inline("".join(out))


def odt_blocks(path: Path, tables: str = "skip") -> tuple[list[Block], dict]:
    try:
        with zipfile.ZipFile(path) as z:
            content = etree.fromstring(z.read("content.xml"), etree.XMLParser(resolve_entities=False, no_network=True))
            meta_xml = etree.fromstring(z.read("meta.xml"), etree.XMLParser(resolve_entities=False, no_network=True)) \
                if "meta.xml" in z.namelist() else None
    except (zipfile.BadZipFile, KeyError, etree.XMLSyntaxError) as e:
        raise ValueError(f"could not open the OpenDocument file ({e})") from e
    meta = {"title": "", "author": ""}
    if meta_xml is not None:
        t = meta_xml.find(".//dc:title", NS)
        a = meta_xml.find(".//meta:initial-creator", NS)
        a2 = meta_xml.find(".//dc:creator", NS)
        meta["title"] = common.clean_meta(t.text if t is not None else "")
        meta["author"] = common.clean_meta((a.text if a is not None else "") or (a2.text if a2 is not None else ""))
    body = content.find(".//office:text", NS)
    if body is None:
        raise ValueError("this OpenDocument file has no text")
    blocks: list[Block] = []

    def walk(container) -> None:
        for el in container:
            tag = el.tag
            if tag == _q("text", "h"):
                t = inline_text(el)
                if t:
                    blocks.append((min(int(el.get(_q("text", "outline-level"), "1")), 6), t))
            elif tag == _q("text", "p"):
                t = inline_text(el)
                if t:
                    blocks.append((0, t))
            elif tag in (_q("text", "list"), _q("text", "list-item"), _q("text", "section"), _q("text", "list-header"),
                         _q("text", "index-body")):
                walk(el)
            elif tag == _q("table", "table") and tables == "read":
                for row in el.iter(_q("table", "table-row")):
                    cells = [inline_text(c) for c in row.iter(_q("text", "p"))]
                    cells = [c for c in cells if c]
                    if cells:
                        blocks.append((0, ", ".join(cells) + "."))
            elif tag == _q("text", "table-of-content") or tag == _q("text", "alphabetical-index"):
                continue

    walk(body)
    return blocks, meta


def can_handle(path) -> bool:
    return Path(path).suffix.lower() == ".odt"


def extract(path, opts=None) -> Book:
    path = Path(path)
    blocks, meta = odt_blocks(path, config.get_settings().tables)
    return common.blocks_to_book(blocks, title=meta["title"], author=meta["author"], path=path, source="odt",
                                 max_chapters=getattr(opts, "max_chapters", None))


def formats() -> list[FormatInfo]:
    return [FormatInfo("OpenDocument text", (".odt",), "built in (reads content.xml directly)")]
