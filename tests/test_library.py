"""SQLite library + queue: schema, migrations, ordering, resume, cancel, retry, playback, bookmarks."""
import sqlite3

import pytest

from bookbuddy import config
from bookbuddy.library import SCHEMA_VERSION, Library, LibraryError
from bookbuddy.library import db as libdb


@pytest.fixture
def lib(env):
    l = Library(config.get_settings().db_path)
    yield l
    l.close()


def add(lib, name="a.epub", origin="web", **extra):
    return lib.enqueue(title=name, source_name=name, source_hash="h", source_path=f"/x/{name}", voice="af_heart", speed=1.0,
                       fmt="m4b", origin=origin, options=extra)


def test_schema_has_every_table_and_version(lib):
    tables = {r[0] for r in lib.db.execute("select name from sqlite_master where type='table'")}
    assert {"books", "chapters", "jobs", "playback", "bookmarks"} <= tables
    assert lib.schema_version == SCHEMA_VERSION == len(libdb.MIGRATIONS)
    cols = {r[1] for r in lib.db.execute("pragma table_info(books)")}
    assert {"title", "author", "source_name", "source_hash", "cover_path", "created", "status", "voice", "speed", "format",
            "llm_provider", "total_duration"} <= cols
    assert {"para_start", "para_end", "audio_path", "duration", "status"} <= {r[1] for r in lib.db.execute("pragma table_info(chapters)")}
    assert {"origin", "state", "progress", "eta", "error", "created", "started", "finished"} <= {r[1] for r in lib.db.execute("pragma table_info(jobs)")}


def test_reopen_is_idempotent_and_keeps_data(env):
    p = config.get_settings().db_path
    a = Library(p)
    book, _ = add(a)
    a.close()
    b = Library(p)
    assert b.schema_version == SCHEMA_VERSION and b.get_book(book)["title"] == "a.epub"


def test_migrations_upgrade_an_old_database(env, monkeypatch):
    p = config.get_settings().db_path
    a = Library(p)
    book, _ = add(a)
    a.close()
    monkeypatch.setattr(libdb, "MIGRATIONS", [*libdb.MIGRATIONS, "ALTER TABLE books ADD COLUMN rating INTEGER; CREATE INDEX books_rating ON books(rating);"])
    b = Library(p)
    assert b.schema_version == 2
    assert b.db.execute("select rating from books where id=?", (book,)).fetchone()[0] is None      # old rows survive


def test_database_from_the_future_is_refused(env):
    p = config.get_settings().db_path
    Library(p).close()
    c = sqlite3.connect(p)
    c.execute(f"pragma user_version = {SCHEMA_VERSION + 5}")
    c.close()
    with pytest.raises(LibraryError, match="newer"):
        Library(p)


def test_queue_is_fifo_and_claim_is_exclusive(lib):
    ids = [add(lib, f"{i}.epub")[1] for i in range(3)]
    first, second = lib.claim_next(), lib.claim_next()
    assert [first["id"], second["id"]] == ids[:2] and first["state"] == "extracting"
    assert lib.get_book(first["book_id"])["status"] == "processing"
    assert lib.claim_next()["id"] == ids[2] and lib.claim_next() is None


def test_requeue_interrupted_resumes_running_jobs_in_order(lib):
    b1, j1 = add(lib, "1.epub")
    b2, j2 = add(lib, "2.epub")
    lib.claim_next()
    lib.set_job(j1, state="voicing", progress=0.4)
    lib.replace_chapters(b1, [{"index": 0, "title": "One", "para_start": 0, "para_end": 3}, {"index": 1, "title": "Two"}])
    lib.set_chapter(b1, 0, status="voicing")
    assert lib.requeue_interrupted() == [j1]
    assert lib.get_job(j1)["state"] == "queued" and lib.get_job(j1)["progress"] == 0
    assert lib.get_chapter(b1, 0)["status"] == "queued"
    assert lib.claim_next()["id"] == j1                       # interrupted job runs before the later one


