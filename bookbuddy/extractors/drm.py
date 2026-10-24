"""DRM detection. Book Buddy never removes DRM: it recognises protected files early and says so plainly.

    EPUB     META-INF/encryption.xml that encrypts content documents (font obfuscation alone is fine),
             META-INF/rights.xml (Adobe ADEPT) or META-INF/sinf.xml (Apple FairPlay)
    PDF      needs a password to open, or poppler refuses text extraction (Adobe DRM / copy-protected)
    Kindle   MOBI/AZW/AZW3 header says the text is encrypted, Topaz books, KFX with DRMION content
"""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path

from lxml import etree

DOCS_URL = "https://github.com/reed-and-write/bookbuddy/blob/main/docs/drm.md"
MESSAGE = f"This book is DRM-protected. Book Buddy can't read DRM-protected files. ({DOCS_URL})"

# Algorithms that only obfuscate embedded fonts (IDPF and Adobe font mangling): not DRM.
FONT_OBFUSCATION = {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}
FONT_EXTS = (".otf", ".ttf", ".ttc", ".woff", ".woff2", ".eot")
_ENC_NS = {"enc": "http://www.w3.org/2001/04/xmlenc#", "ocf": "urn:oasis:names:tc:opendocument:xmlns:container"}


class DRMError(ValueError):
    """The file is DRM-protected. The message is the user-facing text."""

    def __init__(self, detail: str = ""):
        super().__init__(MESSAGE)
        self.detail = detail


def epub_encrypted_resources(z: zipfile.ZipFile) -> list[str]:
    """Resources that are really encrypted (not just font-obfuscated) according to META-INF/encryption.xml."""
    names = {n.lower(): n for n in z.namelist()}
    if "meta-inf/encryption.xml" not in names:
        return []
    try:
        root = etree.fromstring(z.read(names["meta-inf/encryption.xml"]), etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError:
        return ["META-INF/encryption.xml (unreadable)"]
    bad: list[str] = []
    for ed in root.iter("{%s}EncryptedData" % _ENC_NS["enc"]):
        method = ed.find("enc:EncryptionMethod", _ENC_NS)
        algo = method.get("Algorithm", "") if method is not None else ""
        ref = ed.find(".//enc:CipherReference", _ENC_NS)
        uri = (ref.get("URI", "") if ref is not None else "").lower()
        if algo in FONT_OBFUSCATION or uri.endswith(FONT_EXTS):
            continue
        bad.append(uri or algo or "unknown resource")
    return bad


def check_epub(z: zipfile.ZipFile) -> None:
    lower = {n.lower() for n in z.namelist()}
    if "meta-inf/rights.xml" in lower:
        raise DRMError("META-INF/rights.xml (Adobe ADEPT)")
    if "meta-inf/sinf.xml" in lower:
        raise DRMError("META-INF/sinf.xml (Apple FairPlay)")
    bad = epub_encrypted_resources(z)
    if bad:
        raise DRMError(f"encrypted content: {', '.join(bad[:3])}")


def check_epub_path(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as z:
            check_epub(z)
    except zipfile.BadZipFile:
        return                      # not our problem here: the EPUB reader reports a broken file


def check_pdf_error(stderr: str) -> None:
    """Raise DRMError when a poppler tool's error output says the PDF is password- or DRM-protected."""
    s = stderr.lower()
    if any(k in s for k in ("incorrect password", "unknown security handler", "pubsec", "copying of text", "not allowed")):
        raise DRMError("PDF needs a password or forbids text extraction")


def check_kindle(path: Path) -> None:
    """Cheap markers: MOBI/AZW encryption field, Topaz magic, KFX DRMION."""
    path = Path(path)
    with path.open("rb") as f:
        head = f.read(1024 * 1024)
    if head[:3] == b"TPZ":
        raise DRMError("Topaz (always DRM-protected)")
    if b"\xeaDRMION\xee" in head or head[:4] == b"\xeaDRM":
        raise DRMError("Kindle KFX DRMION container")
    if head[60:68] in (b"BOOKMOBI", b"TEXtREAd") and len(head) > 100:
        nrec = struct.unpack(">H", head[76:78])[0]
        if nrec:
            rec0 = struct.unpack(">I", head[78:82])[0]
            if rec0 + 14 <= len(head):
                enc = struct.unpack(">H", head[rec0 + 12:rec0 + 14])[0]
                if enc != 0:
                    raise DRMError(f"MOBI header encryption type {enc}")


def looks_like_drm_failure(output: str) -> bool:
    """Calibre's ebook-convert failure text for protected books."""
    s = output.lower()
    return "drm" in s or "encrypted" in s and "calibre" in s
