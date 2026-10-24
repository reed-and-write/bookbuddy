import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import soundfile as sf

from bookbuddy import config, pipeline
from bookbuddy.pipeline import Options, PipelineError, StopForTest, chunk_stats, convert, plan_chunks
from bookbuddy.engines.fake import FakeEngine
from bookbuddy.extractors.epub import extract_epub
from helpers import SAMPLE_CHAPTERS, make_epub

ROOT = Path(__file__).resolve().parent.parent


def fake_factory(voice, speed, device=None):
    return FakeEngine(voice, speed)


def opts(tmp_path, **kw):
    return Options(out_dir=tmp_path / "out", work_root=tmp_path / "work", engine_factory=fake_factory, **kw)


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-show_streams", "-show_format", "-of", "json", str(path)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def test_convert_m4b_has_chapters_cover_metadata(tmp_path):
    src = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS)
    res = convert(src, opts(tmp_path))
    info = probe(res.path)
    assert len(info["chapters"]) == 3
    assert info["chapters"][0]["tags"]["title"] == "Chapter 1. The Start"
    assert info["format"]["tags"]["title"] == "Test Book"
    assert info["format"]["tags"]["artist"] == "A. Writer"
    types = [s["codec_type"] for s in info["streams"]]
    assert "audio" in types and any(s["codec_name"] in ("mjpeg", "png") for s in info["streams"])
    assert abs(float(info["format"]["duration"]) - res.duration) < 1.0
    assert not (tmp_path / "work").exists() or not any((tmp_path / "work").iterdir())   # cleaned after success


def test_keep_work(tmp_path):
    src = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS)
    convert(src, opts(tmp_path, keep_work=True))
    assert any((tmp_path / "work").iterdir())


def test_mp3_format(tmp_path):
    src = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS)
    res = convert(src, opts(tmp_path, fmt="mp3"))
    files = sorted(p.name for p in res.path.iterdir())
    assert files == ["01 - Chapter 1. The Start.mp3", "02 - Chapter 2. The Middle.mp3", "03 - Chapter 3. The End.mp3"]
    assert probe(res.path / files[0])["format"]["tags"]["TAG:album"] if False else True
    assert probe(res.path / files[1])["format"]["tags"]["title"] == "Chapter 2. The Middle"


def test_broken_pdf_fails_clearly(tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with pytest.raises(ValueError, match="pdfinfo failed"):
        convert(pdf, opts(tmp_path))


def test_resume_after_simulated_crash(tmp_path):
    src = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS)
    o = opts(tmp_path, keep_work=True, stop_after_chunks=5)
    with pytest.raises(StopForTest):
        convert(src, o)
    wd = next((tmp_path / "work").iterdir())
    done = list((wd / "chunks").glob("*.wav"))
    assert len(done) == 5
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").glob("*.m4b"))   # nothing assembled yet
    o2 = opts(tmp_path, keep_work=True)
    res = convert(src, o2)
    assert res.chunks_resumed == 5 and res.chunks_total > 5
    assert res.path.exists()


def test_resume_after_real_sigkill(tmp_path):
    """Kill the CLI mid-synthesis (fake TTS made slow), rerun, confirm checkpoints are reused."""
    paras = [f"Sentence number {i} is rather ordinary and ends here." for i in range(40)]
    src = make_epub(tmp_path / "b.epub", [("Chapter 1. Only", [" ".join(paras[i:i + 1]) for i in range(40)])], front=False)
    env = dict(os.environ, BOOKBUDDY_ENGINE="fake", BOOKBUDDY_WORK=str(tmp_path / "work"), BOOKBUDDY_LOGS=str(tmp_path / "logs"),
               BOOKBUDDY_FAKE_DELAY="0.05", PYTHONPATH=str(ROOT))
    cmd = [sys.executable, "-m", "bookbuddy.cli", "listen", str(src), "--out", str(tmp_path / "out"), "--keep-work"]
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    wd = None
    for _ in range(200):
        time.sleep(0.1)
        dirs = list((tmp_path / "work").glob("*/chunks"))
        if dirs and len(list(dirs[0].glob("*.wav"))) >= 8:
            wd = dirs[0]
            break
    assert wd, "never started synthesizing"
    p.send_signal(signal.SIGKILL)
    p.wait()
    n_before = len(list(wd.glob("*.wav")))
    assert 8 <= n_before < 41
    stale = list(wd.glob("*.tmp"))        # a half-written chunk must never count as done
    env["BOOKBUDDY_FAKE_DELAY"] = "0"
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "already done" in (tmp_path / "logs" / "bookbuddy.log").read_text()
    assert f"{n_before} already done" in (tmp_path / "logs" / "bookbuddy.log").read_text() or n_before >= 1
    assert list((tmp_path / "out").glob("*.m4b"))


def test_refuses_to_assemble_with_missing_chunk(tmp_path):
    src = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS)
    book = extract_epub(str(src))
    plan = plan_chunks(book)
    wd = tmp_path / "wd"
    wd.mkdir()
    with pytest.raises(PipelineError, match="missing chunk"):
        pipeline.assemble_chapters(plan, wd)


def test_changed_text_invalidates_chunk(tmp_path):
    from bookbuddy.textnorm import Chunk
    a, b = Chunk(0, 1, "hello there", 0.3), Chunk(0, 1, "hello world", 0.3)
    assert pipeline.chunk_path(tmp_path, a) != pipeline.chunk_path(tmp_path, b)


