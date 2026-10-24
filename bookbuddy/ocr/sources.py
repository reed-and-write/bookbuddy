"""Page sources for OCR: a PDF, a DJVU, or a list of images (a folder, a zip, a CBZ or a CBR extracted to a scratch dir).

Every source answers `count` and `render(i, tmpdir) -> Path` (an image file Tesseract can read). Pages are rendered one at
a time inside the worker that OCRs them, so a 600-page book never sits on disk as 600 big images.
"""
from __future__ import annotations

import contextlib
import io
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from .common import OcrError, which

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".pgm", ".ppm", ".pbm", ".pnm", ".jp2")
NATIVE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".pgm", ".ppm", ".pbm", ".pnm")   # Tesseract reads these as is
RENDER_DPI = 300
MIN_WIDTH = 1000                 # smaller page images are upscaled before OCR
MAX_MEMBER_BYTES = 300 * 1024 * 1024
MAX_PAGES = 5000
RAR_MAGIC = (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")

NEEDS_UNRAR = ("Reading .cbr (RAR) comics needs a RAR extractor. Install unrar (or bsdtar / 7-zip), then try again. "
               "A .cbz (zip) file needs nothing extra.")


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def is_image_name(name: str) -> bool:
    n = Path(name)
    if n.name.startswith(("._", ".")) or "__MACOSX" in n.parts:
        return False
    return n.suffix.lower() in IMAGE_EXTS


def sort_images(paths: list[Path], base: Path | None = None) -> list[Path]:
    return sorted(paths, key=lambda p: natural_key(str(p.relative_to(base)) if base else str(p)))


def list_images(folder: Path) -> list[Path]:
    found = [p for p in Path(folder).rglob("*") if p.is_file() and not p.is_symlink() and is_image_name(str(p.relative_to(folder)))]
    return sort_images(found, Path(folder))


# ------------------------------------------------------------------ preparing an image for Tesseract

def prepare_image(src: Path, tmpdir: Path, tag: str = "p") -> Path:
    """Return a file Tesseract reads well: the original when it is a normal, big-enough image; otherwise a cleaned PNG
    (EXIF rotation applied, transparency flattened onto white, palette/CMYK -> grey/RGB, tiny scans upscaled)."""
    from PIL import Image, ImageOps
    try:
        with Image.open(src) as im:
            w, h = im.size
            orient = (im.getexif() or {}).get(274, 1)
            ok = (src.suffix.lower() in NATIVE_EXTS and w >= MIN_WIDTH and orient == 1
                  and im.mode in ("1", "L", "RGB") and getattr(im, "n_frames", 1) == 1)
            if ok:
                return src
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, "white")
                bg.paste(rgba, mask=rgba.split()[-1])
                im = bg
            elif im.mode not in ("1", "L", "RGB"):
                im = im.convert("RGB")
            if im.width < MIN_WIDTH:
                k = min(3.0, MIN_WIDTH / im.width * 1.5)
                im = im.convert("L" if im.mode == "1" else im.mode).resize((int(im.width * k), int(im.height * k)), Image.LANCZOS)
            out = tmpdir / f"{tag}-prepared.png"
            im.save(out, dpi=(RENDER_DPI, RENDER_DPI))
            return out
    except OcrError:
        raise
    except Exception as e:  # noqa: BLE001
        raise OcrError(f"cannot read image {src.name}: {e}") from e


# ------------------------------------------------------------------ sources

class PageSource:
    count = 0
    label = "pages"

    def render(self, i: int, tmpdir: Path) -> Path:  # pragma: no cover - interface
        raise NotImplementedError

    def cover_jpeg(self, max_side: int = 700) -> bytes | None:
        try:
            from PIL import Image
            with tempfile.TemporaryDirectory(prefix="bb-cover-") as td:
                p = self.render(0, Path(td))
                with Image.open(p) as im:
                    im = im.convert("RGB")
                    im.thumbnail((max_side, max_side))
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=82)
                    return buf.getvalue()
        except Exception:  # noqa: BLE001
            return None


class ImageFiles(PageSource):
    label = "images"

    def __init__(self, files: list[Path]):
        self.files = list(files)
        self.count = len(self.files)

    def render(self, i: int, tmpdir: Path) -> Path:
        return prepare_image(self.files[i], tmpdir, f"p{i}")


class PdfPages(PageSource):
    label = "pdf"

    def __init__(self, path: Path, count: int | None = None):
        self.path = Path(path)
        if count is None:
            from ..extractors.pdf import pdf_info
            count = int(pdf_info(self.path).get("Pages", "0") or 0)
        self.count = count

    def render(self, i: int, tmpdir: Path) -> Path:
        base = tmpdir / f"pdf{i}"
        # grey PGM, not PNG: poppler's PNG encoder took ~8 s per 300 dpi page, PGM takes 0.15 s (Tesseract reads it as is)
        r = subprocess.run(["pdftoppm", "-r", str(RENDER_DPI), "-gray", "-f", str(i + 1), "-l", str(i + 1),
                            "-singlefile", str(self.path), str(base)], capture_output=True, timeout=600)
        out = base.with_suffix(".pgm")
        if r.returncode != 0 or not out.exists():
            raise OcrError(f"could not render page {i + 1} (pdftoppm): {r.stderr.decode('utf-8', 'replace')[-200:]}")
        return out


