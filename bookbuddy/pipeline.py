"""Book -> audio: plan chunks, synthesize with per-chunk checkpoints, QC, assemble M4B / MP3."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf

from . import config
from .book import Book
from .extractors import UnsupportedFormat, extract_book
from .textnorm import Chunk, chunk_chapter, spoken_title

log = logging.getLogger("bookbuddy.pipeline")


class PipelineError(RuntimeError):
    pass


class Cancelled(Exception):
    """The job was cancelled (checkpoints are kept, so a retry resumes)."""


@dataclass
class Options:
    voice: str = config.DEFAULT_VOICE
    speed: float = config.DEFAULT_SPEED
    fmt: str = "m4b"                      # m4b | mp3
    out_dir: Path | None = None            # None = settings.output_dir
    max_chapters: int | None = None
    keep_work: bool = False
    work_root: Path | None = None
    device: str | None = None
    engine_factory: Callable | None = None  # tests inject a fake engine
    stop_after_chunks: int | None = None    # tests: simulate a crash after N new chunks
    llm: str | None = None                  # PDF cleanup provider: None = settings, auto | none | <provider name>
    on_event: Callable | None = None        # (kind, **info) progress hook: 'stage', 'extracted', 'planned', 'chunk_progress',
                                            # 'chapter_start', 'chapter_ready', 'progress', 'llm_*', 'ocr_*'
    ocr_lang: str | None = None             # None = settings ([ocr] lang)
    ocr_backend: str | None = None          # None = settings: auto | ocrmypdf | tesseract
    ocr_workers: int | None = None          # None = settings (0 = auto)
    chapter_dir: Path | None = None         # where per-chapter .m4a files go (None = <work dir>/chapters)
    should_cancel: Callable[[], bool] | None = None   # polled between chunks; True stops the job with Cancelled
    pump: Callable[[], None] | None = None  # called between chunks (the worker runs queued GPU side-tasks there)


@dataclass
class Result:
    path: Path
    title: str
    author: str
    duration: float
    size: int
    chapters: list[tuple[str, float, float]]
    qc_warnings: list[str] = field(default_factory=list)
    chunks_total: int = 0
    chunks_resumed: int = 0
    llm: str = "none"                       # LLM cleanup provider used (PDF only)


# ------------------------------------------------------------------ planning

def book_hash(path: Path, voice: str, speed: float) -> str:
    h = hashlib.sha256()
    path = Path(path)
    if path.is_dir():                      # an image folder: every file's relative name and content
        for f in sorted(p for p in path.rglob("*") if p.is_file()):
            h.update(str(f.relative_to(path)).encode())
            with open(f, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
    else:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
    h.update(f"|{voice}|{speed}|v{config.PIPELINE_VERSION}".encode())
    return h.hexdigest()[:16]


def plan_chunks(book: Book) -> list[list[Chunk]]:
    return [chunk_chapter(i, spoken_title(ch.title), ch.paragraphs) for i, ch in enumerate(book.chapters)]


def chunk_path(workdir: Path, c: Chunk) -> Path:
    th = hashlib.sha1(c.text.encode()).hexdigest()[:8]
    return workdir / "chunks" / f"c{c.chapter:03d}_{c.index:05d}_{th}.wav"


# ------------------------------------------------------------------ QC

RMS_MIN = 0.003          # below this a chunk is effectively silent
SECONDS_PER_CHAR = (0.02, 0.35)   # plausible range, very wide on purpose


def chunk_stats(audio: np.ndarray, text: str, sr: int = config.SAMPLE_RATE) -> dict:
    dur = len(audio) / sr
    rms = float(np.sqrt(np.mean(np.square(audio)))) if len(audio) else 0.0
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    flags = []
    if dur < 0.05 or rms < RMS_MIN:
        flags.append("silent")
    if peak >= 0.999:
        flags.append("clipped")
    chars = max(1, len(text))
    lo, hi = SECONDS_PER_CHAR
    if chars >= 8 and (dur < chars * lo * 0.5 or dur > chars * hi + 3):
        flags.append("duration")
    return {"dur": round(dur, 3), "rms": round(rms, 5), "peak": round(peak, 4), "chars": chars, "flags": flags}


def _write_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pcm = np.clip(audio, -1.0, 1.0)
    sf.write(str(tmp), pcm, config.SAMPLE_RATE, subtype="PCM_16", format="WAV")
    os.replace(tmp, path)


class StopForTest(Exception):
    pass


# ------------------------------------------------------------------ synthesis with checkpoints

def synthesize(plan: list[list[Chunk]], workdir: Path, engine, stop_after: int | None = None,
               progress: Callable[[int, int], None] | None = None, on_chunk: Callable | None = None,
               should_cancel: Callable[[], bool] | None = None, pump: Callable[[], None] | None = None) -> tuple[int, int]:
    """Synthesize every missing chunk. Returns (done_before, newly_synthesized).

    on_chunk(chunk, n_done, total) fires after each NEW chunk is written (so a chapter can be finished the moment its
    last chunk lands); should_cancel is polled before each chunk; pump runs queued side-tasks between chunks."""
    all_chunks = [c for ch in plan for c in ch]
    total = len(all_chunks)
    resumed = sum(1 for c in all_chunks if chunk_path(workdir, c).exists())
    log.info("synthesis: %d chunks, %d already done", total, resumed)
    new = 0
    t0 = time.time()
    for n, c in enumerate(all_chunks, 1):
        p = chunk_path(workdir, c)
        if p.exists():
            continue
        if should_cancel and should_cancel():
            raise Cancelled("cancelled")
        if pump:
            pump()
        audio = engine.synth(c.text)
        st = chunk_stats(audio, c.text)
        if "silent" in st["flags"]:
            log.warning("chunk %s looks silent, retrying once", p.name)
            audio = engine.synth(c.text)
        _write_wav(p, audio)
        new += 1
        if on_chunk:
            on_chunk(c, n, total)
        if progress and (new % 25 == 0 or n == total):
            progress(n, total)
        if new % 100 == 0:
            rate = new / (time.time() - t0)
            log.info("synthesized %d/%d chunks (%.1f chunks/s, ETA %.0f min)", resumed + new, total, rate,
                     (total - resumed - new) / rate / 60)
        if stop_after is not None and new >= stop_after:
            raise StopForTest(f"stopped after {new} new chunks")
    return resumed, new


# ------------------------------------------------------------------ assembly

def _ffmeta_escape(s: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", s)


def assemble_chapter(ci: int, chunks: list[Chunk], workdir: Path) -> tuple[Path, float, list[str]]:
    """Write one chapter WAV (chunks + pauses). Returns (path, seconds, qc warnings)."""
    sr = config.SAMPLE_RATE
    out = workdir / f"chapter_{ci:03d}.wav"
    tmp = out.with_name(out.name + ".part")
    warnings: list[str] = []
    total = 0
    with sf.SoundFile(str(tmp), "w", samplerate=sr, channels=1, subtype="PCM_16", format="WAV") as w:
        for c in chunks:
            p = chunk_path(workdir, c)
            if not p.exists():
                raise PipelineError(f"missing chunk {p.name}; refusing to assemble")
            audio, _ = sf.read(str(p), dtype="float32")
            st = chunk_stats(audio, c.text)
            if st["flags"]:
                warnings.append(f"chapter {ci + 1} chunk {c.index} {st['flags']} rms={st['rms']} dur={st['dur']}s: {c.text[:50]!r}")
            w.write(audio)
            gap = np.zeros(int(c.pause * sr), dtype=np.float32)
            w.write(gap)
            total += len(audio) + len(gap)
    os.replace(tmp, out)
    return out, total / sr, warnings


def assemble_chapters(plan: list[list[Chunk]], workdir: Path) -> tuple[list[Path], list[tuple[float, float]], list[str]]:
    """Write one WAV per chapter (chunks + pauses). Returns (paths, [(start, end)], qc warnings)."""
    paths, spans, warnings = [], [], []
    t = 0.0
    for ci, chunks in enumerate(plan):
        out, dur, w = assemble_chapter(ci, chunks, workdir)
        paths.append(out)
        spans.append((t, t + dur))
        warnings += w
        t += dur
    return paths, spans, warnings


def encode_chapter(wav: Path, dest: Path, title: str = "", artist: str = "", album: str = "", track: str = "") -> None:
    """AAC in .m4a at the book bitrate (the same encoder settings the M4B always used)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav), "-vn", "-c:a", "aac", "-b:a",
           config.AAC_BITRATE, "-ac", "1"]
    for k, v in (("title", title), ("artist", artist), ("album", album), ("track", track)):
        if v:
            cmd += ["-metadata", f"{k}={v}"]
    cmd += ["-movflags", "+faststart", "-f", "ipod", str(tmp)]
    _run(cmd)
    os.replace(tmp, dest)


