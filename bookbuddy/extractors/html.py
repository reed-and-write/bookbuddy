"""HTML / XHTML pages: main-content extraction, <h1>/<h2> chapters, <title> and <meta name="author">."""
from __future__ import annotations

import re
import warnings
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from .. import config
from ..book import Book
from . import common
from .common import Block, FormatInfo
from .epub import HEADINGS, html_to_blocks

extensions = (".html", ".htm", ".xhtml")

DROP_TAGS = ("script", "style", "noscript", "template", "iframe", "form", "button", "select", "svg", "canvas", "video",
             "audio", "object", "embed", "dialog", "head")
CHROME_TAGS = ("nav", "header", "footer", "aside", "menu")
_NOISE = re.compile(r"(^|[\s_-])(nav|navbar|navigation|menu|sidebar|side-bar|footer|masthead|cookie|cookies|consent|banner|"
                    r"advert|advertisement|ads?|share|sharing|social|comments?|related|breadcrumbs?|popup|modal|newsletter|"
                    r"subscribe|toc|skip-link|pagination|pager)($|[\s_-])", re.I)
_ROLE_NOISE = {"navigation", "banner", "contentinfo", "complementary", "search", "menu", "menubar", "dialog"}
CODE_NOTE = "Code block omitted."


def _meta(soup: BeautifulSoup, *names: str) -> str:
    wanted = {n.lower() for n in names}
    for m in soup.find_all("meta"):
        key = (m.get("name") or m.get("property") or "").lower()
        if key in wanted and m.get("content", "").strip():
            return common.clean_meta(m["content"])
    return ""


def _text_len(el: Tag) -> int:
    return len(el.get_text(" ", strip=True))


def _drop(el: Tag) -> None:
    if not getattr(el, "decomposed", False):
        el.decompose()


def main_content(soup: BeautifulSoup) -> Tag:
    """The element holding the page's real text: <main>/<article>/role=main (the biggest), else <body> without chrome."""
    for t in soup.find_all(DROP_TAGS):
        _drop(t)
    for c in soup.find_all(True):
        if isinstance(c, Tag) and not getattr(c, "decomposed", False) and c.attrs is not None \
                and (c.get("role") in _ROLE_NOISE or c.has_attr("hidden") or c.get("aria-hidden") == "true"):
            _drop(c)
    cands = soup.find_all(["main", "article"]) or soup.find_all(attrs={"role": "main"})
    root = max(cands, key=_text_len) if cands else (soup.body or soup)
    for t in root.find_all(CHROME_TAGS):
        if t is not root and not (t.name == "header" and t.find(["h1", "h2"]) and root.name in ("article", "main")):
            _drop(t)
    for t in root.find_all(True):
        if t is not root and not getattr(t, "decomposed", False) and t.attrs is not None:
            ident = " ".join(t.get("class", []) + [t.get("id", "")])
            if ident.strip() and _NOISE.search(ident) and _text_len(t) < 0.6 * max(_text_len(root), 1):
                _drop(t)
    return root


def html_blocks(html: bytes | str, code_blocks: str = "skip", tables: str = "skip") -> tuple[list[Block], dict]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        soup = BeautifulSoup(html, "lxml")
    title = common.clean_meta(soup.title.get_text()) if soup.title else ""
    meta = {"title": title,
            "og_title": _meta(soup, "og:title"),
            "author": _meta(soup, "author", "article:author", "dc.creator", "dcterms.creator", "citation_author")}
    if not meta["author"]:
        a = soup.find(["a", "span", "p"], attrs={"rel": "author"}) or soup.find(attrs={"itemprop": "author"})
        if a:
            meta["author"] = common.clean_meta(a.get_text())
    root = main_content(soup)
    for t in root.find_all("pre"):
        if code_blocks == "announce":
            p = soup.new_tag("p")
            p.string = CODE_NOTE
            t.replace_with(p)
        else:
            t.decompose()
    if tables == "skip":
        for t in root.find_all("table"):
            t.decompose()
    for t in root.find_all(["img", "figure"]):
        t.decompose()
    raw, _ = html_to_blocks(str(root))
    blocks = [(int(tag[1]) if tag in HEADINGS else 0, text) for tag, text in raw]
    return blocks, meta


def _page_title(meta: dict) -> str:
    t = meta.get("og_title") or meta.get("title") or ""
    parts = re.split(r"\s+[|–—]\s+|\s+-\s+|\s+::\s+", t)
    return parts[0].strip() if parts else t


def can_handle(path) -> bool:
    return Path(path).suffix.lower() in extensions


def extract(path, opts=None) -> Book:
    path = Path(path)
    st = config.get_settings()
    blocks, meta = html_blocks(path.read_bytes(), st.code_blocks, st.tables)
    return common.blocks_to_book(blocks, title=_page_title(meta), author=meta["author"], path=path, source="html",
                                 max_chapters=getattr(opts, "max_chapters", None))


def formats() -> list[FormatInfo]:
    return [FormatInfo("HTML", (".html", ".htm", ".xhtml"), "built in (BeautifulSoup main-content extraction)")]
