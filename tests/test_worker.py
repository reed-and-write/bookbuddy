"""Worker: one GPU user, progressive chapters, M4B from chapter files, resume, cancel, retry, shutdown."""
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from bookbuddy import config, intake
from bookbuddy.engines.fake import FakeEngine
from bookbuddy.library import Library
from bookbuddy.worker import AlreadyRunning, EventBus, ServerLock, Worker, server_running
from helpers import SAMPLE_CHAPTERS, make_epub


class TrackingEngine(FakeEngine):
    """Records concurrent use (GPU exclusivity), call counts and optionally blocks or fails."""
    lock = threading.Lock()
    active = 0
    max_active = 0
    calls = 0
    delay = 0.0
    fail_at: int | None = None
    on_call = None

    @classmethod
    def reset(cls):
        cls.active = cls.max_active = cls.calls = 0
        cls.delay, cls.fail_at, cls.on_call = 0.0, None, None

    def synth(self, text):
        cls = TrackingEngine
        with cls.lock:
            cls.active += 1
            cls.max_active = max(cls.max_active, cls.active)
            cls.calls += 1
            n = cls.calls
        try:
            if cls.on_call:
                cls.on_call(n)
            if cls.fail_at is not None and n == cls.fail_at:
                raise RuntimeError("simulated GPU crash")
            time.sleep(cls.delay)
            return super().synth(text)
        finally:
            with cls.lock:
                cls.active -= 1


def factory(voice, speed, device=None):
    return TrackingEngine(voice, speed, device)


@pytest.fixture
def setup(env):
    TrackingEngine.reset()
    lib = Library(config.get_settings().db_path)
    bus = EventBus()
    events = []
    q = bus.subscribe()
    w = Worker(lib, bus, engine_factory=factory, poll=0.05)
    yield lib, w, events, q
    w.stop(10)
    lib.close()


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def book_file(tmp_path, name="book.epub", chapters=SAMPLE_CHAPTERS, title="Test Book"):
    return make_epub(tmp_path / name, chapters, title=title)


def enqueue(lib, path, origin="web", **kw):
    staged, sha, _ = intake.stage_path(path)
    return intake.enqueue_file(lib, staged, sha, origin=origin, voice="af_heart", speed=1.0, fmt=kw.pop("fmt", "m4b"),
                               llm="none", original_name=path.name, **kw)


def wait_for(cond, timeout=30, what="condition"):
    t = time.time()
    while time.time() - t < timeout:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def wait_state(lib, job_id, states, timeout=30):
    wait_for(lambda: lib.get_job(job_id)["state"] in states, timeout, f"job {job_id} -> {states}")
    return lib.get_job(job_id)


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_chapters", "-of", "json", str(path)], capture_output=True, text=True)
    return json.loads(r.stdout)


def test_job_runs_to_done_with_chapters_cover_and_m4b(setup, tmp_path):
    lib, w, _, q = setup
    book, job = enqueue(lib, book_file(tmp_path))
    w.start()
    assert wait_state(lib, job, {"done", "failed"})["state"] == "done"
    b = lib.get_book(book, chapters=True)
    assert b["status"] == "ready" and b["title"] == "Test Book" and b["author"] == "A. Writer"
    assert [c["status"] for c in b["chapters"]] == ["ready"] * 3
    assert all(Path(c["audio_path"]).suffix == ".m4a" and Path(c["audio_path"]).is_file() for c in b["chapters"])
    assert b["chapters"][0]["para_end"] >= 0 and Path(b["cover_path"]).is_file()
    assert Path(b["output_path"]).suffix == ".m4b" and b["total_duration"] > 0
    kinds = [e["type"] for e in drain(q)]
    assert {"job", "book", "chapter", "planned", "done"} <= set(kinds)


