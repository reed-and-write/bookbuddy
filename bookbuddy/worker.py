"""The single worker: owns the GPU, runs queued jobs one at a time, resumes interrupted ones, and runs GPU side-tasks
(voice samples) between chunks. Plus the server lock (one server per data dir) and the event bus behind /api/events."""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import threading
import time
import traceback
from concurrent.futures import Future
from pathlib import Path
from typing import Callable

from . import config
from .library import Library
from .pipeline import Cancelled, Options, convert, deliver

log = logging.getLogger("bookbuddy.worker")

try:
    import fcntl
except ImportError:          # Windows (best effort, untested): lock the first byte with msvcrt instead
    fcntl = None
    import msvcrt


def _try_lock(fd: int, shared: bool = False) -> bool:
    try:
        if fcntl is not None:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


# ------------------------------------------------------------------ one server per data dir

class AlreadyRunning(RuntimeError):
    pass


def lock_path() -> Path:
    return config.data_dir() / "server.lock"


class ServerLock:
    """flock on <data>/server.lock for as long as the server (and its worker) lives. The file also says where it listens."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else lock_path()
        self._fd: int | None = None

    def acquire(self, info: dict | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        if not _try_lock(fd):
            os.close(fd)
            raise AlreadyRunning(f"another Book Buddy server already owns {self.path.parent}") from None
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), **(info or {})}).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.ftruncate(self._fd, 0)
                _unlock(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None


def server_running(path: Path | None = None) -> dict | None:
    """Info about a running server for this data dir, or None. (The kernel drops the lock if the server dies.)"""
    p = Path(path) if path else lock_path()
    if not p.exists():
        return None
    fd = os.open(p, os.O_RDONLY)
    try:
        if not _try_lock(fd, shared=True):
            try:
                return json.loads(os.read(fd, 4096).decode() or "{}") or {"pid": None}
            except (ValueError, OSError):
                return {"pid": None}
        _unlock(fd)
        return None
    finally:
        os.close(fd)


# ------------------------------------------------------------------ events

class EventBus:
    """Fan-out of job/chapter events to SSE subscribers. Thread-safe; a slow subscriber drops events, never blocks the worker."""

    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._seq = 0

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, type_: str, **data) -> None:
        with self._lock:
            self._seq += 1
            ev = {"id": self._seq, "type": type_, **data}
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass


# ------------------------------------------------------------------ the worker

class Worker:
    PROGRESS_EVERY = 0.5          # seconds between progress events/DB writes while voicing

    def __init__(self, library: Library, bus: EventBus | None = None, engine_factory: Callable | None = None,
                 poll: float = 1.0, lock: ServerLock | None = None):
        self.lib = library
        self.bus = bus or EventBus()
        self.engine_factory = engine_factory
        self.poll = poll
        self.lock = lock
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._tasks: queue.Queue = queue.Queue()
        self.current_job: int | None = None

    # -- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if self.lock is not None:
            self.lock.acquire()
        self._stop.clear()
        resumed = self.lib.requeue_interrupted()
        if resumed:
            log.info("resuming %d interrupted job(s): %s", len(resumed), resumed)
        self._thread = threading.Thread(target=self._loop, name="bookbuddy-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
        if self.lock is not None:
            self.lock.release()

    def wake(self) -> None:
        self._wake.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- GPU side tasks (voice samples): run on the worker thread, so the GPU still has exactly one user
    def submit(self, fn: Callable) -> Future:
        fut: Future = Future()
        self._tasks.put((fn, fut))
        self._wake.set()
        return fut

    def _drain_tasks(self) -> None:
        while True:
            try:
                fn, fut = self._tasks.get_nowait()
            except queue.Empty:
                return
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn())
            except BaseException as e:  # noqa: BLE001
                fut.set_exception(e)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._drain_tasks()
                job = self.lib.claim_next()
                if job is None:
                    self._wake.wait(self.poll)
                    self._wake.clear()
                    continue
                self.run_job(job)
            except Exception:  # noqa: BLE001 - the worker must survive anything
                log.exception("worker loop error")
                time.sleep(1)

    # -- one job
    def _publish_job(self, job_id: int) -> None:
        j = self.lib.get_job(job_id)
        if j:
            self.bus.publish("job", job=_public_job(j))

    def run_job(self, job: dict) -> None:
        from .jobs import move_source, notifier_hook, write_failure_note
        from .notify import Notifier

        lib, jid, bid = self.lib, job["id"], job["book_id"]
        st = config.get_settings()
        book = lib.get_book(bid)
        o = job["options"]
        origin = job["origin"]
        source = Path(book["source_path"])
        bookdir = st.library_dir / str(bid)
        bookdir.mkdir(parents=True, exist_ok=True)
        notifier = Notifier(Path(o["out_dir"]) if o.get("out_dir") else None) if origin == "watch" else None
        hook = notifier_hook(notifier) if notifier else None
        self.current_job = jid
        t_voice = {"t": None, "last": 0.0}
        log.info("job %d (%s) starting: %s", jid, origin, book["source_name"])
        self._publish_job(jid)
        self.bus.publish("book", book_id=bid, status="processing")
        if notifier:
            notifier.started(book["source_name"])

        def set_state(state: str) -> None:
            lib.set_job(jid, state=state, progress=0, eta=None)
            self._publish_job(jid)

        def on_event(kind: str, **info) -> None:
            if hook:
                hook(kind, **info)
            if kind == "stage":
                set_state(info["state"])
            elif kind == "extracted":
                fields = {"title": info["title"], "author": info["author"], "llm_provider": info["llm_provider"]}
                if info.get("cover") and Path(info["cover"]).is_file():
                    dest = bookdir / "cover.jpg"
                    shutil.copy2(info["cover"], dest)
                    fields["cover_path"] = str(dest)
                lib.set_book(bid, **fields)
                lib.replace_chapters(bid, info["chapters"])
                self.bus.publish("book", book_id=bid, title=info["title"], author=info["author"],
                                 chapters=[{"index": c["index"], "title": c["title"], "status": (lib.get_chapter(bid, c["index"]) or {}).get("status", "queued")}
                                           for c in info["chapters"]])
            elif kind in ("ocr_plan", "ocr_progress"):
                if (lib.get_job(jid) or {}).get("state") != "ocr":
                    set_state("ocr")
                if kind == "ocr_progress":
                    lib.set_job(jid, progress=info["percent"] / 100, eta=info.get("remaining_seconds"))
                self.bus.publish("ocr", job_id=jid, book_id=bid, **_scalars(info))
            elif kind in ("llm_plan", "llm_progress"):
                if kind == "llm_plan" and info.get("to_send") and (lib.get_job(jid) or {}).get("state") != "cleaning":
                    set_state("cleaning")
                if kind == "llm_progress":
                    lib.set_job(jid, progress=info["done"] / max(1, info["total"]), eta=info.get("eta_seconds"))
                self.bus.publish("llm", job_id=jid, book_id=bid, **_scalars(info))
            elif kind == "planned":
                lib.set_job(jid, eta=info["est_seconds"])
                t_voice["t"] = time.monotonic()
                self.bus.publish("planned", job_id=jid, book_id=bid, **_scalars(info))
            elif kind == "chunk_progress":
                now = time.monotonic()
                if now - t_voice["last"] >= self.PROGRESS_EVERY or info["done"] == info["total"]:
                    t_voice["last"] = now
                    frac = info["done"] / max(1, info["total"])
                    eta = None
                    if t_voice["t"] and frac > 0:
                        el = now - t_voice["t"]
                        eta = el * (1 - frac) / frac
                    lib.set_job(jid, progress=frac, eta=eta)
                    self.bus.publish("progress", job_id=jid, book_id=bid, progress=round(frac, 4), eta=eta)
            elif kind == "chapter_start":
                lib.set_chapter(bid, info["index"], status="voicing")
                self.bus.publish("chapter", book_id=bid, index=info["index"], status="voicing")
            elif kind == "chapter_ready":
                lib.set_chapter(bid, info["index"], status="ready", audio_path=info["path"], duration=info["duration"])
                self.bus.publish("chapter", book_id=bid, index=info["index"], status="ready", title=info["title"],
                                 duration=info["duration"])

        opts = Options(voice=book["voice"], speed=book["speed"], fmt=book["format"], out_dir=bookdir,
                       chapter_dir=bookdir / "chapters", llm=o.get("llm"), engine_factory=self.engine_factory,
                       on_event=on_event, should_cancel=lambda: lib.cancel_requested(jid) or self._stop.is_set(),
                       pump=self._drain_tasks, device=st.device, max_chapters=o.get("max_chapters"),
                       ocr_lang=o.get("ocr_lang"), ocr_backend=o.get("ocr_backend"), ocr_workers=o.get("ocr_workers"))
        try:
            res = convert(source, opts)
            out = res.path
            if origin in ("watch", "cli"):
                try:
                    target = Path(o["out_dir"]) if o.get("out_dir") else st.outbox
                    deliver(out, target, out.name)
                except Exception:  # noqa: BLE001 - the book is safe in the library; say so
                    log.exception("could not copy the finished book to %s", o.get("out_dir"))
            lib.set_book(bid, status="ready", total_duration=res.duration, output_path=str(out), error=None,
                         llm_provider=res.llm)
            lib.set_job(jid, state="done", progress=1.0, eta=0, finished=time.time())
            if origin == "watch" and o.get("inbox") and o.get("source_path") and Path(o["source_path"]).exists():
                try:
                    move_source(Path(o["source_path"]), Path(o["inbox"]) / "done")
                except Exception:  # noqa: BLE001
                    log.exception("could not move the finished source")
            if notifier:
                notifier.finished(book["source_name"], res)
            self.bus.publish("done", job_id=jid, book_id=bid, duration=res.duration)
            log.info("job %d done: %s (%.1f min)", jid, res.title, res.duration / 60)
        except Cancelled:
            self._reset_voicing_chapters(bid)
            if self._stop.is_set() and not lib.cancel_requested(jid):     # server shutting down: resume on next start
                lib.set_job(jid, state="queued", progress=0, eta=None)
                lib.set_book(bid, status="queued")
                log.info("job %d paused for shutdown (it resumes on the next start)", jid)
            else:
                lib.set_job(jid, state="cancelled", finished=time.time(), eta=None)
                lib.set_book(bid, status="cancelled")
                self.bus.publish("cancelled", job_id=jid, book_id=bid)
                log.info("job %d cancelled", jid)
        except Exception as e:  # noqa: BLE001
            reason = str(e) or type(e).__name__
            log.error("job %d failed: %s", jid, reason)
            log.debug("traceback:\n%s", traceback.format_exc())
            lib.set_job(jid, state="failed", error=reason, finished=time.time(), eta=None)
            lib.set_book(bid, status="failed", error=reason)
            self._reset_voicing_chapters(bid)
            if origin == "watch" and o.get("inbox") and o.get("source_path") and Path(o["source_path"]).exists():
                try:
                    write_failure_note(Path(o["source_path"]), Path(o["inbox"]), reason, traceback.format_exc())
                except Exception:  # noqa: BLE001
                    log.exception("could not move the failed source")
            if notifier:
                notifier.failed(book["source_name"], reason)
            self.bus.publish("failed", job_id=jid, book_id=bid, error=reason)
        finally:
            self.current_job = None
            self._publish_job(jid)

    def _reset_voicing_chapters(self, bid: int) -> None:
        for ch in self.lib.chapters(bid):
            if ch["status"] == "voicing":
                self.lib.set_chapter(bid, ch["idx"], status="queued")


def _scalars(info: dict) -> dict:
    return {k: v for k, v in info.items() if isinstance(v, (str, int, float, bool, type(None)))}


def _public_job(j: dict) -> dict:
    keep = ("id", "book_id", "origin", "state", "progress", "eta", "error", "created", "started", "finished", "attempts")
    return {k: j.get(k) for k in keep}
