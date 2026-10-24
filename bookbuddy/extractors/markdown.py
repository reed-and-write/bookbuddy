"""Markdown (.md, .markdown): headings define chapters; markup is stripped to speakable text."""
from __future__ import annotations

import html
import re
from pathlib import Path

from .. import config
from ..book import Book
from . import common, txt
from .common import Block, FormatInfo

extensions = (".md", ".markdown")

CODE_NOTE = "Code block omitted."

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_ATX = re.compile(r"^\s{0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?\s*$")
_ATX_EMPTY = re.compile(r"^\s{0,3}#{1,6}\s*$")
_SETEXT = re.compile(r"^\s{0,3}(=+|-+)\s*$")
_HR = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_LIST = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s+\S+.*$")
_FOOT_DEF = re.compile(r"^\s{0,3}\[\^[^\]]+\]:")


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Leading YAML block -> ({key: value}, rest). A deliberately small subset: scalars, [a, b] and '- item' lists."""
    m = re.match(r"﻿?---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|$)", text, re.S)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    key = None
    for line in m.group(1).split("\n"):
        kv = re.match(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$", line)
        if kv:
            key = kv.group(1).lower()
            v = kv.group(2).strip()
            if v.startswith("[") and v.endswith("]"):
                v = ", ".join(x.strip().strip("'\"") for x in v[1:-1].split(","))
            meta[key] = v.strip("'\"")
        elif key and re.match(r"^\s+-\s+(.*)$", line):
            item = re.match(r"^\s+-\s+(.*)$", line).group(1).strip().strip("'\"")
            meta[key] = f"{meta[key]}, {item}" if meta.get(key) else item
    return meta, text[m.end():]


def strip_inline(s: str) -> str:
    """Inline Markdown -> plain text: links keep their text, images and footnote marks go."""
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)|!\[[^\]]*\]\[[^\]]*\]", "", s)             # images
    s = re.sub(r"\[\^[^\]]+\]", "", s)                                            # footnote refs
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)                                # [text](url)
    s = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", s)                               # [text][ref]
    s = re.sub(r"<(https?://|mailto:)[^>]*>", "", s)                              # <autolinks>
    s = re.sub(r"</?[A-Za-z][^>]*>", "", s)                                       # inline HTML
    s = re.sub(r"(?<![\w])https?://\S+", "", s)                                   # bare URLs are not speakable
    s = re.sub(r"`+([^`]*)`+", r"\1", s)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", s)
    s = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"\1", s)
    s = re.sub(r"~~(.+?)~~", r"\1", s)
    s = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~])", r"\1", s)
    s = html.unescape(s)
    return common.clean_inline(s)


def parse_blocks(text: str, code_blocks: str = "skip", tables: str = "skip") -> list[Block]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[Block] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            t = strip_inline(" ".join(para))
            if t:
                blocks.append((0, t))
            para.clear()

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:                                   # fenced code block
            flush()
            mark = fence.group(1)
            i += 1
            while i < n and not re.match(rf"^\s{{0,3}}{re.escape(mark[0])}{{{len(mark)},}}\s*$", lines[i]):
                i += 1
            i += 1
            if code_blocks == "announce":
                blocks.append((0, CODE_NOTE))
            continue
        if not line.strip():
            flush()
            i += 1
            continue
        m = _ATX.match(line)
        if m and not _ATX_EMPTY.match(line):
            flush()
            t = strip_inline(m.group(2))
            if t:
                blocks.append((len(m.group(1)), t))
            i += 1
            continue
        if _HR.match(line) and not para:
            i += 1
            continue
        if _SETEXT.match(line) and len(para) == 1:
            level = 1 if line.strip().startswith("=") else 2
            t = strip_inline(para[0])
            para.clear()
            if t:
                blocks.append((level, t))
            i += 1
            continue
        if _HR.match(line):
            flush()
            i += 1
            continue
        if _FOOT_DEF.match(line) or _REF_DEF.match(line):
            flush()
            i += 1
            while i < n and lines[i].startswith(("    ", "\t")):     # footnote continuation
                i += 1
            continue
        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
            flush()
            rows = [line]
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            if tables == "read":
                for r in rows:
                    cells = [strip_inline(c) for c in r.strip().strip("|").split("|")]
                    t = ", ".join(c for c in cells if c)
                    if t:
                        blocks.append((0, t + "."))
            continue
        if line.startswith(("    ", "\t")) and not para and not _LIST.match(line):
            # indented code block
            while i < n and (lines[i].startswith(("    ", "\t")) or not lines[i].strip()):
                i += 1
            if code_blocks == "announce":
                blocks.append((0, CODE_NOTE))
            continue
        q = re.match(r"^\s{0,3}>\s?(.*)$", line)
        if q:                                        # blockquote: read its text as ordinary lines
            if q.group(1).strip():
                para.append(q.group(1))
            else:
                flush()
            i += 1
            continue
        lm = _LIST.match(line)
        if lm:
            flush()
            item = [re.sub(r"^\[[ xX]\]\s*", "", lm.group(1))]
            i += 1
            while i < n and lines[i].strip() and lines[i].startswith((" ", "\t")) and not _LIST.match(lines[i]):
                item.append(lines[i].strip())
                i += 1
            t = strip_inline(" ".join(item))
            if t:
                blocks.append((0, t if re.search(r"[.!?:;]$", t) else t + "."))
            continue
        para.append(line.strip())
        i += 1
    flush()
    return blocks


def can_handle(path) -> bool:
    return Path(path).suffix.lower() in extensions


def extract(path, opts=None) -> Book:
    path = Path(path)
    st = config.get_settings()
    meta, body = parse_front_matter(txt.read_text(path))
    blocks = parse_blocks(body, st.code_blocks, st.tables)
    return common.blocks_to_book(blocks, title=meta.get("title", ""), author=meta.get("author") or meta.get("authors", ""),
                                 path=path, source="markdown", max_chapters=getattr(opts, "max_chapters", None))


def formats() -> list[FormatInfo]:
    return [FormatInfo("Markdown", (".md", ".markdown"), "built in (headings become chapters, YAML front matter)")]