def test_chapter_is_playable_before_the_book_is_finished(setup, tmp_path):
    lib, w, _, q = setup
    seen = {}
    book, job = enqueue(lib, book_file(tmp_path, chapters=[(f"Chapter {i}", [f"Paragraph {i} " * 30] * 6) for i in range(1, 5)]))

    def at_call(n):
        ch = lib.get_chapter(book, 0)
        if ch and ch["status"] == "ready" and "first" not in seen:
            seen["first"] = (n, Path(ch["audio_path"]).is_file(), lib.get_job(job)["state"], lib.get_chapter(book, 3)["status"])

    TrackingEngine.on_call = at_call
    TrackingEngine.delay = 0.05      # slower than the background encode of one chapter
    w.start()
    wait_state(lib, job, {"done"})
    n, is_file, state, last = seen["first"]
    assert is_file and state == "voicing" and last != "ready"          # chapter 1 was ready while the book was still voicing
    assert n < TrackingEngine.calls                                    # ...before the later chapters were even synthesized
    events = drain(q)
    order = [(e["type"], e.get("index")) for e in events if e["type"] == "chapter" and e.get("status") == "ready"]
    assert [i for _, i in order] == [0, 1, 2, 3]


def test_m4b_is_built_from_the_chapter_files_without_resynthesis(setup, tmp_path):
    lib, w, _, _ = setup
    book, job = enqueue(lib, book_file(tmp_path))
    w.start()
    wait_state(lib, job, {"done"})
    from bookbuddy.extractors import extract_book
    from bookbuddy.pipeline import plan_chunks
    n_chunks = sum(len(c) for c in plan_chunks(extract_book(tmp_path / "book.epub")))
    b = lib.get_book(book, chapters=True)
    chapter_total = sum(probe(c["audio_path"])["format"]["duration"] and float(probe(c["audio_path"])["format"]["duration"]) for c in b["chapters"])
    info = probe(b["output_path"])
    assert abs(float(info["format"]["duration"]) - chapter_total) < 0.5
    assert [c["tags"]["title"] for c in info["chapters"]] == [c["title"] for c in b["chapters"]]
    assert abs(float(info["chapters"][-1]["end_time"]) - chapter_total) < 0.5
    # every chunk was synthesized exactly once: building the M4B never re-synthesizes
    assert TrackingEngine.calls == n_chunks
    assert not list(config.get_settings().work_dir.glob("*"))


def test_one_worker_never_uses_the_gpu_twice_at_once(setup, tmp_path):
    lib, w, _, _ = setup
    jobs = [enqueue(lib, book_file(tmp_path, f"b{i}.epub", title=f"Book {i}"))[1] for i in range(3)]
    TrackingEngine.delay = 0.002
    results = []
    w.start()
    futs = [w.submit(lambda: factory("v", 1.0).synth("sample " * 20)) for _ in range(4)]       # voice samples share the same thread
    for j in jobs:
        wait_state(lib, j, {"done"}, 60)
    [f.result(10) for f in futs]
    assert TrackingEngine.max_active == 1
    order = [lib.get_job(j)["started"] for j in jobs]
    assert order == sorted(order)                                                              # FIFO


def test_a_second_worker_cannot_start_in_the_same_data_dir(env):
    lib = Library(config.get_settings().db_path)
    a, b = Worker(lib, lock=ServerLock()), Worker(lib, lock=ServerLock())
    a.start()
    try:
        assert server_running() is not None
        with pytest.raises(AlreadyRunning):
            b.start()
    finally:
        a.stop(5)
    assert server_running() is None
    b.start()
    b.stop(5)


def test_failure_then_retry_resumes_from_checkpoints(setup, tmp_path):
    lib, w, _, _ = setup
    book, job = enqueue(lib, book_file(tmp_path))
    TrackingEngine.fail_at = 7
    w.start()
    j = wait_state(lib, job, {"failed", "done"})
    assert j["state"] == "failed" and "simulated GPU crash" in j["error"]
    assert lib.get_book(book)["status"] == "failed"
    from bookbuddy.extractors import extract_book
    from bookbuddy.pipeline import plan_chunks
    n_chunks = sum(len(c) for c in plan_chunks(extract_book(tmp_path / "book.epub")))
    lib.retry(job)
    w.wake()
    wait_state(lib, job, {"done"})
    assert lib.get_book(book)["status"] == "ready"
    assert TrackingEngine.calls == n_chunks + 1                    # the 6 finished chunks were not redone (only the crashed one twice)


