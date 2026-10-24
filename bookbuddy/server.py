"""FastAPI app: library, queue, progressive audio, SSE events, settings and keys. Served by `bookbuddy serve`.

Security (architecture Q4): binds 127.0.0.1 by default; any other host needs a token, checked in constant time on every
API call (header `Authorization: Bearer <token>` or the `bb_token` cookie set by POST /api/login). Only `/` (the page),
`/api/session` and `/api/login` are open, so the page can ask for the token. CORS is off. On a loopback server without a
token the Host header must name the loopback address (stops DNS-rebinding) and state-changing requests must be
same-origin (stops a web page you visit from posting to your local server).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shutil
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import __version__, config, intake, llm_providers, preview  # noqa: F401  (llm_providers registers anthropic/openai)
from .config_edit import update_config
from .extractors import list_formats
from .extractors.drm import DRMError
from .library import ACTIVE_STATES, Library, LibraryError
from .logs import register_secret
from .registry import llm_providers as provider_registry
from .voices import SAMPLE_TEXT, VOICES, list_voices
from .worker import EventBus, Worker, _public_job

log = logging.getLogger("bookbuddy.server")

WEB_DIR = Path(__file__).parent / "web"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
KEY_PROVIDERS = ("anthropic", "openai")
COOKIE = "bb_token"
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
OPEN_PATHS = {"/api/session", "/api/login"}
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".woff2": "font/woff2",
                ".webp": "image/webp", ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8", ".png": "image/png"}


def is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS or host.startswith("127.")


class ServerConfigError(ValueError):
    pass


def check_bind(host: str, token: str | None) -> None:
    """Refuse a non-loopback bind without a token (called before the server starts)."""
    if not is_loopback(host) and not token:
        raise ServerConfigError(f"refusing to listen on {host} without a token: pass --token SECRET (or set [web] token / "
                                "BOOKBUDDY_TOKEN). Only 127.0.0.1 may run without one.")


def _host_name(header: str) -> str:
    h = header.strip().lower()
    if h.startswith("["):
        return h.split("]")[0] + "]"
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def public_book(b: dict, chapters: list[dict] | None = None, lib: Library | None = None) -> dict:
    out = {k: b[k] for k in ("id", "title", "author", "source_name", "created", "status", "voice", "speed", "format",
                             "llm_provider", "total_duration", "error")}
    out["has_cover"] = bool(b.get("cover_path") and Path(b["cover_path"]).is_file())
    op = b.get("output_path")
    out["has_download"] = bool(op and Path(op).exists())
    out["download_kind"] = ("m4b" if op and Path(op).is_file() else "mp3-folder" if op and Path(op).is_dir() else None)
    for k in ("chapters_ready", "chapters_total", "listened"):
        if k in b:
            out[k] = b[k]
    if b.get("job"):
        out["job"] = _public_job(b["job"])
    if chapters is not None:
        out["chapters"] = [{"index": c["idx"], "title": c["title"], "para_start": c["para_start"], "para_end": c["para_end"],
                            "duration": c["duration"], "status": c["status"]} for c in chapters]
        out["chapters_ready"] = sum(1 for c in chapters if c["status"] == "ready")
        out["chapters_total"] = len(chapters)
    return out


def key_status(provider: str) -> dict:
    cls = provider_registry.get(provider)
    cfg = config.get_settings().llm_providers.get(provider, {})
    cfg_key = (cfg.get("api_key") or "").strip()
    key = cls.find_key()
    if not key:
        return {"set": False}
    return {"set": True, "last4": key[-4:], "source": "config" if cfg_key else "env"}


def create_app(*, library: Library | None = None, worker: Worker | None = None, token: str | None = None,
               engine_factory: Callable | None = None, start_worker: bool = True, host: str = "127.0.0.1") -> FastAPI:
    st0 = config.get_settings()
    token = token if token is not None else st0.web_token
    check_bind(host, token)
    if token:
        register_secret(token)
    lib = library or Library(st0.db_path)
    bus = worker.bus if worker else EventBus()
    if worker is None:
        worker = Worker(lib, bus, engine_factory=engine_factory)
    loopback_only = is_loopback(host) and not token

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_worker:
            worker.start()

        async def sweep():                                   # unclaimed inspected uploads expire after 24 h
            while True:
                try:
                    await run_in_threadpool(preview.cleanup_expired)
                except Exception:  # noqa: BLE001 - housekeeping must never take the server down
                    log.exception("upload cleanup failed")
                await asyncio.sleep(3600)

        sweeper = asyncio.create_task(sweep())
        yield
        sweeper.cancel()
        if start_worker:
            await run_in_threadpool(worker.stop)

    app = FastAPI(title="Book Buddy", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.library, app.state.worker, app.state.bus, app.state.token = lib, worker, bus, token

    # ------------------------------------------------------------ security
    def authed(request: Request) -> bool:
        if not token:
            return True
        given = request.cookies.get(COOKIE, "")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        return bool(given) and secrets.compare_digest(given.encode(), token.encode())

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if loopback_only and _host_name(request.headers.get("host", "")) not in LOOPBACK_HOSTS:
            return JSONResponse({"detail": "unexpected Host header"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
            if origin.split("://", 1)[-1] != request.headers.get("host", ""):
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        if path.startswith("/api/") and path not in OPEN_PATHS and not authed(request):
            return JSONResponse({"detail": "token required"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Content-Security-Policy", CSP)
        if path.startswith("/api/") and "cache-control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/session")
    async def session(request: Request):
        return {"auth_required": bool(token), "authenticated": authed(request), "version": __version__}

    @app.post("/api/login")
    async def login(request: Request, body: dict = Body(...)):
        given = str(body.get("token", ""))
        if not token or not secrets.compare_digest(given.encode(), token.encode()):
            await asyncio.sleep(0.5)                                         # slow down guessing
            raise HTTPException(401, "wrong token")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/", max_age=60 * 60 * 24 * 30)
        return resp

    @app.post("/api/logout")
    async def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # ------------------------------------------------------------ page
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEB_DIR / "index.html", media_type="text/html", headers={"Cache-Control": "no-store"})

    def static_file(folder: Path, name: str, cache: str) -> FileResponse:
        """One file from the packaged web folder. Only plain names that exist as files there are served."""
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name) or name.startswith("."):
            raise HTTPException(404, "not found")
        path = folder / name
        if not path.is_file():
            raise HTTPException(404, "not found")
        media = STATIC_TYPES.get(path.suffix.lower())
        if media is None:
            raise HTTPException(404, "not found")
        return FileResponse(path, media_type=media, headers={"Cache-Control": cache})

    @app.get("/app.css", include_in_schema=False)
    async def app_css():
        return static_file(WEB_DIR, "app.css", "no-cache")

    @app.get("/app.js", include_in_schema=False)
    async def app_js():
        return static_file(WEB_DIR, "app.js", "no-cache")

    @app.get("/fonts/{name}", include_in_schema=False)
    async def web_font(name: str):
        return static_file(WEB_DIR / "fonts", name, "max-age=86400")

    @app.get("/art/{name}", include_in_schema=False)
    async def web_art(name: str):
        return static_file(WEB_DIR / "art", name, "max-age=86400")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    # ------------------------------------------------------------ helpers
    def book_or_404(book_id: int, chapters: bool = False) -> dict:
        b = lib.get_book(book_id, chapters=chapters)
        if b is None:
            raise HTTPException(404, "no such book")
        return b

    def ok_voice(v: str) -> str:
        if v not in VOICES:
            raise HTTPException(400, f"unknown voice {v!r}")
        return v

    def ok_speed(s: float) -> float:
        if not 0.5 <= s <= 2.0:
            raise HTTPException(400, "speed must be between 0.5 and 2.0")
        return round(float(s), 2)

    def ok_format(f: str) -> str:
        if f not in ("m4b", "mp3"):
            raise HTTPException(400, "format must be m4b or mp3")
        return f

    # ------------------------------------------------------------ books
    def read_options(voice, speed, format, llm) -> tuple[str, float, str, str]:
        st = config.get_settings()
        voice = ok_voice(voice or st.voice)
        speed = ok_speed(speed if speed is not None else st.speed)
        fmt = ok_format(format or st.format)
        mode = (llm or "auto").lower()
        if mode not in ("on", "off", "auto", "none", "true", "false", "1", "0"):
            raise HTTPException(400, "llm must be on or off")
        return voice, speed, fmt, ("none" if mode in ("off", "none", "false", "0") else "auto")

    async def stage_upload(request: Request, file: UploadFile) -> tuple[Path, str, int]:
        st = config.get_settings()
        limit = st.web_max_upload_mb * (1 << 20)
        clen = request.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > limit + (1 << 20):
            raise HTTPException(413, f"file is larger than the {st.web_max_upload_mb} MB upload limit")
        try:
            return await run_in_threadpool(intake.stage_stream, file.file, file.filename or "book", limit)
        except intake.TooLarge as e:
            raise HTTPException(413, str(e)) from None
        except intake.IntakeError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/books", status_code=201)
    async def upload_book(request: Request, file: UploadFile | None = File(None), upload_id: str | None = Form(None),
                          voice: str | None = Form(None), speed: float | None = Form(None), format: str | None = Form(None),
                          llm: str | None = Form(None)):
        """Queue a book: either a new `file`, or an `upload_id` from POST /api/inspect (the staged file is used, once)."""
        if file is None and upload_id is None:
            raise HTTPException(422, "send a file or an upload_id")
        if file is not None and upload_id is not None:
            raise HTTPException(400, "send either a file or an upload_id, not both")
        voice, speed, fmt, llm_choice = read_options(voice, speed, format, llm)
        if upload_id is not None:
            try:
                staged, meta = await run_in_threadpool(preview.claim, upload_id)
            except preview.UploadExpired as e:
                raise HTTPException(410, str(e)) from None
            except preview.UploadGone:
                raise HTTPException(404, "no such upload (it may have been queued or discarded already)") from None
            sha, shown_title = meta["sha"], meta.get("title")
        else:
            staged, sha, _ = await stage_upload(request, file)
            shown_title = None
        book_id, job_id = intake.enqueue_file(lib, staged, sha, origin="web", voice=voice, speed=speed, fmt=fmt, llm=llm_choice,
                                              title=shown_title)
        worker.wake()
        bus.publish("book", book_id=book_id, status="queued")
        bus.publish("job", job=_public_job(lib.get_job(job_id)))
        return {"book_id": book_id, "job_id": job_id}

    # ------------------------------------------------------------ inspect (stage + metadata, no job)
    @app.post("/api/inspect", status_code=201)
    async def inspect_book(request: Request, file: UploadFile = File(...), speed: float | None = Form(None)):
        sp = ok_speed(speed if speed is not None else config.get_settings().speed)
        staged, sha, size = await stage_upload(request, file)
        try:
            return await run_in_threadpool(preview.inspect_upload, staged, sha, size, sp)
        except (DRMError, ValueError) as e:
            raise HTTPException(400, str(e) if isinstance(e, DRMError) else f"Book Buddy could not read this file: {e}") from None
        except Exception as e:  # noqa: BLE001 - a broken file is the user's problem to see, not a 500
            log.warning("inspect failed for %s: %s", staged.name, e)
            raise HTTPException(400, "Book Buddy could not read this file.") from None

    @app.get("/api/inspect/{upload_id}/cover")
    async def inspect_cover(upload_id: str):
        try:
            p = await run_in_threadpool(preview.cover_path, upload_id)
        except preview.UploadExpired as e:
            raise HTTPException(410, str(e)) from None
        except preview.UploadGone:
            raise HTTPException(404, "no such upload") from None
        if p is None:
            raise HTTPException(404, "no cover")
        return FileResponse(p, media_type={".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}.get(p.suffix, "image/jpeg"),
                            headers={"Cache-Control": "no-store"})

    @app.delete("/api/inspect/{upload_id}")
    async def inspect_discard(upload_id: str):
        try:
            await run_in_threadpool(preview.discard, upload_id)
        except preview.UploadExpired as e:
            raise HTTPException(410, str(e)) from None
        except preview.UploadGone:
            raise HTTPException(404, "no such upload") from None
        return {"ok": True}

    @app.get("/api/books")
    async def list_books():
        return [public_book(b) for b in lib.list_books()]

    @app.get("/api/books/{book_id}")
    async def get_book(book_id: int):
        b = book_or_404(book_id, chapters=True)
        out = public_book(b, b["chapters"])
        out["job"] = _public_job(b["job"]) if b.get("job") else None
        out["playback"] = lib.get_playback(book_id)
        return out

    @app.delete("/api/books/{book_id}")
    async def delete_book(book_id: int):
        b = book_or_404(book_id)
        job = lib.latest_job(book_id)
        if job and job["state"] in ACTIVE_STATES:
            raise HTTPException(409, "this book is being processed: cancel its job first")
        if job and job["state"] == "queued":
            lib.request_cancel(job["id"])
        st = config.get_settings()
        trash = st.trash_dir / f"{book_id}-{int(time.time())}"
        trash.mkdir(parents=True, exist_ok=True)
        bookdir = st.library_dir / str(book_id)
        if bookdir.exists():
            shutil.move(str(bookdir), str(trash / "library"))
        src = Path(b["source_path"]) if b.get("source_path") else None
        if src is not None:
            try:
                folder = src.parent.resolve()
                if folder.parent == intake.uploads_dir().resolve():          # only ever our own staged copy
                    shutil.move(str(folder), str(trash / "source"))
            except OSError:
                log.warning("could not move the staged source of book %d to the trash", book_id)
        try:
            from .pipeline import book_hash
            if src is not None and src.exists():
                shutil.rmtree(st.work_dir / book_hash(src, b["voice"], b["speed"]), ignore_errors=True)
        except OSError:
            pass
        lib.delete_book(book_id)
        bus.publish("deleted", book_id=book_id)
        return {"ok": True, "trash": trash.name}

    @app.get("/api/books/{book_id}/chapters/{n}/audio")
    async def chapter_audio(book_id: int, n: int):
        book_or_404(book_id)
        ch = lib.get_chapter(book_id, n)
        if ch is None:
            raise HTTPException(404, "no such chapter")
        if ch["status"] != "ready" or not ch["audio_path"] or not Path(ch["audio_path"]).is_file():
            raise HTTPException(409, "this chapter is not ready yet")
        return FileResponse(ch["audio_path"], media_type="audio/mp4", headers={"Cache-Control": "no-cache"})   # Range: handled by FileResponse

    @app.get("/api/books/{book_id}/download")
    async def download(book_id: int, format: str = Query("m4b")):
        b = book_or_404(book_id)
        if format == "m4b":
            op = b.get("output_path")
            if not op or not Path(op).is_file():
                raise HTTPException(404, "no M4B for this book" + (" (it is made as MP3)" if b["format"] == "mp3" else " yet"))
            return FileResponse(op, media_type="audio/mp4", filename=Path(op).name)
        if format != "zip":
            raise HTTPException(400, "format must be m4b or zip")
        op = Path(b["output_path"]) if b.get("output_path") else None
        if op is not None and op.is_dir():
            files = sorted(p for p in op.iterdir() if p.is_file() and not p.name.startswith("."))
            names = [p.name for p in files]
        else:
            chs = [c for c in lib.chapters(book_id) if c["status"] == "ready" and c["audio_path"] and Path(c["audio_path"]).is_file()]
            files = [Path(c["audio_path"]) for c in chs]
            names = [f"{c['idx'] + 1:02d} - {intake.safe_filename(c['title'])}.m4a" for c in chs]
        if not files:
            raise HTTPException(404, "no chapters are ready yet")
        zroot = config.get_settings().cache_dir / "zips"
        zroot.mkdir(parents=True, exist_ok=True)
        tmpdir = Path(tempfile.mkdtemp(prefix="bb-zip-", dir=zroot))
        zpath = tmpdir / "chapters.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:       # audio is already compressed
            for p, name in zip(files, names):
                z.write(p, name)
        return FileResponse(zpath, media_type="application/zip", filename=f"{intake.safe_filename(b['title'])}.zip",
                            background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True))

    @app.get("/api/books/{book_id}/cover")
    async def cover(book_id: int):
        b = book_or_404(book_id)
        if not b.get("cover_path") or not Path(b["cover_path"]).is_file():
            raise HTTPException(404, "no cover")
        return FileResponse(b["cover_path"], media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    # ------------------------------------------------------------ playback + bookmarks
    @app.get("/api/books/{book_id}/playback")
    async def get_playback(book_id: int):
        book_or_404(book_id)
        return lib.get_playback(book_id)

    @app.put("/api/books/{book_id}/playback")
    async def put_playback(book_id: int, body: dict = Body(...)):
        book_or_404(book_id)
        try:
            chapter, position = int(body["chapter"]), float(body["position"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "body needs chapter (int) and position (seconds)") from None
        if chapter < 0 or position < 0 or position != position:
            raise HTTPException(400, "chapter and position must be >= 0")
        if lib.get_chapter(book_id, chapter) is None:
            raise HTTPException(400, "no such chapter")
        return lib.set_playback(book_id, chapter, position)

    @app.get("/api/books/{book_id}/bookmarks")
    async def get_bookmarks(book_id: int):
        book_or_404(book_id)
        return lib.bookmarks(book_id)

    @app.post("/api/books/{book_id}/bookmarks", status_code=201)
    async def add_bookmark(book_id: int, body: dict = Body(...)):
        book_or_404(book_id)
        try:
            chapter, position = int(body["chapter"]), float(body["position"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "body needs chapter (int) and position (seconds)") from None
        note = str(body.get("note", ""))[:500]
        if chapter < 0 or position < 0 or lib.get_chapter(book_id, chapter) is None:
            raise HTTPException(400, "bad chapter or position")
        return lib.add_bookmark(book_id, chapter, position, note)

    @app.delete("/api/books/{book_id}/bookmarks/{bookmark_id}")
    async def delete_bookmark(book_id: int, bookmark_id: int):
        book_or_404(book_id)
        if not lib.delete_bookmark(book_id, bookmark_id):
            raise HTTPException(404, "no such bookmark")
        return {"ok": True}

    # ------------------------------------------------------------ jobs
    @app.get("/api/jobs")
    async def list_jobs(state: str | None = Query(None)):
        states = state.split(",") if state else None
        return [{**_public_job(j), "title": j["title"], "source_name": j["source_name"]} for j in lib.list_jobs(states)]

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: int):
        try:
            result = lib.request_cancel(job_id)
        except LibraryError as e:
            raise HTTPException(409 if "already" in str(e) else 404, str(e)) from None
        bus.publish("job", job=_public_job(lib.get_job(job_id)))
        return {"state": result}

    @app.post("/api/jobs/{job_id}/retry")
    async def retry_job(job_id: int):
        try:
            lib.retry(job_id)
        except LibraryError as e:
            raise HTTPException(409 if "only" in str(e) else 404, str(e)) from None
        worker.wake()
        bus.publish("job", job=_public_job(lib.get_job(job_id)))
        return {"state": "queued"}

    # ------------------------------------------------------------ SSE
    @app.get("/api/events")
    async def events(request: Request):
        q = bus.subscribe()

        async def stream():
            try:
                yield "retry: 3000\n\nevent: hello\ndata: {}\n\n"
                last_beat = time.monotonic()
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await run_in_threadpool(q.get, True, 1.0)
                    except Exception:  # queue.Empty
                        if time.monotonic() - last_beat > 15:
                            last_beat = time.monotonic()
                            yield ": keepalive\n\n"
                        continue
                    yield f"id: {ev['id']}\nevent: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------ voices + formats
    @app.get("/api/voices")
    async def voices():
        return list_voices()

    @app.get("/api/voices/{voice}/sample")
    async def voice_sample(voice: str, speed: float = Query(1.0)):
        ok_voice(voice)
        speed = ok_speed(round(speed * 20) / 20)
        cache = config.get_settings().cache_dir / "samples"
        out = cache / f"{voice}_{speed:.2f}.m4a"
        if not out.is_file():
            def make() -> None:
                from .pipeline import _write_wav, encode_chapter
                factory = worker.engine_factory
                if factory is None:
                    from .engines import make_engine as factory
                engine = factory(voice, speed, config.get_settings().device)
                audio = engine.synth(SAMPLE_TEXT)
                cache.mkdir(parents=True, exist_ok=True)
                wav = cache / f"{voice}_{speed:.2f}.wav"
                _write_wav(wav, audio)
                try:
                    encode_chapter(wav, out)
                finally:
                    wav.unlink(missing_ok=True)

            fut = worker.submit(make) if worker.running else None
            try:
                if fut is None:
                    await run_in_threadpool(make)
                else:
                    await run_in_threadpool(fut.result, 180)
            except Exception as e:  # noqa: BLE001
                log.warning("voice sample failed: %s", e)
                raise HTTPException(503, "could not make the sample") from None
        return FileResponse(out, media_type="audio/mp4", headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/formats")
    async def formats():
        return [r.to_dict() for r in list_formats()]

    # ------------------------------------------------------------ settings + keys
    def home_short(p: Path) -> str:
        """Show folders under the user's home as ~/... (config expands ~ again on load)."""
        try:
            return "~/" + Path(p).expanduser().relative_to(Path.home()).as_posix()
        except ValueError:
            return str(p)

    def settings_view() -> dict:
        st = config.get_settings()
        return {
            "voice": st.voice, "speed": st.speed, "format": st.format,
            "output_dir": home_short(st.output_dir), "watch_inbox": home_short(st.watch_inbox),
            "watch_outbox": home_short(st.outbox),
            "donate_url": st.donate_url, "max_upload_mb": st.web_max_upload_mb,
            "llm_enabled": st.llm_enabled,
            "llm": {"enabled": st.llm_enabled, "order": st.llm_order, "available": provider_registry.names(),
                    "keys": {p: key_status(p) for p in KEY_PROVIDERS},
                    "models": {p: (st.llm_providers.get(p, {}).get("model") or provider_registry.get(p).default_model) for p in KEY_PROVIDERS}},
            "auth_required": bool(token),
        }

    @app.get("/api/settings")
    async def get_settings():
        return settings_view()

    @app.put("/api/settings")
    async def put_settings(body: dict = Body(...)):
        changes: dict[tuple[str, str], object] = {}
        allowed = {"voice", "speed", "format", "output_dir", "watch_inbox", "watch_outbox", "donate_url", "llm_order", "llm_enabled"}
        unknown = set(body) - allowed
        if unknown:
            raise HTTPException(400, f"unknown setting(s): {', '.join(sorted(unknown))}")
        if "voice" in body:
            changes[("", "voice")] = ok_voice(str(body["voice"]))
        if "speed" in body:
            try:
                changes[("", "speed")] = ok_speed(float(body["speed"]))
            except (TypeError, ValueError):
                raise HTTPException(400, "speed must be a number") from None
        if "format" in body:
            changes[("", "format")] = ok_format(str(body["format"]))
        for key, (table, name) in {"output_dir": ("", "output_dir"), "watch_inbox": ("watch", "inbox"), "watch_outbox": ("watch", "outbox")}.items():
            if key in body:
                v = str(body[key]).strip()
                if not v or "\x00" in v or len(v) > 500:
                    raise HTTPException(400, f"{key} must be a folder path")
                changes[(table, name)] = v
        if "donate_url" in body:
            v = str(body["donate_url"]).strip()
            if v and not re.match(r"^https?://[^\s]+$", v):
                raise HTTPException(400, "donate_url must be an http(s) link")
            changes[("web", "donate_url")] = v or config.DEFAULT_DONATE_URL
        if "llm_enabled" in body:
            if not isinstance(body["llm_enabled"], bool):
                raise HTTPException(400, "llm_enabled must be true or false")
            changes[("llm", "enabled")] = body["llm_enabled"]
        if "llm_order" in body:
            order = body["llm_order"]
            if not isinstance(order, list) or not all(isinstance(x, str) and provider_registry.has(x) for x in order):
                raise HTTPException(400, f"llm_order must be a list of provider names ({', '.join(provider_registry.names())})")
            changes[("llm", "order")] = order
        await run_in_threadpool(update_config, config.config_path(), changes)
        config.reload_settings()
        return settings_view()

    @app.put("/api/settings/keys")
    async def put_keys(body: dict = Body(...)):
        changes: dict[tuple[str, str], object] = {}
        for provider, key in body.items():
            if provider not in KEY_PROVIDERS:
                raise HTTPException(400, f"unknown provider {provider!r} (use {', '.join(KEY_PROVIDERS)})")
            key = str(key).strip()
            if not 8 <= len(key) <= 300 or re.search(r"\s", key):
                raise HTTPException(400, "that does not look like an API key")
            register_secret(key)
            changes[(f"llm.{provider}", "api_key")] = key
        if changes:
            changes[("llm", "enabled")] = True          # saving a key here is the opt-in
        if not changes:
            raise HTTPException(400, "send {\"anthropic\": \"...\"} and/or {\"openai\": \"...\"}")
        await run_in_threadpool(update_config, config.config_path(), changes)
        config.reload_settings()
        return {p: key_status(p) for p in KEY_PROVIDERS}

    @app.delete("/api/settings/keys/{provider}")
    async def delete_key(provider: str):
        if provider not in KEY_PROVIDERS:
            raise HTTPException(404, "unknown provider")
        await run_in_threadpool(update_config, config.config_path(), {(f"llm.{provider}", "api_key"): None})
        config.reload_settings()
        status = key_status(provider)
        return {p: key_status(p) for p in KEY_PROVIDERS} | ({"note": "a key is still set in the environment"} if status["set"] else {})

    return app
