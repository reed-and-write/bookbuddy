"""SQLite library + job queue. WAL mode, so the server, the CLI and the watcher can share one file safely.

Schema changes are numbered migrations (PRAGMA user_version); a database written by a newer Book Buddy is refused
rather than guessed at. Chapters keep their paragraph anchor range (first/last paragraph index inside the chapter),
so highlights and notes can attach to (chapter, paragraph) later.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE books (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        author TEXT NOT NULL DEFAULT '',
        source_name TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        source_path TEXT,
        cover_path TEXT,
        created REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',        -- queued | processing | ready | failed | cancelled
        voice TEXT NOT NULL,
        speed REAL NOT NULL,
        format TEXT NOT NULL DEFAULT 'm4b',
        llm_provider TEXT NOT NULL DEFAULT 'none',
        total_duration REAL NOT NULL DEFAULT 0,
        output_path TEXT,                              -- the finished M4B (or MP3 folder), inside the library
        error TEXT
    );
    CREATE INDEX books_hash ON books(source_hash);
    CREATE TABLE chapters (
        book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        idx INTEGER NOT NULL,
        title TEXT NOT NULL,
        para_start INTEGER NOT NULL DEFAULT 0,         -- first paragraph index in the chapter (anchor c<idx>.p<n>)
        para_end INTEGER NOT NULL DEFAULT -1,          -- last paragraph index (inclusive, -1 = empty chapter)
        audio_path TEXT,
        duration REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'queued',         -- queued | voicing | ready | failed
        PRIMARY KEY (book_id, idx)
    );
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        origin TEXT NOT NULL,                          -- web | watch | cli
        state TEXT NOT NULL DEFAULT 'queued',          -- queued | extracting | ocr | cleaning | voicing | assembling | done | failed | cancelled
        progress REAL NOT NULL DEFAULT 0,              -- 0..1
        eta REAL,                                      -- seconds left, when known
        error TEXT,
        options TEXT NOT NULL DEFAULT '{}',            -- JSON: llm, out_dir, inbox, source_path (watch/cli)
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        created REAL NOT NULL,
        started REAL,
        finished REAL
    );
    CREATE INDEX jobs_state ON jobs(state, id);
    CREATE TABLE playback (
        book_id INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
        chapter INTEGER NOT NULL DEFAULT 0,
        position REAL NOT NULL DEFAULT 0,
        updated REAL NOT NULL
    );
    CREATE TABLE bookmarks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        chapter INTEGER NOT NULL,
        position REAL NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL
    );
    CREATE INDEX bookmarks_book ON bookmarks(book_id);
    """,
]
SCHEMA_VERSION = len(MIGRATIONS)

ACTIVE_STATES = ("extracting", "ocr", "cleaning", "voicing", "assembling")
FINISHED_STATES = ("done", "failed", "cancelled")


class JobState:
    QUEUED = "queued"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LibraryError(RuntimeError):
    pass


def _now() -> float:
    return time.time()


