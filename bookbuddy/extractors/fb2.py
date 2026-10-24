"""FictionBook 2 (.fb2 and .fb2.zip): title-info, <section>/<title> chapters, cover from <binary>."""
from __future__ import annotations

import base64
import re
import zipfile
from pathlib import Path

from lxml import etree

from ..book import Book
from . import common
from .common import Block, FormatInfo

extensions = (".fb2", ".fb2.zip")

_XLINK = "http://www.w3.org/1999/xlink"
_PARSER = dict(resolve_entities=False, no_network=True, recover=True, huge_tree=True)


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def read_xml(path: Path) -> bytes:
    path = Path(path)
    if path.name.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(path) as z:
                names = [n for n in z.namelist() if n.lower().endswith(".fb2")]
                if not names:
                    raise ValueError("the zip file has no .fb2 inside")
                return z.read(names[0])
        except zipfile.BadZipFile as e:
            raise ValueError(f"could not open the zip file ({e})") from e
    return path.read_bytes()


def _text(el) -> str:
    """Inline text of a <p>, minus footnote references."""
    out: list[str] = []

    def walk(e) -> None:
        if e.text:
            out.append(e.text)
        for ch in e:
            name = _local(ch.tag)
            if name == "a" and ch.get("type") == "note":
                pass
            elif name == "image":
                pass
            else:
                walk(ch)
            if ch.tail:
                out.append(ch.tail)

    walk(el)
    return common.clean_inline("".join(out))


def _author(a) -> str:
    parts = [common.clean_meta(a.findtext(f"{{*}}{k}")) for k in ("first-name", "middle-name", "last-name")]
    name = " ".join(p for p in parts if p)
    return name or common.clean_meta(a.findtext("{*}nickname"))


def fb2_blocks(root) -> tuple[list[Block], dict, tuple[bytes, str] | None]:
    ti = root.find(".//{*}description/{*}title-info")
    meta = {"title": "", "author": ""}
    cover = None
    if ti is not None:
        meta["title"] = common.clean_meta(ti.findtext("{*}book-title"))
        meta["author"] = ", ".join(n for n in (_author(a) for a in ti.findall("{*}author")) if n)
        img = ti.find("{*}coverpage/{*}image")
        if img is not None:
            href = (img.get(f"{{{_XLINK}}}href") or img.get("href") or "").lstrip("#")
            for b in root.findall("{*}binary"):
                if b.get("id") == href and b.text:
                    ctype = (b.get("content-type") or "").lower()
                    ext = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/gif": "gif"}.get(ctype)
                    try:
                        data = base64.b64decode(b.text, validate=False)
                    except Exception:  # noqa: BLE001
                        data = b""
                    if data and ext:
                        cover = (data, ext)
    blocks: list[Block] = []

    def section(sec) -> None:
        for el in sec:
            name = _local(el.tag)
            if name == "title":
                t = common.clean_inline(" ".join(_text(p) for p in el.findall("{*}p")))
                if t:
                    blocks.append((1, t))
            elif name == "section":
                section(el)
            elif name in ("p", "subtitle"):
                t = _text(el)
                if t:
                    blocks.append((0, t))
            elif name in ("epigraph", "cite", "poem", "stanza"):
                section(el)
            elif name == "v":
                t = _text(el)
                if t:
                    blocks.append((0, t if re.search(r"[.!?,;:]$", t) else t + ","))

    for body in root.findall("{*}body"):
        if (body.get("name") or "").lower() in ("notes", "comments"):
            continue
        section(body)
    return blocks, meta, cover


def can_handle(path) -> bool:
    return Path(path).name.lower().endswith((".fb2", ".fb2.zip"))


def extract(path, opts=None) -> Book:
    path = Path(path)
    raw = read_xml(path)
    root = etree.fromstring(raw, etree.XMLParser(**_PARSER))
    if root is None or _local(root.tag) != "FictionBook":
        raise ValueError("this does not look like an FB2 (FictionBook) file")
    blocks, meta, cover = fb2_blocks(root)
    # every section title is emitted at level 1, so sections are chapters however deeply they nest
    return common.blocks_to_book(blocks, title=meta["title"], author=meta["author"], path=common.stem_of(path), source="fb2",
                                 cover=cover[0] if cover else None, cover_ext=cover[1] if cover else "jpg",
                                 max_chapters=getattr(opts, "max_chapters", None))


def formats() -> list[FormatInfo]:
    return [FormatInfo("FictionBook 2", (".fb2", ".fb2.zip"), "built in (lxml)")]