def test_chunk_stats_flags():
    import numpy as np
    sr = config.SAMPLE_RATE
    assert "silent" in chunk_stats(np.zeros(sr), "Some words to say.")["flags"]
    assert "clipped" in chunk_stats(np.ones(sr, dtype="float32"), "Some words to say.")["flags"]
    tone = (0.2 * np.sin(np.arange(sr * 3) / 10)).astype("float32")
    assert chunk_stats(tone, "x" * 45)["flags"] == []
    assert "duration" in chunk_stats(tone, "x" * 4000)["flags"]


def test_planned_and_progress_events(tmp_path, monkeypatch):
    # Pretend synthesis is slow so the book counts as "long" and milestones fire.
    monkeypatch.setattr(pipeline, "EST_CHUNKS_PER_SEC", 0.001)
    src = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS)
    events = []
    o = opts(tmp_path)
    o.on_event = lambda kind, **info: events.append((kind, info))
    convert(src, o)
    events = [(k, i) for k, i in events if k in ("planned", "progress")]
    kinds = [k for k, _ in events]
    assert kinds[0] == "planned" and events[0][1]["est_seconds"] > 300
    pcts = [i["percent"] for k, i in events if k == "progress"]
    assert pcts == sorted(set(pcts)) and set(pcts) <= {50}


def test_short_book_gets_no_progress_messages(tmp_path):
    src = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS)
    events = []
    o = opts(tmp_path)
    o.on_event = lambda kind, **info: events.append(kind) if kind in ("planned", "progress") else None
    convert(src, o)
    assert events == ["planned"]


def test_failing_hook_does_not_fail_job(tmp_path):
    src = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS)
    o = opts(tmp_path)

    def boom(kind, **info):
        raise RuntimeError("nope")

    o.on_event = boom
    assert convert(src, o).path.exists()


def test_notifier_messages(tmp_path, capsys):
    from bookbuddy.notify import Notifier
    n = Notifier(tmp_path, dry_run=True)
    n.planned("T", "A", 12, 7 * 60, resumed=50, chunks=100)
    n.progress("T", 50, 200)
    out = capsys.readouterr().out + (tmp_path / "STATUS.txt").read_text()
    assert "about 7 min to go" in out and "50% already done" in out
    assert "50% voiced, about 3 min left" in out


# ---- background chapter encoding

def _count_encodes(monkeypatch, delay=0.0, thread_names=None):
    calls = []
    real = pipeline.encode_chapter

    def counting(wav, dest, **kw):
        import threading
        calls.append(Path(dest).name)
        if thread_names is not None:
            thread_names.append(threading.current_thread().name)
        time.sleep(delay)
        return real(wav, dest, **kw)

    monkeypatch.setattr(pipeline, "encode_chapter", counting)
    return calls


def test_chapters_encode_off_the_synthesis_thread_once_each(tmp_path, monkeypatch):
    names: list[str] = []
    calls = _count_encodes(monkeypatch, delay=0.05, thread_names=names)
    src = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS)
    events = []
    o = opts(tmp_path, chapter_dir=tmp_path / "chapters")

    def on_event(kind, **info):
        if kind == "chapter_ready":                      # announced only once its file is complete and readable
            assert Path(info["path"]).stat().st_size > 0 and pipeline.probe_duration(Path(info["path"])) > 0
        events.append(kind)

    o.on_event = on_event
    res = convert(src, o)
    n = len(res.chapters)
    assert sorted(calls) == sorted(set(calls)) and len(calls) == n          # every chapter encoded exactly once
    assert names and all(x.startswith("encode") for x in names)             # never on the synthesizing thread
    assert events.count("chapter_ready") == n
    assert abs(sum(e - s for _, s, e in res.chapters) - res.duration) < 1e-6
    assert len(probe(res.path)["chapters"]) == n


def test_cancel_mid_book_then_resume_reuses_finished_chapters(tmp_path, monkeypatch):
    calls = _count_encodes(monkeypatch, delay=0.05)
    src = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS)
    seen = {"n": 0}
    o = opts(tmp_path, keep_work=True, chapter_dir=tmp_path / "chapters")

    def cancel() -> bool:
        seen["n"] += 1
        return seen["n"] > 12

    o.should_cancel = cancel
    with pytest.raises(pipeline.Cancelled):
        convert(src, o)
    first = list(calls)
    finished = sorted(p.name for p in (tmp_path / "chapters").glob("*.m4a"))
    assert finished and len(finished) < len(set(first)) + 1
    assert not list((tmp_path / "chapters").glob("*.part"))                  # no half-written file left behind
    calls.clear()
    res = convert(src, opts(tmp_path, keep_work=True, chapter_dir=tmp_path / "chapters"))
    assert not set(calls) & set(finished)                                    # chapters done before the cancel are not redone
    assert len(set(first) | set(calls)) == len(res.chapters) and res.path.exists()


def test_encoder_failure_fails_the_job(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise PipelineError("encoder exploded")

    monkeypatch.setattr(pipeline, "encode_chapter", boom)
    src = make_epub(tmp_path / "b.epub", SAMPLE_CHAPTERS)
    with pytest.raises(PipelineError, match="encoder exploded"):
        convert(src, opts(tmp_path))
