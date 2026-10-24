"""bookbuddy listen|watch|serve|queue

    bookbuddy listen book.epub [--voice af_heart] [--speed 1.0] [--format m4b|mp3] [--out DIR]
    bookbuddy watch [--inbox DIR] [--out DIR]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, config


def _add_listen(sub) -> None:
    st = config.get_settings()
    ap = sub.add_parser("listen", help="convert a book to an audiobook now",
                        description="Turn a book or document (run `bookbuddy formats` for the list) into a chaptered audiobook.")
    ap.add_argument("book", type=Path, help="path to the book file")
    ap.add_argument("--voice", default=st.voice, help="Kokoro voice (default: %(default)s)")
    ap.add_argument("--speed", type=float, default=st.speed, help="speaking speed (default: %(default)s)")
    ap.add_argument("--format", dest="fmt", choices=["m4b", "mp3"], default=st.format, help="output format (default: %(default)s)")
    ap.add_argument("--out", type=Path, default=None, help=f"output directory (default: {st.output_dir})")
    ap.add_argument("--max-chapters", type=int, default=None, help="only convert the first N chapters (testing)")
    ap.add_argument("--keep-work", action="store_true", help="keep the work folder after success")
    ap.add_argument("--llm", default=None, metavar="PROVIDER",
                    help=f"PDF cleanup provider: auto | none | an installed provider name; naming one turns the cleanup on for this run "
                         f"(default: {st.llm_provider}, used only when [llm] enabled = true)")
    ap.add_argument("--local-only", action="store_true", help="same as --llm none (PDF text never leaves the machine)")
    ap.add_argument("--ocr-lang", default=None, metavar="LANG", help=f"OCR language for scanned input (default: {st.ocr_lang})")
    ap.add_argument("--ocr-backend", default=None, choices=list(config.OCR_BACKENDS),
                    help=f"OCR backend (default: {st.ocr_backend})")
    ap.add_argument("--ocr-workers", type=int, default=None, help="parallel OCR processes (default: auto)")
    ap.add_argument("--device", choices=["cuda", "cpu"], default=st.device)
    ap.add_argument("--now", action="store_true", help="convert in the foreground even when a server is running")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.set_defaults(func=_listen)


def build_parser() -> argparse.ArgumentParser:
    from .watcher import add_watch_arguments

    ap = argparse.ArgumentParser(prog="bookbuddy", description="Turn books and documents into audiobooks, locally.")
    ap.add_argument("--version", action="version", version=f"bookbuddy {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="{listen,watch,formats,serve,queue,keys}")
    sub.required = True
    _add_listen(sub)
    w = sub.add_parser("watch", help="watch a folder and convert every book dropped into it")
    add_watch_arguments(w)
    f = sub.add_parser("formats", help="list supported book formats and which work right now")
    f.add_argument("--json", action="store_true", help="machine-readable output")
    f.set_defaults(func=_formats)
    _add_serve(sub)
    _add_queue(sub)
    _add_keys(sub)
    return ap


KEY_PROVIDERS = ("anthropic", "openai")


def _add_keys(sub) -> None:
    k = sub.add_parser("keys", help="save, remove or show the API key for the optional AI text cleanup",
                       description="Saving a key here turns the AI cleanup pass on (it is off by default and a key in the "
                                   "environment alone never enables it). The key is read from a hidden prompt or from stdin, "
                                   "never from the command line.")
    ks = k.add_subparsers(dest="keys_cmd", metavar="{set,remove,status}")
    ks.required = True
    s = ks.add_parser("set", help="save a key (turns the AI cleanup on)")
    s.add_argument("provider", choices=KEY_PROVIDERS)
    r = ks.add_parser("remove", help="remove a saved key (the cleanup stays on or off as set)")
    r.add_argument("provider", choices=KEY_PROVIDERS)
    ks.add_parser("status", help="show whether the cleanup is on and which keys exist (last 4 characters only)")
    k.set_defaults(func=_keys)


def _keys(args) -> int:
    import getpass
    import re

    from . import llm_providers
    from .config_edit import update_config
    from .logs import register_secret

    cmd = args.keys_cmd
    if cmd == "status":
        st = config.get_settings()
        print(f"AI cleanup: {'on' if st.llm_enabled else 'off'}")
        for p in KEY_PROVIDERS:
            cls = llm_providers.registry.get(p)
            key = cls.find_key()
            cfg = (st.llm_providers.get(p, {}).get("api_key") or "").strip()
            print(f"  {p}: " + (f"set ...{key[-4:]} ({'saved' if cfg else 'environment'})" if key else "not set"))
        return 0
    if cmd == "remove":
        update_config(config.config_path(), {(f"llm.{args.provider}", "api_key"): None})
        print(f"{args.provider}: saved key removed")
        return 0
    key = (getpass.getpass(f"{args.provider} API key: ") if sys.stdin.isatty() else sys.stdin.readline()).strip()
    if not 8 <= len(key) <= 300 or re.search(r"\s", key):
        print("error: that does not look like an API key", file=sys.stderr)
        return 2
    register_secret(key)
    update_config(config.config_path(), {(f"llm.{args.provider}", "api_key"): key, ("llm", "enabled"): True})
    print(f"{args.provider}: key saved; AI cleanup is on (`bookbuddy keys status`, or --local-only per run, to check or avoid it)")
    return 0


def _add_serve(sub) -> None:
    st = config.get_settings()
    p = sub.add_parser("serve", help="web UI, HTTP API and the job queue worker",
                       description="Run the web page, the HTTP API and the single worker that voices queued books.")
    p.add_argument("--host", default=None, help=f"address to listen on (default: {st.web_host}); anything but 127.0.0.1 needs --token")
    p.add_argument("--port", type=int, default=None, help=f"port (default: {st.web_port})")
    p.add_argument("--token", default=None, help="access token (or [web] token in config.toml, or BOOKBUDDY_TOKEN)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=_serve)


def _add_queue(sub) -> None:
    q = sub.add_parser("queue", help="list, cancel or retry queued jobs",
                       description="Inspect and manage the job queue (works with or without a running server).")
    qs = q.add_subparsers(dest="queue_cmd", metavar="{list,cancel,retry}")
    for name, hlp in (("list", "show jobs (default)"), ("cancel", "cancel a queued or running job"), ("retry", "re-queue a failed or cancelled job")):
        sp = qs.add_parser(name, help=hlp)
        if name != "list":
            sp.add_argument("job", type=int, help="job id")
        else:
            sp.add_argument("--all", action="store_true", help="include finished jobs")
            sp.add_argument("--json", action="store_true")
    q.add_argument("--all", action="store_true", help="include finished jobs")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=_queue)


def _serve(args) -> int:
    import uvicorn

    from .library import Library
    from .logs import setup_logging
    from .server import ServerConfigError, check_bind, create_app
    from .worker import AlreadyRunning, EventBus, ServerLock, Worker

    setup_logging(args.verbose)
    st = config.get_settings()
    host, port = args.host or st.web_host, args.port or st.web_port
    token = args.token or st.web_token
    try:
        check_bind(host, token)
    except ServerConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    lock = ServerLock()
    try:
        lock.acquire({"host": host, "port": port})
    except AlreadyRunning as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        lib = Library(st.db_path)
        bus = EventBus()
        worker = Worker(lib, bus)
        app = create_app(library=lib, worker=worker, token=token, host=host)
        shown = "localhost" if host in ("127.0.0.1", "::1") else host
        print(f"Book Buddy: http://{shown}:{port}  (token {'required' if token else 'not needed on localhost'})", flush=True)
        # open event streams must not hold up Ctrl-C / systemctl stop: connections are cut after 5 s
        uvicorn.run(app, host=host, port=port, log_level="warning" if not args.verbose else "info", timeout_graceful_shutdown=5)
    finally:
        lock.release()
    return 0


def _fmt_job(j: dict) -> str:
    pct = f"{int(j['progress'] * 100):>3}%" if j["state"] not in ("queued", "done") else "    "
    return f"{j['id']:>4}  {j['state']:<10} {pct}  {j['origin']:<5} {j['title'][:44]}" + (f"  -- {j['error'][:80]}" if j.get("error") else "")


def _queue(args) -> int:
    import json as _json

    from .library import Library, LibraryError

    lib = Library(config.get_settings().db_path)
    cmd = getattr(args, "queue_cmd", None) or "list"
    if cmd in ("cancel", "retry"):
        try:
            r = lib.request_cancel(args.job) if cmd == "cancel" else lib.retry(args.job)
        except LibraryError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"job {args.job}: {r or 'queued'}")
        return 0
    states = None if getattr(args, "all", False) else ("queued", "extracting", "ocr", "cleaning", "voicing", "assembling")
    jobs = lib.list_jobs(states)
    if getattr(args, "json", False):
        print(_json.dumps(jobs, indent=1, default=str))
    elif not jobs:
        print("queue is empty" if states else "no jobs")
    else:
        print(f"{'id':>4}  {'state':<10} {'done':>4}  {'from':<5} book")
        for j in reversed(jobs):
            print(_fmt_job(j))
    return 0


def _enqueue_listen(args) -> int:
    from . import intake
    from .library import Library

    try:
        staged, sha, _ = intake.stage_path(args.book)
    except intake.IntakeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    st = config.get_settings()
    extra = {"out_dir": str(args.out or st.output_dir), "max_chapters": args.max_chapters, "ocr_lang": args.ocr_lang,
             "ocr_backend": args.ocr_backend, "ocr_workers": args.ocr_workers}
    book_id, job_id = intake.enqueue_file(Library(st.db_path), staged, sha, origin="cli", voice=args.voice, speed=args.speed,
                                          fmt=args.fmt, llm="none" if args.local_only else args.llm, extra=extra,
                                          original_name=args.book.name)
    print(f"queued as job {job_id} (book {book_id}); the running server will voice it. `bookbuddy queue` shows progress; "
          f"the audiobook lands in {extra['out_dir']}. Use --now to convert in the foreground instead.")
    return 0


def _formats(args) -> int:
    import json
    from .extractors import list_formats

    rows = list_formats()
    if args.json:
        print(json.dumps([r.to_dict() for r in rows], indent=1))
        return 0
    w = max(len(r.name) for r in rows)
    print(f"{'Format':<{w}}  {'Extensions':<34}  {'Status':<11} How")
    for r in rows:
        status = "available" if r.available else "UNAVAILABLE"
        extra = f" [needs {r.needs}]" if r.needs and not r.available else ""
        print(f"{r.name:<{w}}  {', '.join(r.extensions):<34}  {status:<11} {r.how}{extra}")
        if r.note:
            print(f"{'':<{w}}    -> {r.note}")
    from .ocr import describe
    d = describe()
    print()
    if not d["tesseract"]:
        print("OCR: unavailable (Tesseract is not installed)")
    else:
        pdfb = d["pdf_backend"]
        alt = f" (setting: {d['setting']})"
        print(f"OCR: PDFs via {pdfb}{alt}; images, CBZ, CBR and DjVu via Tesseract {d['tesseract_version']}; "
              f"language {d['lang']}; {d['workers']} workers")
        if d["ocrmypdf_problem"]:
            print(f"     ocrmypdf not in use: {d['ocrmypdf_problem']}")
    return 0


def _listen(args) -> int:
    from .logs import setup_logging
    from .pipeline import Options, convert, fmt_duration

    setup_logging(args.verbose)
    if not args.book.exists():
        print(f"error: {args.book} not found", file=sys.stderr)
        return 2
    if not args.now and args.book.is_file():
        from .worker import server_running
        if server_running():
            return _enqueue_listen(args)
    opts = Options(voice=args.voice, speed=args.speed, fmt=args.fmt, out_dir=args.out,
                   max_chapters=args.max_chapters, keep_work=args.keep_work, device=args.device,
                   llm="none" if args.local_only else args.llm,
                   ocr_lang=args.ocr_lang, ocr_backend=args.ocr_backend, ocr_workers=args.ocr_workers)
    try:
        res = convert(args.book, opts)
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{res.path}  ({fmt_duration(res.duration)}, {res.size / 1e6:.1f} MB, {len(res.chapters)} chapters, "
          f"{len(res.qc_warnings)} QC warnings, LLM cleanup: {res.llm})")
    for w in res.qc_warnings[:10]:
        print("  QC:", w)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
