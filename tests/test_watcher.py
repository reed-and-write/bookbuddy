import os
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from bookbuddy.jobs import process_file
from bookbuddy.notify import Notifier
from bookbuddy.pipeline import Options, PipelineError
from bookbuddy.watcher import SettleTracker, has_temp_sibling, is_candidate, run_once, scan


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


def fake_result(tmp_path):
    return SimpleNamespace(title="T", author="A", duration=3725, size=5_000_000, path=tmp_path / "T.m4b")


def test_settle_requires_stable_size(tmp_path):
    f = tmp_path / "b.epub"
    f.write_bytes(b"aaa")
    clk = Clock()
    tr = SettleTracker(10, clk)
    assert not tr.check(f)             # first sighting
    clk.t = 5
    assert not tr.check(f)             # stable but < 10s
    f.write_bytes(b"aaaaaa")           # still syncing: size changed
    clk.t = 12
    assert not tr.check(f)             # clock restarted
    clk.t = 21
    assert not tr.check(f)
    clk.t = 22.5
    assert tr.check(f)                 # unchanged for >= 10 s


def test_settle_blocked_by_temp_sibling(tmp_path):
    f = tmp_path / "book.epub"
    f.write_bytes(b"data")
    tmpf = tmp_path / ".book.epub.dropbox.tmp"
    tmpf.write_bytes(b"x")
    clk = Clock()
    tr = SettleTracker(10, clk)
    tr.check(f)
    clk.t = 100
    assert has_temp_sibling(f)
    assert not tr.check(f)
    tmpf.unlink()
    clk.t = 105
    assert not tr.check(f)   # clock was reset while the temp file existed
    clk.t = 111
    assert tr.check(f)


def test_empty_file_never_settles(tmp_path):
    f = tmp_path / "b.epub"
    f.write_bytes(b"")
    clk = Clock()
    tr = SettleTracker(1, clk)
    tr.check(f); clk.t = 50
    assert not tr.check(f)


def test_candidates(tmp_path):
    for n in ["a.epub", "b.PDF", "c.xyz", ".d.epub", "e.epub.tmp", "f.epub.part", "~$g.epub"]:
        (tmp_path / n).write_bytes(b"x")
    (tmp_path / "done").mkdir()
    assert sorted(p.name for p in scan(tmp_path)) == ["a.epub", "b.PDF"]
    assert not is_candidate(tmp_path / "done")


def test_success_moves_to_done_and_never_deletes(tmp_path):
    inbox = tmp_path / "inbox"; inbox.mkdir()
    src = inbox / "b.epub"; src.write_bytes(b"epub")
    n = Notifier(tmp_path / "outbox", dry_run=True)
    ok = process_file(src, inbox, Options(), n, convert_fn=lambda s, o: fake_result(tmp_path))
    assert ok and not src.exists() and (inbox / "done" / "b.epub").read_bytes() == b"epub"
    status = (tmp_path / "outbox" / "STATUS.txt").read_text()
    assert "START | b.epub" in status and "OK | T | A | 1h02m | 5.0 MB" in status


def test_failure_moves_to_failed_with_error_note(tmp_path):
    inbox = tmp_path / "inbox"; inbox.mkdir()
    src = inbox / "b.pdf"; src.write_bytes(b"%PDF")
    def boom(s, o): raise PipelineError("PDF not supported yet (phase 3)")
    ok = process_file(src, inbox, Options(), Notifier(tmp_path / "outbox", dry_run=True), convert_fn=boom)
    assert not ok and not src.exists()
    assert (inbox / "failed" / "b.pdf").read_bytes() == b"%PDF"
    note = (inbox / "failed" / "b.pdf.error.txt").read_text()
    assert "PDF not supported yet" in note and "Traceback" in note
    assert "FAIL | b.pdf" in (tmp_path / "outbox" / "STATUS.txt").read_text()


def test_name_collision_keeps_both(tmp_path):
    inbox = tmp_path / "inbox"; (inbox / "done").mkdir(parents=True)
    (inbox / "done" / "b.epub").write_bytes(b"old")
    src = inbox / "b.epub"; src.write_bytes(b"new")
    process_file(src, inbox, Options(), Notifier(tmp_path / "o", dry_run=True), convert_fn=lambda s, o: fake_result(tmp_path))
    names = sorted(p.name for p in (inbox / "done").iterdir())
    assert len(names) == 2 and (inbox / "done" / "b.epub").read_bytes() == b"old"


def test_notification_failure_never_fails_job(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"; inbox.mkdir()
    src = inbox / "b.epub"; src.write_bytes(b"x")
    n = Notifier(tmp_path / "o", dry_run=False)
    def bad(*a, **k): raise OSError("no notify-send")
    monkeypatch.setattr("bookbuddy.notifiers.desktop.subprocess.run", bad)
    assert process_file(src, inbox, Options(), n, convert_fn=lambda s, o: fake_result(tmp_path))
    assert (inbox / "done" / "b.epub").exists()
    assert "OK | T" in (tmp_path / "o" / "STATUS.txt").read_text()


def test_dry_run_prints_notification_and_does_not_call_subprocess(tmp_path, monkeypatch, capsys):
    called = []
    monkeypatch.setattr("bookbuddy.notifiers.desktop.subprocess.run", lambda *a, **k: called.append(a))
    n = Notifier(tmp_path, dry_run=True)
    n.finished("b.epub", fake_result(tmp_path))
    out = capsys.readouterr().out
    assert "T by A is ready (1h02m, 5.0 MB)" in out and "notify-send" in out
    assert not called


def test_run_once_processes_one_book_at_a_time_and_picks_up_existing(tmp_path):
    inbox = tmp_path / "inbox"; inbox.mkdir()
    for n in ("a.epub", "b.epub"):
        (inbox / n).write_bytes(b"x" * 10)
        os.utime(inbox / n, (1, 1 if n == "a.epub" else 2))
    clk = Clock(); tr = SettleTracker(10, clk)
    notifier = Notifier(tmp_path / "o", dry_run=True)
    seen = []
    import bookbuddy.watcher as w
    monkey = lambda s, o: (seen.append(s.name), fake_result(tmp_path))[1]
    orig = w.process_file
    w.process_file = lambda p, i, o, n: orig(p, i, o, n, convert_fn=monkey)
    try:
        assert run_once(inbox, tr, Options(), notifier, set()) == 0    # first sighting, nothing settled
        clk.t = 11
        assert run_once(inbox, tr, Options(), notifier, set()) == 1
        assert seen == ["a.epub"]                                     # oldest first, only one per call
        assert run_once(inbox, tr, Options(), notifier, set()) == 1
        assert seen == ["a.epub", "b.epub"]
    finally:
        w.process_file = orig
    assert sorted(p.name for p in (inbox / "done").iterdir()) == ["a.epub", "b.epub"]


def test_notifier_llm_plan_message(tmp_path, capsys):
    n = Notifier(tmp_path / "o", dry_run=True)
    n.llm_plan(30, 72, 540, "beta")
    n.llm_plan(0, 72, 0, "beta")            # nothing to send: silent
    out = capsys.readouterr().out
    assert "30 of 72" in out and "9 min" in out and out.count("Audiobook plan") <= 3
