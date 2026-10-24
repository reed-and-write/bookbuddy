"""CLI: `queue` list/cancel/retry, `listen` and `watch` enqueue into a running server and fall back to today's behaviour otherwise."""
import json
from pathlib import Path

import pytest

from bookbuddy import config
from bookbuddy.cli import main
from bookbuddy.library import Library
from bookbuddy.worker import ServerLock
from helpers import SAMPLE_CHAPTERS, make_epub


@pytest.fixture
def book(tmp_path):
    return make_epub(tmp_path / "cli book.epub", SAMPLE_CHAPTERS)


@pytest.fixture
def server_lock(env):
    lock = ServerLock()
    lock.acquire({"host": "127.0.0.1", "port": 1})        # "a server is running" for this data dir
    yield lock
    lock.release()


def lib():
    return Library(config.get_settings().db_path)


def test_listen_enqueues_when_a_server_is_running(server_lock, book, tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["listen", str(book), "--voice", "bf_emma", "--speed", "1.2", "--out", str(out), "--local-only", "--max-chapters", "2"]) == 0
    assert "queued as job 1" in capsys.readouterr().out
    j = lib().get_job(1)
    b = lib().get_book(j["book_id"])
    assert (j["origin"], j["state"], b["voice"], b["speed"]) == ("cli", "queued", "bf_emma", 1.2)
    assert j["options"]["out_dir"] == str(out) and j["options"]["llm"] == "none" and j["options"]["max_chapters"] == 2
    assert Path(b["source_path"]).read_bytes() == book.read_bytes() and Path(b["source_path"]) != book
    assert not out.exists()                                           # nothing was converted in the foreground


def test_listen_now_ignores_the_server(server_lock, book, tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["listen", str(book), "--now", "--out", str(out), "--local-only"]) == 0
    assert list(out.glob("*.m4b")) and lib().list_jobs() == []


def test_listen_without_a_server_behaves_as_before(env, book, tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["listen", str(book), "--out", str(out), "--local-only"]) == 0
    assert list(out.glob("*.m4b")) and lib().list_jobs() == []


def test_a_killed_server_leaves_no_stale_lock(env):
    import os
    import signal
    import subprocess
    import sys
    from bookbuddy.worker import server_running
    code = "import time; from bookbuddy.worker import ServerLock; ServerLock().acquire({'port': 1}); print('locked', flush=True); time.sleep(60)"
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent)))
    try:
        assert p.stdout.readline().strip() == "locked"
        assert server_running()["port"] == 1                          # held by another process
        with pytest.raises(Exception):
            ServerLock().acquire()
        p.send_signal(signal.SIGKILL)
        p.wait(10)
        assert server_running() is None                               # the kernel dropped the lock with the process
        ServerLock().acquire()
    finally:
        p.kill()


def test_queue_list_cancel_retry(server_lock, book, tmp_path, capsys):
    main(["listen", str(book), "--out", str(tmp_path / "o")])
    main(["listen", str(book), "--out", str(tmp_path / "o")])
    capsys.readouterr()
    assert main(["queue"]) == 0
    out = capsys.readouterr().out
    assert "queued" in out and "cli book" in out
    assert main(["queue", "cancel", "2"]) == 0
    assert lib().get_job(2)["state"] == "cancelled"
    assert main(["queue", "cancel", "2"]) == 1                          # already finished
    assert main(["queue", "retry", "1"]) == 1                           # queued jobs cannot be retried
    assert main(["queue", "retry", "2"]) == 0 and lib().get_job(2)["state"] == "queued"
    capsys.readouterr()
    main(["queue", "list", "--json"])
    assert [j["id"] for j in json.loads(capsys.readouterr().out)] == [2, 1]
    assert main(["queue", "cancel", "99"]) == 1


def test_watch_enqueues_into_the_server_and_leaves_the_source_until_it_is_done(server_lock, book, tmp_path):
    inbox, outbox = tmp_path / "inbox", tmp_path / "outbox"
    inbox.mkdir()
    dropped = inbox / "dropped.epub"
    dropped.write_bytes(book.read_bytes())
    assert main(["watch", "--inbox", str(inbox), "--out", str(outbox), "--once", "--settle", "0", "--poll", "0.05", "--dry-run", "--local-only"]) == 0
    jobs = lib().list_jobs()
    assert len(jobs) == 1 and jobs[0]["origin"] == "watch" and jobs[0]["state"] == "queued"
    o = jobs[0]["options"]
    assert o["source_path"] == str(dropped) and o["out_dir"] == str(outbox) and o["inbox"] == str(inbox)
    assert dropped.exists()                                            # moved to done/ only after the server finishes it
    assert lib().active_job_for_source(str(dropped)) is not None       # so a second pass will not queue it again
    assert main(["watch", "--inbox", str(inbox), "--out", str(outbox), "--once", "--settle", "0", "--poll", "0.05", "--dry-run"]) == 0
    assert len(lib().list_jobs()) == 1


def test_watch_without_a_server_converts_in_place_as_before(env, book, tmp_path):
    inbox, outbox = tmp_path / "inbox", tmp_path / "outbox"
    inbox.mkdir()
    (inbox / "dropped.epub").write_bytes(book.read_bytes())
    assert main(["watch", "--inbox", str(inbox), "--out", str(outbox), "--once", "--settle", "0", "--poll", "0.05", "--dry-run", "--local-only"]) == 0
    assert list(outbox.glob("*.m4b")) and (inbox / "done" / "dropped.epub").exists()


def test_server_unit_and_installer_exist_and_are_generic():
    root = Path(__file__).resolve().parent.parent / "systemd"
    unit = (root / "bookbuddy-server.service").read_text()
    assert "ExecStart=@BOOKBUDDY@ serve" in unit and "/home/" not in unit
    inst = root / "install-server.sh"
    assert inst.stat().st_mode & 0o111 and "bookbuddy-server.service" in inst.read_text() and "sudo" not in inst.read_text().replace("no sudo", "")


def test_serve_subprocess_stops_cleanly_with_an_open_event_stream(env, book):
    """Real `bookbuddy serve` process: lock held while it runs, token enforced, SIGINT exits promptly even with an SSE client attached."""
    import os
    import signal
    import socket
    import subprocess
    import sys
    import time
    import httpx
    from bookbuddy.worker import server_running
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    e = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent), BOOKBUDDY_ENGINE="fake")
    p = subprocess.Popen([sys.executable, "-m", "bookbuddy.cli", "serve", "--port", str(port), "--token", "tok-abcdefgh"], env=e,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                httpx.get(f"http://127.0.0.1:{port}/api/session", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        assert server_running()["port"] == port
        assert main(["serve", "--port", str(port + 1)]) == 1                     # a second server for the same data dir is refused
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/events", headers={"Authorization": "Bearer tok-abcdefgh"}, timeout=10) as r:
            next(r.iter_lines())
            p.send_signal(signal.SIGINT)
            assert p.wait(30) == 0
    finally:
        p.kill()
    assert server_running() is None