def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError as e:
        raise PipelineError(f"ffprobe could not read {path.name}: {r.stderr[-300:]}") from e


def safe_name(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "", s).strip(" .")
    return re.sub(r"\s+", " ", s)[:120] or "audiobook"


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise PipelineError(f"{cmd[0]} failed ({r.returncode}): {r.stderr[-1500:]}")


def _prepare_cover(book: Book, workdir: Path) -> Path | None:
    """Write the cover as JPEG (the one format every player/muxer agrees on)."""
    if not book.cover:
        return None
    raw = workdir / f"cover_src.{book.cover_ext}"
    raw.write_bytes(book.cover)
    if book.cover_ext == "jpg":
        return raw
    out = workdir / "cover.jpg"
    try:
        _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw), "-q:v", "2", str(out)])
        return out
    except PipelineError as e:
        log.warning("could not convert cover art: %s", e)
        return None


def _concat_line(p: Path) -> str:
    return "file '" + str(p).replace("'", "'\\''") + "'\n"


def build_m4b(book: Book, chapter_audio: list[Path], spans: list[tuple[float, float]], workdir: Path, out: Path,
              copy: bool | None = None) -> None:
    """Chapter audio -> one chaptered M4B. .m4a inputs (the progressive per-chapter files) are joined with a stream
    copy, so nothing is re-encoded or re-synthesized; WAV inputs are encoded."""
    if copy is None:
        copy = all(p.suffix.lower() == ".m4a" for p in chapter_audio)
    lst = workdir / "concat.txt"
    lst.write_text("".join(_concat_line(p) for p in chapter_audio))
    meta = [";FFMETADATA1", f"title={_ffmeta_escape(book.title)}", f"artist={_ffmeta_escape(book.author)}",
            f"album={_ffmeta_escape(book.title)}", f"album_artist={_ffmeta_escape(book.author)}", "genre=Audiobook"]
    for ch, (s, e) in zip(book.chapters, spans):
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(s * 1000)}", f"END={int(e * 1000)}",
                 f"title={_ffmeta_escape(ch.title)}"]
    metaf = workdir / "ffmetadata.txt"
    metaf.write_text("\n".join(meta) + "\n", encoding="utf-8")

    cover = _prepare_cover(book, workdir)

    def cmd(with_cover: bool) -> list[str]:
        c = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-i", str(metaf)]
        if with_cover:
            c += ["-i", str(cover)]
        c += ["-map", "0:a"]
        if with_cover:
            c += ["-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
        c += ["-map_metadata", "1", "-map_chapters", "1"]
        c += ["-c:a", "copy"] if copy else ["-c:a", "aac", "-b:a", config.AAC_BITRATE, "-ac", "1"]
        c += ["-movflags", "+faststart", "-f", "ipod", str(out)]
        return c

    try:
        _run(cmd(cover is not None))
    except PipelineError:
        if cover is None:
            raise
        log.warning("ffmpeg failed with cover art; retrying without it")
        _run(cmd(False))


def build_mp3s(book: Book, chapter_wavs: list[Path], workdir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cover = _prepare_cover(book, workdir)
    n = len(chapter_wavs)
    for i, (ch, wav) in enumerate(zip(book.chapters, chapter_wavs), 1):
        dest = out_dir / f"{i:02d} - {safe_name(ch.title)}.mp3"
        c = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav)]
        if cover:
            c += ["-i", str(cover)]
        c += ["-map", "0:a"]
        if cover:
            c += ["-map", "1:v", "-c:v", "copy", "-id3v2_version", "3",
                  "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
        c += ["-c:a", "libmp3lame", "-b:a", config.AAC_BITRATE, "-ac", "1",
              "-metadata", f"title={ch.title}", "-metadata", f"artist={book.author}", "-metadata", f"album={book.title}",
              "-metadata", f"track={i}/{n}", "-metadata", "genre=Audiobook", str(dest) + ".part.mp3"]
        try:
            _run(c)
        except PipelineError:
            if not cover:
                raise
            log.warning("mp3 with cover failed; retrying without it")
            nc = [x for x in c]
            i_cover = nc.index(str(cover))
            del nc[i_cover - 1:i_cover + 1]                       # drop "-i cover"
            j = nc.index("-map", nc.index("-map") + 1)            # drop "-map 1:v -c:v copy ... comment=..."
            k = nc.index("-c:a")
            del nc[j:k]
            _run(nc)
        os.replace(str(dest) + ".part.mp3", dest)


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size


# ------------------------------------------------------------------ top level

def load_book(src: Path, opts: Options) -> Book:
    """Extractor dispatch through the plugin registry. Every extractor yields a Book (title, author, chapters)."""
    try:
        return extract_book(src, opts)
    except UnsupportedFormat as e:
        raise PipelineError(str(e)) from e


# Rough speed on the RTX 5060 (measured ~10-12 chunks/s) plus ffmpeg assembly; only used for the first estimate.
EST_CHUNKS_PER_SEC = 10.0
EST_ASSEMBLY_SEC_PER_CHUNK = 0.065  # ffmpeg AAC encode: measured 90 s / 1393 chunks, 220 s / 3255 chunks
PROGRESS_MILESTONES = (0.5,)
MIN_ESTIMATE_FOR_PROGRESS = 300.0  # seconds; shorter books only get the ready message


def _emit(opts: Options, kind: str, **info) -> None:
    if opts.on_event:
        try:
            opts.on_event(kind, **info)
        except Exception:  # noqa: BLE001 - notifications must never fail a job
            log.exception("progress hook failed")


ENCODE_WORKERS = 2          # background chapter encoders; the GPU thread never waits for ffmpeg


class _ChapterBuilder:
    """Finishes each chapter (WAV -> AAC .m4a in the library) the moment its last chunk exists.

    The assembly + ffmpeg encode runs on a small background pool so the thread that owns the GPU keeps synthesizing.
    A chapter is submitted at most once (`submitted`), is announced (`chapter_ready`) only after its file is fully
    written, and every callback/event fires on the caller's thread (drain/join), never on an encoder thread."""

    def __init__(self, book: Book, plan: list[list[Chunk]], workdir: Path, chapter_dir: Path, keep_wav: bool, opts: Options):
        self.book, self.plan, self.workdir, self.dir, self.keep_wav, self.opts = book, plan, workdir, chapter_dir, keep_wav, opts
        self.missing = [{id(c) for c in chunks if not chunk_path(workdir, c).exists()} for chunks in plan]
        self.started: set[int] = set()
        self.submitted: set[int] = set()
        self.durations: dict[int, float] = {}
        self.warnings: dict[int, list[str]] = {}
        self._pending: dict = {}                      # future -> chapter index (not yet announced)
        self._pool: ThreadPoolExecutor | None = None
        stamp = chapter_dir / ".workdir"
        chapter_dir.mkdir(parents=True, exist_ok=True)
        if not stamp.exists() or stamp.read_text() != workdir.name:    # files from a different job/voice: start clean
            for f in chapter_dir.glob("*.m4a"):
                f.unlink()
            stamp.write_text(workdir.name)

    def path(self, ci: int) -> Path:
        return self.dir / f"{ci + 1:03d}.m4a"

    def _qc_path(self, ci: int) -> Path:
        return self.workdir / f"chapter_{ci:03d}.qc.json"

    def _build(self, ci: int) -> tuple[float, list[str]]:
        """Runs on an encoder thread: make the chapter file (or reuse a complete one) -> (duration, qc warnings)."""
        dest = self.path(ci)
        if dest.exists() and dest.stat().st_size > 0 and self._qc_path(ci).exists():
            return probe_duration(dest), json.loads(self._qc_path(ci).read_text())
        ch = self.book.chapters[ci]
        wav, _, warns = assemble_chapter(ci, self.plan[ci], self.workdir)
        encode_chapter(wav, dest, title=ch.title, artist=self.book.author, album=self.book.title,
                       track=f"{ci + 1}/{len(self.plan)}")
        if not self.keep_wav:
            wav.unlink(missing_ok=True)
        self._qc_path(ci).write_text(json.dumps(warns))
        return probe_duration(dest), warns

    def submit(self, ci: int) -> None:
        if ci in self.submitted:                      # never encode a chapter twice
            return
        self.submitted.add(ci)
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=ENCODE_WORKERS, thread_name_prefix="encode")
        self._pending[self._pool.submit(self._build, ci)] = ci

    def _announce(self, fut, ci: int) -> None:
        self.durations[ci], self.warnings[ci] = fut.result()          # re-raises an encoder failure here
        _emit(self.opts, "chapter_ready", index=ci, title=self.book.chapters[ci].title, path=str(self.path(ci)),
              duration=self.durations[ci])

    def drain(self) -> None:
        """Announce every chapter whose file is finished (non-blocking)."""
        for fut in [f for f in self._pending if f.done()]:
            self._announce(fut, self._pending.pop(fut))

    def join(self) -> None:
        """Wait for every submitted chapter and announce it (the M4B join must see all of them)."""
        while self._pending:
            done, _ = wait(list(self._pending), return_when=FIRST_COMPLETED)
            for fut in done:
                self._announce(fut, self._pending.pop(fut))

    def shutdown(self) -> None:
        """Stop without announcing: queued encodes are dropped, a running one finishes its atomic write (the next run
        reuses complete chapter files and redoes the rest)."""
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None
        self._pending.clear()

    def catch_up(self) -> None:
        """Chapters already complete from an earlier run are queued straight away (playable before the model loads)."""
        for ci, miss in enumerate(self.missing):
            if not miss:
                self.submit(ci)

    def on_chunk(self, c: Chunk, n: int, total: int) -> None:
        ci = c.chapter
        if ci not in self.started:
            self.started.add(ci)
            _emit(self.opts, "chapter_start", index=ci, title=self.book.chapters[ci].title)
        self.missing[ci].discard(id(c))
        if not self.missing[ci]:
            self.submit(ci)
        _emit(self.opts, "chunk_progress", done=n, total=total)


