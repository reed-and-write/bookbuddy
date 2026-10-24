"""EPUB extractor: spine + TOC -> Book, strips front/back matter."""
from __future__ import annotations

import posixpath
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urldefrag

import ebooklib
from bs4 import BeautifulSoup, NavigableString, Tag, XMLParsedAsHTMLWarning
from ebooklib import epub

from ..book import Book, Chapter
from ..textnorm import display_title, normalize_text
from . import drm

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
warnings.filterwarnings("ignore", module="ebooklib")

BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "tr", "section",
              "article", "pre", "dd", "dt", "figcaption", "header", "footer", "ul", "ol", "table",
              "tbody", "thead", "caption", "body", "hr", "aside", "main", "nav"}
SKIP_TAGS = {"script", "style", "head", "svg", "math", "img", "image", "figure"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

FRONT_BACK_TITLE = re.compile(
    r"""^\W*(
        cover(\s*page)? | title(\s*page)? | half[\s-]*title(\s*page)? | series\s*page |
        copyright(s)?(\s*(page|notice|information))? | (table\s+of\s+)?contents | dedication |
        acknowledg(e)?ments? | also\s+by\b.* | other\s+(books|titles)\b.* | books\s+by\b.* |
        by\s+the\s+same\s+author.* | about\s+the\s+(author|authors|publisher|type)\b.* | praise\s+for\b.* |
        colophon | imprint | list\s+of\s+(illustrations|figures|maps|tables) | illustrations |
        index | bibliography | endnotes | footnotes | credits | permissions | newsletter.* |
        a\s+note\s+on\s+the\s+(text|type|typography).* | sign\s+up\b.* | join\s+(our|the)\b.* |
        preview(\s+of\b.*)? | excerpt\s+from\b.* | coming\s+soon.* | also\s+available.* |
        (the\s+)?(full\s+)?project\s+gutenberg\W*.* | transcriber.*notes? | license | licence |
        copyright\s+and\s+licen[sc]ing.* | nav(igation)? | landmarks | start\s+reading | begin\s+reading
    )\W*$""",
    re.I | re.X,
)
FRONT_BACK_BODY = re.compile(
    r"all rights reserved|no part of this (book|publication)|library of congress|isbn[\s:-]*[\d-]{9,}|"
    r"project gutenberg(?:-tm|™)? (license|eBook of)|terms of the project gutenberg",
    re.I,
)
CHAPTERISH = re.compile(r"\b(chapter|part|book|prologue|epilogue|preface|foreword|introduction|prelude|afterword|canto|act)\b|^\W*[ivxlcdm\d]+\W*$", re.I)


# ------------------------------------------------------------------ HTML -> blocks

def html_to_blocks(html: bytes | str) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """Return ([(block_tag, raw_text)], {anchor_id: index of the block it starts})."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        soup = BeautifulSoup(html, "lxml")
    blocks: list[tuple[str, str]] = []
    anchors: dict[str, int] = {}
    buf: list[str] = []
    cur_tag = ["p"]

    def flush() -> None:
        txt = re.sub(r"\s+", " ", "".join(buf)).strip()
        buf.clear()
        if txt:
            blocks.append((cur_tag[0], txt))

    def mark(el: Tag) -> None:
        for key in ("id", "name"):
            v = el.get(key)
            if v:
                anchors.setdefault(v, len(blocks))

    def walk(el: Tag) -> None:
        for ch in el.children:
            if isinstance(ch, NavigableString):
                if type(ch).__name__ in ("Comment", "Doctype", "ProcessingInstruction", "Declaration"):
                    continue
                buf.append(str(ch))
                continue
            if not isinstance(ch, Tag):
                continue
            name = ch.name.lower()
            if name in SKIP_TAGS:
                continue
            if name == "br":
                buf.append(" ")
                continue
            if name == "sup" or (name == "a" and "noteref" in " ".join(ch.get("class", []) + [ch.get("epub:type", "")])):
                if re.fullmatch(r"\W*[\d*†‡a-z]{0,3}\W*", ch.get_text()) and ch.get_text().strip() not in ("th", "st", "nd", "rd"):
                    continue
            if name in BLOCK_TAGS:
                flush()
                mark(ch)
                cur_tag[0] = name
                if name in ("tr",):
                    # table rows read as one line
                    for cell in ch.find_all(["td", "th"]):
                        buf.append(cell.get_text(" ") + " ")
                    flush()
                    continue
                walk(ch)
                flush()
                cur_tag[0] = "p"
            else:
                mark(ch)
                walk(ch)

    body = soup.body or soup
    mark(body)
    walk(body)
    flush()
    return blocks, anchors


# ------------------------------------------------------------------ TOC

def _flatten_toc(toc, out=None):
    out = [] if out is None else out
    for node in toc:
        if isinstance(node, tuple):
            sec, kids = node
            out.append((sec.title, sec.href))
            _flatten_toc(kids, out)
        elif isinstance(node, (list,)):
            _flatten_toc(node, out)
        else:
            out.append((node.title, node.href))
    return out


def _norm_href(href: str) -> tuple[str, str]:
    path, frag = urldefrag(unquote(href))
    return posixpath.normpath(path), frag


def _meta(book: epub.EpubBook, key: str) -> str:
    vals = book.get_metadata("DC", key)
    return vals[0][0].strip() if vals and vals[0][0] else ""


def _extract_cover(book: epub.EpubBook) -> tuple[bytes | None, str]:
    cover_item = None
    for item in book.get_items_of_type(ebooklib.ITEM_COVER):
        cover_item = item
        break
    if cover_item is None:
        for _, meta in (book.get_metadata("OPF", "meta") or []):
            if meta.get("name") == "cover":
                cover_item = book.get_item_with_id(meta.get("content", ""))
                if cover_item is None:
                    for it in book.get_items():
                        if it.get_id() == meta.get("content"):
                            cover_item = it
                if cover_item:
                    break
    if cover_item is None:
        for it in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            if re.search(r"cover", it.get_name(), re.I):
                cover_item = it
                break
    if cover_item is None:
        return None, "jpg"
    ext = posixpath.splitext(cover_item.get_name())[1].lstrip(".").lower() or "jpg"
    ext = "jpg" if ext == "jpeg" else ext
    return cover_item.get_content(), ext


# ------------------------------------------------------------------ segmentation

@dataclass
class _Seg:
    title: str
    blocks: list[tuple[str, str]]
    source: str
    from_toc: bool = True


def _segments(book: epub.EpubBook) -> list[_Seg]:
    toc = [(t, *_norm_href(h)) for t, h in _flatten_toc(book.toc) if h]
    by_doc: dict[str, list[tuple[str, str]]] = {}
    for title, doc, frag in toc:
        by_doc.setdefault(doc, []).append((title, frag))

    spine_items = []
    for entry in book.spine:
        item = book.get_item_with_id(entry[0] if isinstance(entry, tuple) else entry)
        if item is not None and item.get_type() == ebooklib.ITEM_DOCUMENT:
            spine_items.append(item)

    segs: list[_Seg] = []
    for item in spine_items:
        name = posixpath.normpath(item.get_name())
        blocks, anchors = html_to_blocks(item.get_content())
        entries = by_doc.get(name)
        if not entries:
            # not in the TOC: continuation of the previous chapter (or front matter if first)
            segs.append(_Seg(_guess_title(blocks, name), blocks, name, from_toc=False))
            continue
        starts = []
        for title, frag in entries:
            starts.append((anchors.get(frag, 0) if frag else 0, title))
        starts.sort(key=lambda x: x[0])
        if starts[0][0] > 0:
            segs.append(_Seg("", blocks[:starts[0][0]], name, from_toc=False))
        for i, (st, title) in enumerate(starts):
            en = starts[i + 1][0] if i + 1 < len(starts) else len(blocks)
            segs.append(_Seg(title, blocks[st:en], name))
    return segs


def _guess_title(blocks, name: str) -> str:
    for tag, txt in blocks[:3]:
        if tag in HEADINGS and len(txt) < 120:
            return txt
    return ""


# ------------------------------------------------------------------ cleaning

_PG_START = re.compile(r"\*\*\*\s*START OF (THE|THIS) PROJECT GUTENBERG", re.I)
_PG_END = re.compile(r"\*\*\*\s*END OF (THE|THIS) PROJECT GUTENBERG", re.I)


def _trim_gutenberg(blocks):
    for i, (_, t) in enumerate(blocks):
        if _PG_START.search(t):
            blocks = blocks[i + 1:]
            break
    for i, (_, t) in enumerate(blocks):
        if _PG_END.search(t):
            blocks = blocks[:i]
            break
    return blocks


def _strip_title_blocks(blocks, title: str):
    """Drop leading heading blocks that merely repeat the chapter title (it is announced separately)."""
    words = set(re.findall(r"\w+", title.lower()))
    out = list(blocks)
    for _ in range(3):
        if not out or out[0][0] not in HEADINGS:
            break
        hw = set(re.findall(r"\w+", out[0][1].lower()))
        if not words or not hw or len(hw & words) / len(hw) >= 0.6:
            out.pop(0)
        else:
            break
    return out


def _is_front_back(title: str, text: str, book_title: str, first: bool) -> str | None:
    t = re.sub(r"\s+", " ", title).strip()
    if t and FRONT_BACK_TITLE.match(t):
        return f"title matches front/back matter ({t!r})"
    if t and book_title and re.sub(r"\W+", "", t.lower()) == re.sub(r"\W+", "", book_title.lower()):
        return "title page"
    if FRONT_BACK_BODY.search(text[:4000]) and len(text) < 6000:
        return "copyright/licence boilerplate"
    if len(text) < 150 and not CHAPTERISH.search(t):
        return "almost no text"
    return None


def extract_epub(path: str, max_chapters: int | None = None) -> Book:
    drm.check_epub_path(Path(path))
    ebook = epub.read_epub(str(path), options={"ignore_ncx": False})
    title = _meta(ebook, "title") or posixpath.splitext(posixpath.basename(str(path)))[0]
    author = _meta(ebook, "creator") or "Unknown"
    cover, cover_ext = _extract_cover(ebook)

    chapters: list[Chapter] = []
    dropped: list[tuple[str, str]] = []
    pending: list[tuple[str, str]] = []     # blocks of untitled leading segments
    for seg in _segments(ebook):
        blocks = _trim_gutenberg(seg.blocks)
        disp = display_title(seg.title)
        if not seg.from_toc and not seg.title:
            # continuation text: belongs to previous chapter when one exists
            if chapters:
                chapters[-1].paragraphs.extend(filter(None, (normalize_text(t) for _, t in blocks)))
            continue
        raw_text = " ".join(t for _, t in blocks)
        reason = _is_front_back(disp, raw_text, title, first=not chapters)
        if reason:
            dropped.append((disp or seg.source, reason))
            continue
        if not seg.from_toc and chapters and len(raw_text) < 800:
            chapters[-1].paragraphs.extend(filter(None, (normalize_text(t) for _, t in blocks)))
            continue
        blocks = _strip_title_blocks(blocks, disp)
        paras = [p for p in (normalize_text(t) for _, t in blocks) if p]
        if not paras:
            dropped.append((disp or seg.source, "empty after cleaning"))
            continue
        chapters.append(Chapter(disp or f"Section {len(chapters) + 1}", paras, seg.source))

    if not chapters:
        raise ValueError("no readable chapters found in EPUB")
    if max_chapters:
        chapters = chapters[:max_chapters]
    return Book(title=title, author=author, chapters=chapters, cover=cover, cover_ext=cover_ext, dropped=dropped)


# ------------------------------------------------------------------ extractor plugin interface

extensions = (".epub",)


def can_handle(path) -> bool:
    return Path(path).suffix.lower() == ".epub"


def extract(path, opts=None) -> Book:
    return extract_epub(str(path), getattr(opts, "max_chapters", None))


def formats():
    from .common import FormatInfo
    return [FormatInfo("EPUB", (".epub",), "built in (ebooklib); DRM-protected files are refused")]