def test_interrupted_job_resumes_on_start(setup, tmp_path):
    lib, w, _, _ = setup
    book, job = enqueue(lib, book_file(tmp_path))
    lib.claim_next()
    lib.set_job(job, state="voicing", progress=0.5)              # as if the server had been killed mid-job
    w.start()
    assert wait_state(lib, job, {"done"})["state"] == "done"


def test_cancel_running_job_then_retry(setup, tmp_path):
    lib, w, _, q = setup
    book, job = enqueue(lib, book_file(tmp_path, chapters=[(f"C{i}", ["Some words to say here. " * 20] * 8) for i in range(4)]))
    TrackingEngine.delay = 0.02
    w.start()
    wait_for(lambda: TrackingEngine.calls >= 5, what="voicing to start")
    assert lib.request_cancel(job) == "cancelling"
    j = wait_state(lib, job, {"cancelled", "done"})
    assert j["state"] == "cancelled" and lib.get_book(book)["status"] == "cancelled"
    assert all(c["status"] in ("queued", "ready") for c in lib.chapters(book))
    stopped_at = TrackingEngine.calls
    time.sleep(0.2)
    assert TrackingEngine.calls == stopped_at                     # really stopped
    TrackingEngine.delay = 0
    lib.retry(job)
    w.wake()
    wait_state(lib, job, {"done"})
    assert lib.get_book(book)["status"] == "ready"


def test_cancelling_a_queued_job_never_runs_it(setup, tmp_path):
    lib, w, _, _ = setup
    _, j1 = enqueue(lib, book_file(tmp_path, "1.epub"))
    _, j2 = enqueue(lib, book_file(tmp_path, "2.epub", title="Two"))
    lib.request_cancel(j2)
    w.start()
    wait_state(lib, j1, {"done"})
    assert lib.get_job(j2)["state"] == "cancelled"


def test_shutdown_pauses_a_running_job_and_it_resumes_next_start(setup, tmp_path):
    lib, w, _, _ = setup
    book, job = enqueue(lib, book_file(tmp_path, chapters=[(f"C{i}", ["Some words to say here. " * 20] * 8) for i in range(3)]))
    TrackingEngine.delay = 0.02
    w.start()
    wait_for(lambda: TrackingEngine.calls >= 4)
    w.stop(10)
    assert lib.get_job(job)["state"] == "queued"                   # paused, not cancelled
    TrackingEngine.delay = 0
    w.start()
    wait_state(lib, job, {"done"})


def test_unsupported_content_fails_the_job_with_a_message(setup, tmp_path):
    lib, w, _, _ = setup
    bad = tmp_path / "broken.epub"
    bad.write_bytes(b"this is not an epub")
    _, job = enqueue(lib, bad)
    w.start()
    j = wait_state(lib, job, {"failed"})
    assert j["error"]


def test_watch_job_delivers_to_outbox_and_moves_the_source(setup, tmp_path):
    lib, w, _, _ = setup
    inbox, outbox = tmp_path / "inbox", tmp_path / "outbox"
    inbox.mkdir()
    src = book_file(inbox, "dropped.epub")
    book, job = enqueue(lib, src, origin="watch", extra={"out_dir": str(outbox), "inbox": str(inbox), "source_path": str(src)})
    w.start()
    wait_state(lib, job, {"done"})
    assert [p.name for p in outbox.glob("*.m4b")] == ["Test Book - A. Writer.m4b"]
    assert (inbox / "done" / "dropped.epub").exists() and not src.exists()


def test_watch_job_failure_goes_to_failed_folder(setup, tmp_path):
    lib, w, _, _ = setup
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    src = inbox / "bad.epub"
    src.write_bytes(b"nope")
    _, job = enqueue(lib, src, origin="watch", extra={"out_dir": str(tmp_path / "o"), "inbox": str(inbox), "source_path": str(src)})
    w.start()
    wait_state(lib, job, {"failed"})
    assert (inbox / "failed" / "bad.epub").exists() and (inbox / "failed" / "bad.epub.error.txt").exists()


def test_mp3_format_job(setup, tmp_path):
    lib, w, _, _ = setup
    book, job = enqueue(lib, book_file(tmp_path), fmt="mp3")
    w.start()
    wait_state(lib, job, {"done"})
    out = Path(lib.get_book(book)["output_path"])
    assert out.is_dir() and len(list(out.glob("*.mp3"))) == 3
