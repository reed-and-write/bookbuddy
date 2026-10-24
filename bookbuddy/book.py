"""The Book model every extractor returns, with stable text anchors for future annotations.

Anchors: a chapter is its paragraphs joined by a blank line. A paragraph is addressed by
(chapter index, paragraph index) -- id "c3.p12" -- and carries its character offsets inside the chapter text.
Synthesis chunks point back at their paragraph (and offsets inside it), so a highlight, note or read-along
position can be mapped between the text and the audio later. Indices are positions in the extracted book (after
`max_chapters`), so they are only stable for the same source file and the same extractor version.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1
PARA_SEP = "\n\n"


@dataclass(frozen=True)
class ParagraphAnchor:
    chapter: int
    index: int
    start: int          # character offset of the paragraph inside the chapter text
    end: int

    @property
    def id(self) -> str:
        return f"c{self.chapter}.p{self.index}"


@dataclass
class Chapter:
    title: str
    paragraphs: list[str]           # normalised, speakable
    source: str = ""

    @property
    def chars(self) -> int:
        return sum(len(p) for p in self.paragraphs)

    @property
    def words(self) -> int:
        return sum(len(p.split()) for p in self.paragraphs)

    @property
    def text(self) -> str:
        return PARA_SEP.join(self.paragraphs)

    def anchors(self, chapter_index: int) -> list[ParagraphAnchor]:
        out, pos = [], 0
        for i, p in enumerate(self.paragraphs):
            out.append(ParagraphAnchor(chapter_index, i, pos, pos + len(p)))
            pos += len(p) + len(PARA_SEP)
        return out


@dataclass
class Book:
    title: str
    author: str
    chapters: list[Chapter]
    cover: bytes | None = None
    cover_ext: str = "jpg"
    dropped: list[tuple[str, str]] = field(default_factory=list)   # (title, reason)
    llm_provider: str = "none"     # which LLM cleanup provider touched the text (PDF only)
    artifacts: dict[str, Path] = field(default_factory=dict)   # files made along the way, e.g. "searchable_pdf" (not in book.json)

    @property
    def words(self) -> int:
        return sum(c.words for c in self.chapters)

    def anchors(self) -> list[ParagraphAnchor]:
        return [a for ci, ch in enumerate(self.chapters) for a in ch.anchors(ci)]

    def paragraph(self, chapter: int, index: int) -> str:
        return self.chapters[chapter].paragraphs[index]

    # -- JSON persistence (the cover image is written next to the JSON, not inside it)
    def to_dict(self, source: str = "") -> dict:
        return {
            "schema": SCHEMA_VERSION, "source": source, "title": self.title, "author": self.author,
            "llm_provider": self.llm_provider, "dropped": [list(d) for d in self.dropped],
            "cover": f"cover_src.{self.cover_ext}" if self.cover else None,
            "chapters": [
                {"index": ci, "title": ch.title, "source": ch.source,
                 "paragraphs": [{"id": a.id, "index": a.index, "start": a.start, "end": a.end, "text": ch.paragraphs[a.index]}
                                for a in ch.anchors(ci)]}
                for ci, ch in enumerate(self.chapters)],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Book":
        if d.get("schema") != SCHEMA_VERSION:
            raise ValueError(f"unsupported book.json schema {d.get('schema')!r}")
        chapters = [Chapter(c["title"], [p["text"] for p in c["paragraphs"]], c.get("source", "")) for c in d["chapters"]]
        return cls(title=d["title"], author=d["author"], chapters=chapters, dropped=[tuple(x) for x in d.get("dropped", [])],
                   llm_provider=d.get("llm_provider", "none"))

    def save(self, workdir: Path, source: str = "", extra: dict | None = None) -> Path:
        """Write book.json (and the cover image) into `workdir`, atomically."""
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        if self.cover:
            (workdir / f"cover_src.{self.cover_ext}").write_bytes(self.cover)
        data = self.to_dict(source)
        data.update(extra or {})
        out = workdir / "book.json"
        tmp = out.with_name("book.json.tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, out)
        return out

    @classmethod
    def load(cls, workdir: Path) -> "Book":
        workdir = Path(workdir)
        d = json.loads((workdir / "book.json").read_text(encoding="utf-8"))
        book = cls.from_dict(d)
        if d.get("cover") and (workdir / d["cover"]).exists():
            book.cover = (workdir / d["cover"]).read_bytes()
            book.cover_ext = d["cover"].rsplit(".", 1)[-1]
        return book