class DjvuPages(PageSource):
    label = "djvu"

    def __init__(self, path: Path):
        self.path = Path(path)
        if which("ddjvu") is None or which("djvused") is None:
            raise OcrError("Reading .djvu files needs DjVuLibre (ddjvu, djvused). Install it "
                           "(Arch: djvulibre, Debian/Ubuntu: djvulibre-bin, macOS: brew install djvulibre), then try again.")
        r = subprocess.run(["djvused", "-e", "n", str(self.path)], capture_output=True, text=True, timeout=120)
        try:
            self.count = int(r.stdout.strip())
        except ValueError:
            raise OcrError(f"{self.path.name} does not look like a readable DjVu file") from None

    def render(self, i: int, tmpdir: Path) -> Path:
        out = tmpdir / f"djvu{i}.tif"
        r = subprocess.run(["ddjvu", "-format=tiff", f"-page={i + 1}", str(self.path), str(out)],
                           capture_output=True, timeout=600)
        if r.returncode != 0 or not out.exists():
            raise OcrError(f"could not render DjVu page {i + 1}: {r.stderr.decode('utf-8', 'replace')[-200:]}")
        return prepare_image(out, tmpdir, f"d{i}")


# ------------------------------------------------------------------ archives

def sniff_archive(path: Path) -> str:
    """'zip' | 'rar' | '' from the magic bytes (a .cbr that is really a zip is common)."""
    try:
        with Path(path).open("rb") as f:
            head = f.read(8)
    except OSError:
        return ""
    if head.startswith(ZIP_MAGIC):
        return "zip"
    if any(head.startswith(m) for m in RAR_MAGIC):
        return "rar"
    return ""


def extract_zip(path: Path, dest: Path) -> list[Path]:
    """Write every image member to dest/NNNNN.ext (member names are never used as paths: no zip-slip)."""
    names: list[tuple[str, zipfile.ZipInfo]] = []
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise OcrError(f"{Path(path).name} is not a readable zip/CBZ archive: {e}") from e
    with zf:
        for info in zf.infolist():
            if info.is_dir() or not is_image_name(info.filename):
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                raise OcrError(f"{Path(path).name}: image {info.filename!r} is unreasonably large")
            names.append((info.filename, info))
        names.sort(key=lambda t: natural_key(t[0].replace("\\", "/")))
        if len(names) > MAX_PAGES:
            raise OcrError(f"{Path(path).name}: more than {MAX_PAGES} pages")
        out: list[Path] = []
        for i, (name, info) in enumerate(names):
            target = dest / f"{i:05d}{Path(name).suffix.lower()}"
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            out.append(target)
    return out


def rar_tool() -> list[str] | None:
    """Command prefix that extracts a RAR: unrar, then 7-zip, then bsdtar (libarchive reads RAR)."""
    exe = which("unrar")
    if exe:
        return [exe, "x", "-idq", "-o+", "-y"]
    for seven in ("7zz", "7z", "7za"):
        exe = which(seven)
        if exe:
            return [exe, "x", "-y"]
    exe = which("bsdtar")
    return [exe, "-xf"] if exe else None


def extract_rar(path: Path, dest: Path) -> list[Path]:
    tool = rar_tool()
    if tool is None:
        raise OcrError(NEEDS_UNRAR)
    p = str(Path(path).resolve())
    kind = Path(tool[0]).name
    if kind.startswith("unrar"):
        cmd = tool + [p, str(dest) + "/"]
    elif kind.startswith("7z"):
        cmd = tool + [f"-o{dest}", p]
    else:
        cmd = tool + [p, "-C", str(dest)]
    r = subprocess.run(cmd, capture_output=True, timeout=1800)
    imgs = list_images(dest)
    if r.returncode != 0 and not imgs:
        raise OcrError(f"could not extract {Path(path).name} with {Path(tool[0]).name}: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    if not imgs:
        raise OcrError(f"{Path(path).name} contains no page images")
    return imgs


def extract_archive(path: Path, dest: Path) -> list[Path]:
    kind = sniff_archive(path)
    if kind == "zip":
        files = extract_zip(path, dest)
    elif kind == "rar":
        files = extract_rar(path, dest)
    else:
        suffix = Path(path).suffix.lower()
        if suffix in (".cbr", ".rar"):
            files = extract_rar(path, dest)
        else:
            files = extract_zip(path, dest)
    if not files:
        raise OcrError(f"{Path(path).name} contains no page images")
    return files


@contextlib.contextmanager
def open_source(path):
    """Context manager yielding a PageSource for a PDF, DJVU, image folder, zip/CBZ/CBR, or a list of image files."""
    if isinstance(path, (list, tuple)):
        yield ImageFiles([Path(p) for p in path])
        return
    path = Path(path)
    suffix = path.suffix.lower()
    if path.is_dir():
        files = list_images(path)
        if not files:
            raise OcrError(f"{path.name}: the folder has no page images ({', '.join(IMAGE_EXTS[:6])} ...)")
        yield ImageFiles(files)
    elif suffix == ".pdf":
        yield PdfPages(path)
    elif suffix in (".djvu", ".djv"):
        yield DjvuPages(path)
    elif suffix in (".zip", ".cbz", ".cbr", ".rar"):
        with tempfile.TemporaryDirectory(prefix="bookbuddy-pages-") as td:
            yield ImageFiles(extract_archive(path, Path(td)))
    elif suffix in IMAGE_EXTS:
        yield ImageFiles([path])
    else:
        raise OcrError(f"don't know how to OCR {path.name}")