def _cancel_check(opts: Options) -> None:
    if opts.should_cancel and opts.should_cancel():
        raise Cancelled("cancelled")


def deliver(src: Path, out_dir: Path, name: str) -> Path:
    """Copy a finished file/folder to `out_dir/name` under a hidden .part name, then rename (a sync client never sees
    half a file). An existing output is renamed .old-<time>, never overwritten."""
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / name
    tmp = out_dir / ("." + name + ".part") if src.is_file() else out_dir / (name + ".part")
    if tmp.exists():
        shutil.rmtree(tmp) if tmp.is_dir() else tmp.unlink()
    if src.is_dir():
        shutil.copytree(src, tmp)
    else:
        shutil.copy2(src, tmp)
    if final.exists():
        stamp = int(time.time())
        final.rename(final.with_name(f"{final.stem}.old-{stamp}{final.suffix}") if final.is_file()
                     else final.with_name(final.name + f".old-{stamp}"))
    os.replace(tmp, final)
    return final


def convert(src: Path, opts: Options) -> Result:
    src = Path(src)
    _emit(opts, "stage", state="extracting")
    book = load_book(src, opts)
    _cancel_check(opts)
    log.info("converting %s (voice=%s speed=%s fmt=%s)", src.name, opts.voice, opts.speed, opts.fmt)
    log.info("book: %r by %s, %d chapters, %d words; dropped: %s; LLM cleanup: %s", book.title, book.author,
             len(book.chapters), book.words, book.dropped, book.llm_provider)
    plan = plan_chunks(book)

    st = config.get_settings()
    root = opts.work_root or st.work_dir
    workdir = root / book_hash(src, opts.voice, opts.speed)
    workdir.mkdir(parents=True, exist_ok=True)
    book.save(workdir, source=src.name, extra={"job": {"voice": opts.voice, "speed": opts.speed}})
    cover = _prepare_cover(book, workdir)
    _emit(opts, "extracted", title=book.title, author=book.author, words=book.words, llm_provider=book.llm_provider,
          cover=str(cover) if cover else None,
          chapters=[{"index": i, "title": ch.title, "paragraphs": len(ch.paragraphs), "para_start": 0,
                     "para_end": len(ch.paragraphs) - 1} for i, ch in enumerate(book.chapters)])
    pdf_art = book.artifacts.get("searchable_pdf")
    if pdf_art and Path(pdf_art).exists():          # keep the searchable PDF next to the job (future annotations / library)
        dest = workdir / "searchable.pdf"
        dest.unlink(missing_ok=True)
        try:
            os.link(pdf_art, dest)
        except OSError:
            shutil.copy2(pdf_art, dest)
    (workdir / "chunks.json").write_text(json.dumps(
        [{"chapter": c.chapter, "index": c.index, "kind": c.kind, "paragraph": c.paragraph, "start": c.start,
          "end": c.end, "file": chunk_path(workdir, c).name} for ch in plan for c in ch], indent=0), encoding="utf-8")

    engine_factory = opts.engine_factory
    if engine_factory is None:
        from .engines import make_engine as engine_factory  # noqa: F811
    all_chunks = [c for ch in plan for c in ch]
    total = len(all_chunks)
    todo = sum(1 for c in all_chunks if not chunk_path(workdir, c).exists())
    assembly = total * EST_ASSEMBLY_SEC_PER_CHUNK
    est = todo / EST_CHUNKS_PER_SEC + assembly
    chapter_dir = Path(opts.chapter_dir) if opts.chapter_dir else workdir / "chapters"
    chapters = _ChapterBuilder(book, plan, workdir, chapter_dir, keep_wav=opts.fmt == "mp3", opts=opts)
    _emit(opts, "stage", state="voicing")
    _emit(opts, "planned", title=book.title, author=book.author, chapters=len(book.chapters), words=book.words,
          chunks=total, resumed=total - todo, est_seconds=est)
    def pump() -> None:                 # runs between chunks on the GPU thread: announce finished chapters, then side-tasks
        chapters.drain()
        if opts.pump:
            opts.pump()

    try:
        chapters.catch_up()                 # chapters finished by an earlier run are playable before the model even loads
        # Load the model lazily: a fully-checkpointed rerun should not need the GPU at all.
        engine = None
        if todo:
            _cancel_check(opts)
            engine = engine_factory(opts.voice, opts.speed, opts.device)
        resumed, new = (total, 0)
        if engine is not None:
            resumed0 = total - todo
            t_syn = time.time()
            sent: set[float] = set()

            def on_progress(n: int, tot: int) -> None:
                frac = n / tot
                new_done = n - resumed0
                for m in PROGRESS_MILESTONES:
                    # skip milestones already passed before a resume, and short books
                    if frac >= m and m not in sent and m > resumed0 / tot and est >= MIN_ESTIMATE_FOR_PROGRESS:
                        sent.add(m)
                        rate = new_done / max(time.time() - t_syn, 1e-6)
                        remaining = (tot - n) / rate + assembly if rate > 0 else None
                        _emit(opts, "progress", title=book.title, percent=int(m * 100), remaining_seconds=remaining)

            resumed, new = synthesize(plan, workdir, engine, opts.stop_after_chunks, progress=on_progress,
                                      on_chunk=chapters.on_chunk, should_cancel=opts.should_cancel, pump=pump)
            del engine
        chapters.catch_up()
        chapters.join()                 # every chapter file exists (and is announced) before the M4B join
        _cancel_check(opts)
    finally:
        chapters.shutdown()      # no-op after join(); on cancel/failure drops queued encodes and waits for a running one

    _emit(opts, "stage", state="assembling")
    audio = [chapters.path(i) for i in range(len(plan))]
    durs = [chapters.durations[i] for i in range(len(plan))]
    spans, t = [], 0.0
    for d in durs:
        spans.append((t, t + d))
        t += d
    qc = [w for i in range(len(plan)) for w in chapters.warnings.get(i, [])]
    out_dir = Path(opts.out_dir) if opts.out_dir else st.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    base = safe_name(f"{book.title} - {book.author}")
    if opts.fmt == "mp3":
        final = out_dir / base
        tmp = out_dir / (base + ".part")
        if tmp.exists():
            shutil.rmtree(tmp)
        sources = [w if (w := workdir / f"chapter_{i:03d}.wav").exists() else a for i, a in enumerate(audio)]
        build_mp3s(book, sources, workdir, tmp)
        if final.exists():
            final.rename(final.with_name(final.name + f".old-{int(time.time())}"))
        tmp.rename(final)
    else:
        final = out_dir / f"{base}.m4b"
        tmp = out_dir / f".{base}.part.m4b"
        build_m4b(book, audio, spans, workdir, tmp)
        if final.exists():
            final.rename(final.with_name(f"{base}.old-{int(time.time())}.m4b"))
        os.replace(tmp, final)

    duration = spans[-1][1] if spans else 0.0
    res = Result(path=final, title=book.title, author=book.author, duration=duration, size=_dir_size(final),
                 chapters=[(c.title, s, e) for c, (s, e) in zip(book.chapters, spans)], qc_warnings=qc,
                 chunks_total=len(all_chunks), chunks_resumed=resumed, llm=book.llm_provider)
    (workdir / "qc.json").write_text(json.dumps(qc, indent=1))
    log.info("done: %s (%.1f min, %.1f MB, %d QC warnings)", final, duration / 60, res.size / 1e6, len(qc))
    if not opts.keep_work:
        shutil.rmtree(workdir, ignore_errors=True)
    return res


def fmt_duration(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"