class Library:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._migrate()

    # -- connections (one per thread; sqlite connections must not cross threads)
    @property
    def db(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
            with self._lock:
                self._conns.append(c)
        return c

    def close(self) -> None:
        with self._lock:
            for c in self._conns:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._conns.clear()
        self._local = threading.local()

    def _migrate(self) -> None:
        db = self.db
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise LibraryError(f"{self.path} was written by a newer Book Buddy (schema {version}, this one knows {len(MIGRATIONS)})")
        for v in range(version, len(MIGRATIONS)):
            db.execute("BEGIN IMMEDIATE")
            try:
                # re-check inside the lock: another process may have migrated while we waited
                if db.execute("PRAGMA user_version").fetchone()[0] > v:
                    db.execute("ROLLBACK")
                    continue
                for stmt in _split_sql(MIGRATIONS[v]):
                    db.execute(stmt)
                db.execute(f"PRAGMA user_version = {v + 1}")
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    @property
    def schema_version(self) -> int:
        return self.db.execute("PRAGMA user_version").fetchone()[0]

    # -- helpers
    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        if "options" in d:
            try:
                d["options"] = json.loads(d["options"] or "{}")
            except ValueError:
                d["options"] = {}
        return d

    def _rows(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        return [self._row(r) for r in self.db.execute(sql, tuple(args)).fetchall()]

    def _update(self, table: str, where: str, where_args: tuple, fields: dict, allowed: set[str]) -> None:
        bad = set(fields) - allowed
        if bad:
            raise LibraryError(f"cannot set {sorted(bad)} on {table}")
        if not fields:
            return
        vals = [json.dumps(v) if k == "options" else v for k, v in fields.items()]
        self.db.execute(f"UPDATE {table} SET {', '.join(f'{k}=?' for k in fields)} WHERE {where}", (*vals, *where_args))

    # ------------------------------------------------------------ books
    BOOK_FIELDS = {"title", "author", "source_name", "source_hash", "source_path", "cover_path", "status", "voice", "speed",
                   "format", "llm_provider", "total_duration", "output_path", "error"}

    def enqueue(self, *, title: str, source_name: str, source_hash: str, source_path: str | None, voice: str, speed: float,
                fmt: str, origin: str, author: str = "", options: dict | None = None) -> tuple[int, int]:
        """Create a book and its queued job in one transaction. Returns (book_id, job_id)."""
        if origin not in ("web", "watch", "cli"):
            raise LibraryError(f"bad job origin {origin!r}")
        now = _now()
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            cur = db.execute(
                "INSERT INTO books(title, author, source_name, source_hash, source_path, created, voice, speed, format) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (title, author, source_name, source_hash, source_path, now, voice, speed, fmt))
            book_id = cur.lastrowid
            cur = db.execute("INSERT INTO jobs(book_id, origin, options, created) VALUES (?,?,?,?)",
                             (book_id, origin, json.dumps(options or {}), now))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        return book_id, cur.lastrowid

    def get_book(self, book_id: int, chapters: bool = False) -> dict | None:
        b = self._row(self.db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone())
        if b and chapters:
            b["chapters"] = self.chapters(book_id)
            b["job"] = self.latest_job(book_id)
        return b

    def list_books(self) -> list[dict]:
        books = self._rows("SELECT * FROM books ORDER BY created DESC, id DESC")
        for b in books:
            b["chapters_ready"] = self.db.execute("SELECT COUNT(*) FROM chapters WHERE book_id=? AND status='ready'", (b["id"],)).fetchone()[0]
            b["chapters_total"] = self.db.execute("SELECT COUNT(*) FROM chapters WHERE book_id=?", (b["id"],)).fetchone()[0]
            b["job"] = self.latest_job(b["id"])
            b["listened"] = self._listened(b["id"])
        return books

    def _listened(self, book_id: int) -> float | None:
        """Seconds listened so far: whole chapters before the saved one + the position in it. None if never played."""
        pb = self.db.execute("SELECT chapter, position FROM playback WHERE book_id=?", (book_id,)).fetchone()
        if pb is None:
            return None
        before = self.db.execute("SELECT COALESCE(SUM(duration), 0) FROM chapters WHERE book_id=? AND idx<?",
                                 (book_id, pb[0])).fetchone()[0]
        return float(before) + float(pb[1])

    def set_book(self, book_id: int, **fields) -> None:
        self._update("books", "id=?", (book_id,), fields, self.BOOK_FIELDS)

    def delete_book(self, book_id: int) -> None:
        self.db.execute("DELETE FROM books WHERE id=?", (book_id,))

    # ------------------------------------------------------------ chapters
    def replace_chapters(self, book_id: int, chapters: list[dict]) -> None:
        """Set the chapter list (title + paragraph anchor range). Keeps status/audio of chapters that already exist."""
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            keep = {r["idx"]: r for r in db.execute("SELECT * FROM chapters WHERE book_id=?", (book_id,))}
            db.execute("DELETE FROM chapters WHERE book_id=?", (book_id,))
            for ch in chapters:
                old = keep.get(ch["index"])
                same = old is not None and old["title"] == ch["title"]
                db.execute("INSERT INTO chapters(book_id, idx, title, para_start, para_end, audio_path, duration, status) "
                           "VALUES (?,?,?,?,?,?,?,?)",
                           (book_id, ch["index"], ch["title"], ch.get("para_start", 0), ch.get("para_end", -1),
                            old["audio_path"] if same else None, old["duration"] if same else 0,
                            old["status"] if same else "queued"))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise

    def chapters(self, book_id: int) -> list[dict]:
        return self._rows("SELECT * FROM chapters WHERE book_id=? ORDER BY idx", (book_id,))

    def get_chapter(self, book_id: int, idx: int) -> dict | None:
        return self._row(self.db.execute("SELECT * FROM chapters WHERE book_id=? AND idx=?", (book_id, idx)).fetchone())

    def set_chapter(self, book_id: int, idx: int, **fields) -> None:
        self._update("chapters", "book_id=? AND idx=?", (book_id, idx), fields, {"status", "audio_path", "duration", "title"})

    # ------------------------------------------------------------ jobs / queue
    JOB_FIELDS = {"state", "progress", "eta", "error", "options", "cancel_requested", "attempts", "started", "finished"}

    def get_job(self, job_id: int) -> dict | None:
        return self._row(self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def latest_job(self, book_id: int) -> dict | None:
        return self._row(self.db.execute("SELECT * FROM jobs WHERE book_id=? ORDER BY id DESC LIMIT 1", (book_id,)).fetchone())

    def list_jobs(self, states: Iterable[str] | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT jobs.*, books.title AS title, books.source_name AS source_name FROM jobs JOIN books ON books.id=jobs.book_id"
        args: tuple = ()
        if states:
            states = tuple(states)
            sql += f" WHERE jobs.state IN ({','.join('?' * len(states))})"
            args = states
        return self._rows(sql + " ORDER BY jobs.id DESC LIMIT ?", (*args, limit))

    def set_job(self, job_id: int, **fields) -> None:
        self._update("jobs", "id=?", (job_id,), fields, self.JOB_FIELDS)

    def claim_next(self) -> dict | None:
        """Atomically take the oldest queued job (FIFO by id) and mark it extracting. None when the queue is empty."""
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            r = db.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if r is None:
                db.execute("COMMIT")
                return None
            db.execute("UPDATE jobs SET state='extracting', started=?, finished=NULL, error=NULL, progress=0, eta=NULL, "
                       "attempts=attempts+1 WHERE id=?", (_now(), r["id"]))
            db.execute("UPDATE books SET status='processing', error=NULL WHERE id=(SELECT book_id FROM jobs WHERE id=?)", (r["id"],))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        return self.get_job(r["id"])

    def request_cancel(self, job_id: int) -> str:
        """Cancel a job. Queued: cancelled at once. Running: flagged, the worker stops at the next chunk.
        Returns the resulting state ('cancelled' | 'cancelling'); finished jobs raise LibraryError."""
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            j = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if j is None:
                raise LibraryError("no such job")
            if j["state"] in FINISHED_STATES:
                raise LibraryError(f"job is already {j['state']}")
            if j["state"] == "queued":
                db.execute("UPDATE jobs SET state='cancelled', finished=?, cancel_requested=1 WHERE id=?", (_now(), job_id))
                db.execute("UPDATE books SET status='cancelled' WHERE id=?", (j["book_id"],))
                result = "cancelled"
            else:
                db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
                result = "cancelling"
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        return result

    def cancel_requested(self, job_id: int) -> bool:
        r = self.db.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(r and r["cancel_requested"])

    def retry(self, job_id: int) -> None:
        """Put a failed or cancelled job back in the queue (its checkpoints make it resume, not restart)."""
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            j = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if j is None:
                raise LibraryError("no such job")
            if j["state"] not in ("failed", "cancelled"):
                raise LibraryError(f"only failed or cancelled jobs can be retried (this one is {j['state']})")
            db.execute("UPDATE jobs SET state='queued', error=NULL, progress=0, eta=NULL, cancel_requested=0, finished=NULL WHERE id=?", (job_id,))
            db.execute("UPDATE books SET status='queued', error=NULL WHERE id=?", (j["book_id"],))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise

    def requeue_interrupted(self) -> list[int]:
        """After a crash or restart: jobs that were mid-run go back to the queue; chapters whose file is gone are re-queued."""
        db = self.db
        marks = ",".join("?" * len(ACTIVE_STATES))
        ids = [r["id"] for r in db.execute(f"SELECT id FROM jobs WHERE state IN ({marks})", ACTIVE_STATES)]
        if ids:
            db.execute(f"UPDATE jobs SET state='queued', progress=0, eta=NULL WHERE state IN ({marks})", ACTIVE_STATES)
            db.execute("UPDATE books SET status='queued' WHERE id IN (SELECT book_id FROM jobs WHERE state='queued')")
            db.execute("UPDATE chapters SET status='queued' WHERE status='voicing'")
        for ch in db.execute("SELECT book_id, idx, audio_path FROM chapters WHERE status='ready'").fetchall():
            if not ch["audio_path"] or not Path(ch["audio_path"]).is_file():
                db.execute("UPDATE chapters SET status='queued', audio_path=NULL WHERE book_id=? AND idx=?", (ch["book_id"], ch["idx"]))
        return ids

    def active_job_for_source(self, source_path: str) -> dict | None:
        """A queued/running job whose options name this original file (the watcher uses it to avoid enqueuing twice)."""
        for j in self._rows("SELECT * FROM jobs WHERE state IN ('queued','extracting','ocr','cleaning','voicing','assembling')"):
            if j["options"].get("source_path") == source_path:
                return j
        return None

    # ------------------------------------------------------------ playback + bookmarks
    def get_playback(self, book_id: int) -> dict:
        r = self._row(self.db.execute("SELECT * FROM playback WHERE book_id=?", (book_id,)).fetchone())
        return r or {"book_id": book_id, "chapter": 0, "position": 0.0, "updated": None}

    def set_playback(self, book_id: int, chapter: int, position: float) -> dict:
        self.db.execute("INSERT INTO playback(book_id, chapter, position, updated) VALUES (?,?,?,?) "
                        "ON CONFLICT(book_id) DO UPDATE SET chapter=excluded.chapter, position=excluded.position, updated=excluded.updated",
                        (book_id, chapter, position, _now()))
        return self.get_playback(book_id)

    def bookmarks(self, book_id: int) -> list[dict]:
        return self._rows("SELECT * FROM bookmarks WHERE book_id=? ORDER BY chapter, position, id", (book_id,))

    def add_bookmark(self, book_id: int, chapter: int, position: float, note: str = "") -> dict:
        cur = self.db.execute("INSERT INTO bookmarks(book_id, chapter, position, note, created) VALUES (?,?,?,?,?)",
                              (book_id, chapter, position, note, _now()))
        return self._row(self.db.execute("SELECT * FROM bookmarks WHERE id=?", (cur.lastrowid,)).fetchone())

    def delete_bookmark(self, book_id: int, bookmark_id: int) -> bool:
        return self.db.execute("DELETE FROM bookmarks WHERE id=? AND book_id=?", (bookmark_id, book_id)).rowcount > 0


def _split_sql(script: str) -> list[str]:
    """Split a migration script into statements (no triggers/semicolons-in-strings are used, so a plain split is enough)."""
    stmts = []
    for part in script.split(";"):
        lines = [l for l in part.splitlines() if not l.strip().startswith("--")]
        text = "\n".join(lines).strip()
        if text:
            stmts.append(text)
    return stmts
