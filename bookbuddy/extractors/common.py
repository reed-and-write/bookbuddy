"""Shared building blocks for the text-based extractors (TXT, Markdown, HTML, DOCX, ODT, RTF, FB2).

An extractor turns its format into a flat list of *blocks* -- (level, text) where level 0 is a body paragraph and
1..6 is a heading -- and hands it to `blocks_to_book`, which picks the heading level that makes sensible chapters,
drops front/back matter, normalises the text (the same `normalize_text` the EPUB and PDF paths use) and falls back to
"Part N" sections when a document has no usable headings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..book import Book, Chapter
from ..textnorm import display_title, normalize_text
from .epub import FRONT_BACK_BODY, FRONT_BACK_TITLE

Block = tuple[int, str]           # (level, text): 0 = paragraph, 1..6 = heading

MIN_PREAMBLE_WORDS = 100          # text before the first chapter heading is kept only when it is this long
MIN_CHAPTER_WORDS = 15            # shorter "chapters" are headings with nothing under them (a table of contents)


@dataclass
class FormatInfo:
    """One row of the `bookbuddy formats` table (and of the future web page's format list)."""
    name: str
    extensions: tuple[str, ...]
    how: str                      # what reads it
    needs: str = ""               # optional dependency, e.g. "Calibre"
    available: bool = True
    note: str = ""                # why it is not available, or caveats

    def to_dict(self) -> dict:
        return {"name": self.name, "extensions": list(self.extensions), "how": self.how, "needs": self.needs,
                "available": self.available, "note": self.note}


# ------------------------------------------------------------------ metadata

_JUNK_AUTHOR = re.compile(r"^(unknown|anonymous|admin(istrator)?|user|owner|author|none|n/?a|null|python-docx|"
                          r"microsoft.*|openoffice.*|libreoffice.*|\W*)$", re.I)
_JUNK_TITLE = re.compile(r"^(untitled( document)?|document\d*|microsoft word.*|new document|\W*)$", re.I)


def clean_meta(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def resolve_title_author(path: Path, title: str = "", author: str = "", hint: str = "") -> tuple[str, str]:
    """Metadata first, then a heading found in the text (`hint`), then 'Title - Author.ext' in the file name."""
    from .pdf import pretty_stem, title_author_from_filename
    title, author = clean_meta(title), clean_meta(author)
    if _JUNK_TITLE.match(title):
        title = ""
    if _JUNK_AUTHOR.match(author):
        author = ""
    f_title, f_author = title_author_from_filename(Path(path))
    if not title:
        title = clean_meta(hint) or f_title or pretty_stem(Path(path))
    if not author:
        author = f_author
    return title, author or "Unknown"


def stem_of(path: Path) -> Path:
    """The path without a compound suffix such as .fb2.zip, for file-name based titles."""
    p = Path(path)
    return p.with_suffix("") if p.suffix.lower() == ".zip" and p.stem.lower().endswith(".fb2") else p


# ------------------------------------------------------------------ chapters

def matter_reason(title: str, text: str) -> str | None:
    t = re.sub(r"\s+", " ", title).strip()
    if t and FRONT_BACK_TITLE.match(t):
        return f"title matches front/back matter ({t!r})"
    if FRONT_BACK_BODY.search(text[:4000]) and len(text) < 6000:
        return "copyright/licence boilerplate"
    return None


def pick_chapter_level(blocks: list[Block]) -> int | None:
    """The heading level that gives chapters: the shallowest one used at least twice. A lone shallower heading is
    the document title (so '# Book' followed by '## Chapter' lines read as chapters)."""
    counts: dict[int, int] = {}
    for lv, _ in blocks:
        if lv:
            counts[lv] = counts.get(lv, 0) + 1
    for lv in sorted(counts):
        if counts[lv] >= 2:
            return lv
    return None


def _words(paras: list[str]) -> int:
    return sum(len(p.split()) for p in paras)


def _speakable(paras: list[str]) -> list[str]:
    return [p for p in (normalize_text(t) for t in paras) if p]


def _heading_paragraph(text: str) -> str:
    t = text.strip()
    return t if re.search(r"[.!?:;\"')]$", t) else t + "."


def blocks_to_book(blocks: list[Block], *, title: str = "", author: str = "", path: Path, source: str,
                   cover: bytes | None = None, cover_ext: str = "jpg", max_chapters: int | None = None,
                   title_hint: str = "") -> Book:
    blocks = [(lv, re.sub(r"\s+", " ", t).strip()) for lv, t in blocks]
    blocks = [(lv, t) for lv, t in blocks if t]
    level = pick_chapter_level(blocks)
    dropped: list[tuple[str, str]] = []
    # a lone heading above the chapter level (or the only heading) is the document title
    hint = title_hint
    if not hint:
        for lv, t in blocks:
            if lv and (level is None or lv < level):
                hint = t
                break
    chapters: list[Chapter] = []

    if level is not None:
        sections: list[tuple[str, list[str]]] = []
        pre: list[str] = []
        cur: tuple[str, list[str]] | None = None
        for lv, t in blocks:
            if lv == level:
                cur = (t, [])
                sections.append(cur)
            elif cur is None:
                if lv == 0:
                    pre.append(t)           # headings above the chapter level are the title block
            else:
                cur[1].append(t if lv == 0 else _heading_paragraph(t))
        if pre and _words(pre) >= MIN_PREAMBLE_WORDS:
            sections.insert(0, ("Introduction", pre))
        elif pre:
            dropped.append(("front matter", f"{_words(pre)} words before the first chapter heading"))
        for name, paras in sections:
            ttl = display_title(name)
            paras = _speakable(paras)
            if paras and paras[0].rstrip(".").lower() == ttl.rstrip(".").lower():
                paras = paras[1:]
            reason = matter_reason(ttl, " ".join(paras))
            if reason:
                dropped.append((ttl, reason))
            elif _words(paras) < MIN_CHAPTER_WORDS:
                dropped.append((ttl, "almost no text"))
            else:
                chapters.append(Chapter(ttl or f"Section {len(chapters) + 1}", paras, source))

    if len(chapters) < 2 and (level is None or not chapters):
        # no usable headings: one chapter for a short document, "Part N" sections for a long one
        from .pdf import split_parts
        paras = _speakable([t if lv == 0 else _heading_paragraph(t) for lv, t in blocks
                            if lv == 0 or t.lower() != (title or hint).lower()])
        if _words(paras) >= 2 * config.PDF_PART_WORDS:
            chapters = split_parts(paras)
            for c in chapters:
                c.source = source
        elif paras:
            chapters = [Chapter(display_title(title or hint) or "Text", paras, source)]
    if not chapters:
        raise ValueError(f"no readable text found in {source}")
    title, author = resolve_title_author(path, title, author, hint)
    if max_chapters:
        chapters = chapters[:max_chapters]
    return Book(title=title, author=author, chapters=chapters, cover=cover, cover_ext=cover_ext, dropped=dropped)


# ------------------------------------------------------------------ small helpers

_PUNCT_RUN = re.compile(r"[​­﻿]")


def clean_inline(s: str) -> str:
    return re.sub(r"\s+", " ", _PUNCT_RUN.sub("", s)).strip()


def lines_to_paragraphs(lines: list[str]) -> list[str]:
    """Plain text lines -> paragraphs. Blank lines separate paragraphs, hard-wrapped lines are rejoined (with line-break
    hyphens removed). Text with no blank lines at all falls back to the PDF line-rebuilding heuristic."""
    from .pdf import dehyphenate, rebuild_paragraphs
    nonblank = [l for l in lines if l.strip()]
    if not nonblank:
        return []
    if len(nonblank) > 12 and len(nonblank) / len(lines) > 0.97:
        indented = sum(1 for l in nonblank if l[:1] in " \t")
        if indented >= len(nonblank) * 0.04:       # "    First line indented" paragraphs, no blank lines
            paras, cur = [], []
            for l in lines:
                if l[:1] in " \t" and cur:
                    paras.append(cur)
                    cur = []
                cur.append(l.strip())
            if cur:
                paras.append(cur)
            return [re.sub(r"\s+", " ", dehyphenate("\n".join(p))).strip() for p in paras]
        return rebuild_paragraphs([(0, l) for l in lines])
    paras, cur = [], []
    for l in lines:
        if l.strip():
            cur.append(l.strip())
        elif cur:
            paras.append(cur)
            cur = []
    if cur:
        paras.append(cur)
    return [t for t in (re.sub(r"\s+", " ", dehyphenate("\n".join(p))).strip() for p in paras) if t]
