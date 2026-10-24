"""Format extractors. Each is a module (or object) with:

    extensions                 tuple of lower-case suffixes it reads, e.g. (".epub",)   [optional, used by the watcher]
    can_handle(path) -> bool   cheap check (suffix, magic bytes)
    extract(path, opts) -> Book

Built-ins are registered below; third-party ones come in through the `bookbuddy.extractors` entry-point group.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..book import Book
from ..registry import extractors as registry
from . import calibre, cbr, cbz, djvu, docx, epub, fb2, html, images, markdown, odt, pdf, rtf, txt
from .common import FormatInfo
from .drm import DRMError

log = logging.getLogger("bookbuddy.extractors")

_BUILTIN_ORDER = (("epub", epub), ("pdf", pdf), ("txt", txt), ("markdown", markdown), ("html", html), ("docx", docx),
                  ("odt", odt), ("rtf", rtf), ("fb2", fb2), ("calibre", calibre),
                  ("images", images), ("cbz", cbz), ("cbr", cbr), ("djvu", djvu))
for _name, _mod in _BUILTIN_ORDER:
    registry.register(_name, _mod, builtin=True)


class UnsupportedFormat(ValueError):
    pass


def find_extractor(path: Path):
    """The first extractor that claims `path` (third-party plugins are asked before built-ins), or None."""
    for name, ex in registry.items():
        try:
            if ex.can_handle(path):
                return ex
        except Exception as e:  # noqa: BLE001 - one bad plugin must not hide the others
            log.warning("extractor %r can_handle failed on %s: %s", name, path, e)
    return None


def extract_book(path: Path, opts=None) -> Book:
    path = Path(path)
    ex = find_extractor(path)
    if ex is None:
        raise UnsupportedFormat(f"unsupported file type {path.suffix.lower()!r} "
                                f"(supported: {', '.join(sorted(supported_extensions())) or 'none'})")
    return ex.extract(path, opts)


def supported_extensions() -> set[str]:
    out: set[str] = set()
    for _, ex in registry.items():
        out.update(getattr(ex, "extensions", ()))
    return out


def is_supported_name(name: str) -> bool:
    """True if a file called `name` has an extension some extractor reads (compound ones like .fb2.zip included)."""
    n = name.lower()
    return any(n.endswith(ext) for ext in supported_extensions())


def list_formats() -> list[FormatInfo]:
    """Every supported format and whether it works right now. The CLI `formats` command and the web page use this."""
    out: list[FormatInfo] = []
    order = [n for n, _ in _BUILTIN_ORDER]
    for name, ex in sorted(registry.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in order else len(order), kv[0])):
        try:
            fn = getattr(ex, "formats", None)
            rows = fn() if callable(fn) else [FormatInfo(name, tuple(getattr(ex, "extensions", ())), f"plugin {name!r}")]
        except Exception as e:  # noqa: BLE001
            log.warning("extractor %r formats() failed: %s", name, e)
            rows = []
        out.extend(rows)
    return out