def test_requeue_drops_ready_chapters_whose_file_vanished(lib, tmp_path):
    b, _ = add(lib)
    real = tmp_path / "1.m4a"
    real.write_bytes(b"x")
    lib.replace_chapters(b, [{"index": 0, "title": "A"}, {"index": 1, "title": "B"}])
    lib.set_chapter(b, 0, status="ready", audio_path=str(real))
    lib.set_chapter(b, 1, status="ready", audio_path=str(tmp_path / "gone.m4a"))
    lib.requeue_interrupted()
    assert [c["status"] for c in lib.chapters(b)] == ["ready", "queued"]


def test_cancel_queued_running_and_finished(lib):
    _, j1 = add(lib, "1.epub")
    _, j2 = add(lib, "2.epub")
    assert lib.request_cancel(j2) == "cancelled" and lib.get_job(j2)["state"] == "cancelled"
    assert lib.claim_next()["id"] == j1                         # a cancelled job is never claimed
    assert lib.request_cancel(j1) == "cancelling" and lib.cancel_requested(j1)
    lib.set_job(j1, state="done")
    with pytest.raises(LibraryError, match="already"):
        lib.request_cancel(j1)
    with pytest.raises(LibraryError):
        lib.request_cancel(999)


def test_retry_only_failed_or_cancelled(lib):
    b, j = add(lib)
    with pytest.raises(LibraryError, match="only failed or cancelled"):
        lib.retry(j)
    lib.claim_next()
    lib.set_job(j, state="failed", error="boom")
    lib.retry(j)
    job = lib.get_job(j)
    assert job["state"] == "queued" and job["error"] is None and lib.get_book(b)["status"] == "queued"
    assert lib.claim_next()["attempts"] == 2


def test_chapters_keep_paragraph_anchor_ranges_and_survive_replace(lib):
    b, _ = add(lib)
    lib.replace_chapters(b, [{"index": 0, "title": "One", "para_start": 0, "para_end": 11}, {"index": 1, "title": "Two", "para_start": 0, "para_end": 4}])
    lib.set_chapter(b, 0, status="ready", audio_path="/a", duration=12.5)
    lib.replace_chapters(b, [{"index": 0, "title": "One", "para_start": 0, "para_end": 11}, {"index": 1, "title": "Two!", "para_start": 0, "para_end": 4}])
    c0, c1 = lib.chapters(b)
    assert (c0["para_start"], c0["para_end"], c0["status"], c0["duration"]) == (0, 11, "ready", 12.5)
    assert c1["title"] == "Two!" and c1["status"] == "queued"


def test_playback_and_bookmarks_and_cascade(lib):
    b, _ = add(lib)
    assert lib.get_playback(b)["position"] == 0
    lib.set_playback(b, 2, 33.5)
    assert lib.set_playback(b, 3, 1.0)["chapter"] == 3
    m = lib.add_bookmark(b, 1, 10.0, "nice")
    lib.add_bookmark(b, 0, 5.0)
    assert [x["chapter"] for x in lib.bookmarks(b)] == [0, 1]
    assert lib.delete_bookmark(b, m["id"]) and not lib.delete_bookmark(b, m["id"])
    lib.delete_book(b)
    assert lib.bookmarks(b) == [] and lib.get_job(1) is None and lib.get_playback(b)["updated"] is None


def test_two_connections_share_the_queue(env):
    p = config.get_settings().db_path
    a, c = Library(p), Library(p)                # e.g. the server and a CLI process
    add(a)
    assert c.claim_next() is not None and a.claim_next() is None


def test_bad_origin_and_fields_rejected(lib):
    with pytest.raises(LibraryError):
        add(lib, origin="telepathy")
    b, _ = add(lib)
    with pytest.raises(LibraryError):
        lib.set_book(b, id=7)
