"""HTTP API (TestClient for most, a live uvicorn for streaming): every endpoint, Range, uploads, auth, keys."""
import io
import json
import os
import re
import socket
import stat
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bookbuddy import config
from bookbuddy.engines.fake import FakeEngine
from bookbuddy.server import ServerConfigError, check_bind, create_app
from helpers import SAMPLE_CHAPTERS, make_epub

LOCAL = "http://127.0.0.1"


def fake_factory(voice, speed, device=None):
    return FakeEngine(voice, speed, device)


@pytest.fixture
def epub(tmp_path):
    return make_epub(tmp_path / "my book.epub", SAMPLE_CHAPTERS)


@pytest.fixture
def client(env):
    app = create_app(engine_factory=fake_factory)
    with TestClient(app, base_url=LOCAL) as c:
        yield c


def upload(c, path, **form):
    with open(path, "rb") as f:
        return c.post("/api/books", files={"file": (Path(path).name, f, "application/epub+zip")}, data=form)


def wait_done(c, book_id, timeout=30):
    t = time.time()
    while time.time() - t < timeout:
        b = c.get(f"/api/books/{book_id}").json()
        if b["status"] in ("ready", "failed", "cancelled"):
            return b
        time.sleep(0.05)
    raise AssertionError("timed out")


@pytest.fixture
def done_book(client, epub):
    r = upload(client, epub)
    assert r.status_code == 201, r.text
    return client, wait_done(client, r.json()["book_id"])


# ------------------------------------------------------------------ books

def test_upload_creates_book_and_job_and_finishes(client, epub):
    r = upload(client, epub, voice="bf_emma", speed="1.1", format="m4b", llm="off")
    assert r.status_code == 201
    ids = r.json()
    b = wait_done(client, ids["book_id"])
    assert b["status"] == "ready" and b["voice"] == "bf_emma" and b["speed"] == 1.1 and b["title"] == "Test Book"
    assert b["chapters_ready"] == b["chapters_total"] == 3 and b["has_cover"] and b["has_download"]
    assert b["job"]["state"] == "done" and b["job"]["id"] == ids["job_id"]
    assert not any("path" in k for k in b)                           # no server paths leak to the browser
    assert [x["id"] for x in client.get("/api/books").json()] == [ids["book_id"]]
    assert client.get("/api/jobs").json()[0]["title"] == "Test Book"


def test_chapter_audio_range_requests(done_book):
    c, b = done_book
    url = f"/api/books/{b['id']}/chapters/0/audio"
    full = c.get(url)
    assert full.status_code == 200 and full.headers["accept-ranges"] == "bytes" and full.headers["content-type"] == "audio/mp4"
    size = len(full.content)
    assert full.content[4:8] == b"ftyp"
    part = c.get(url, headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == full.content[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{size}"
    tail = c.get(url, headers={"Range": "bytes=-50"})
    assert tail.status_code == 206 and tail.content == full.content[-50:]
    assert c.get(url, headers={"Range": f"bytes={size + 10}-"}).status_code == 416


def test_chapter_not_ready_or_missing(env, epub):
    from bookbuddy import intake
    app = create_app(engine_factory=fake_factory, start_worker=False)         # nothing runs: the chapter stays queued
    staged, sha, _ = intake.stage_path(epub)
    book_id, _ = intake.enqueue_file(app.state.library, staged, sha, origin="web", voice="af_heart", speed=1.0, fmt="m4b", llm="none")
    app.state.library.replace_chapters(book_id, [{"index": 0, "title": "x"}])
    with TestClient(app, base_url=LOCAL) as c:
        assert c.get(f"/api/books/{book_id}/chapters/0/audio").status_code == 409
        assert c.get(f"/api/books/{book_id}/chapters/99/audio").status_code == 404
        assert c.get("/api/books/9999/chapters/0/audio").status_code == 404
        assert c.get(f"/api/books/{book_id}/download?format=m4b").status_code == 404
        assert c.get(f"/api/books/{book_id}/download?format=zip").status_code == 404
        assert c.get(f"/api/books/{book_id}/cover").status_code == 404


def test_download_m4b_and_zip_and_cover(done_book):
    c, b = done_book
    r = c.get(f"/api/books/{b['id']}/download?format=m4b")
    assert r.status_code == 200 and "Test%20Book%20-%20A.%20Writer.m4b" in r.headers["content-disposition"] and r.content[4:8] == b"ftyp"
    z = c.get(f"/api/books/{b['id']}/download?format=zip")
    assert z.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert len(names) == 3 and names[0].startswith("01 - ") and names[0].endswith(".m4a")
    cv = c.get(f"/api/books/{b['id']}/cover")
    assert cv.status_code == 200 and cv.headers["content-type"] == "image/jpeg" and cv.content[:2] == b"\xff\xd8"
    assert c.get(f"/api/books/{b['id']}/download?format=exe").status_code == 400
    assert c.get("/api/books/9999/download").status_code == 404


def test_mp3_book_zip_is_the_mp3_folder_and_has_no_m4b(client, epub):
    r = upload(client, epub, format="mp3")
    b = wait_done(client, r.json()["book_id"])
    assert client.get(f"/api/books/{b['id']}/download?format=m4b").status_code == 404
    names = zipfile.ZipFile(io.BytesIO(client.get(f"/api/books/{b['id']}/download?format=zip").content)).namelist()
    assert len(names) == 3 and all(n.endswith(".mp3") for n in names)


def test_delete_moves_files_to_trash_and_leaves_outside_files_alone(done_book, env, epub):
    c, b = done_book
    st = config.get_settings()
    assert (st.library_dir / str(b["id"])).is_dir()
    r = c.delete(f"/api/books/{b['id']}")
    assert r.status_code == 200
    assert not (st.library_dir / str(b["id"])).exists()
    trashed = list(st.trash_dir.glob(f"{b['id']}-*/library/*.m4b"))
    assert len(trashed) == 1
    assert c.get(f"/api/books/{b['id']}").status_code == 404
    assert epub.exists()                                              # the user's original is never touched
    assert c.delete(f"/api/books/{b['id']}").status_code == 404


def test_delete_refuses_while_processing(env, epub):
    class Slow(FakeEngine):
        def synth(self, text):
            time.sleep(0.05)
            return super().synth(text)
    app = create_app(engine_factory=lambda v, s, d=None: Slow(v, s, d))
    with TestClient(app, base_url=LOCAL) as c:
        ids = upload(c, make_epub(epub.parent / "long.epub", [("C", ["word " * 60] * 20)] * 3)).json()
        for _ in range(100):
            if c.get(f"/api/books/{ids['book_id']}").json()["job"]["state"] == "voicing":
                break
            time.sleep(0.05)
        assert c.delete(f"/api/books/{ids['book_id']}").status_code == 409
        assert c.post(f"/api/jobs/{ids['job_id']}/cancel").json()["state"] in ("cancelling", "cancelled")
        assert wait_done(c, ids["book_id"])["status"] == "cancelled"
        assert c.post(f"/api/jobs/{ids['job_id']}/cancel").status_code == 409          # already finished
        assert c.post(f"/api/jobs/{ids['job_id']}/retry").json()["state"] == "queued"
        assert wait_done(c, ids["book_id"], 60)["status"] == "ready"
        assert c.post(f"/api/jobs/{ids['job_id']}/retry").status_code == 409           # done jobs cannot be retried
        assert c.post("/api/jobs/9999/cancel").status_code == 404


# ------------------------------------------------------------------ uploads

def test_upload_rejects_bad_extension_empty_and_oversize(client, env, epub):
    r = client.post("/api/books", files={"file": ("virus.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400 and "unsupported" in r.json()["detail"]
    assert client.post("/api/books", files={"file": ("empty.epub", b"", "application/epub+zip")}).status_code == 400
    assert client.post("/api/books", files={"file": ("noext", b"abc", "text/plain")}).status_code == 400
    assert client.post("/api/books").status_code == 422
    cfgdir = Path(os.environ["BOOKBUDDY_CONFIG_DIR"])
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "config.toml").write_text("[web]\nmax_upload_mb = 1\n")
    config.reload_settings()
    big = b"x" * (2 << 20)
    r = client.post("/api/books", files={"file": ("big.txt", big, "text/plain")})
    assert r.status_code == 413
    assert not list((config.data_dir() / "uploads").glob("*/*"))               # nothing half-written is left behind
    assert client.get("/api/books").json() == []


@pytest.mark.parametrize("evil", ["../../evil.txt", "..\\..\\evil.txt", "/etc/cron.d/evil.txt", "a/../../b.txt", "x\x00y.txt", ".hidden.txt"])
def test_upload_filenames_cannot_escape_the_uploads_dir(client, env, evil):
    r = client.post("/api/books", files={"file": (evil, b"Some text to read aloud. " * 20, "text/plain")})
    assert r.status_code == 201, r.text
    uploads = (config.data_dir() / "uploads").resolve()
    files = [p for p in uploads.rglob("*") if p.is_file()]
    assert files and all(uploads in p.resolve().parents for p in files)
    assert not (Path(env) / "evil.txt").exists() and not Path("/etc/cron.d/evil.txt").exists()
    assert all(".." not in p.name and "/" not in p.name for p in files)


def test_upload_option_validation(client, epub):
    assert upload(client, epub, voice="nobody").status_code == 400
    assert upload(client, epub, speed="9").status_code == 400
    assert upload(client, epub, format="wav").status_code == 400
    assert upload(client, epub, llm="maybe").status_code == 400


def test_path_params_cannot_traverse(client):
    for bad in ("..%2f..%2fetc%2fpasswd", "../../etc/passwd"):
        assert client.get(f"/api/voices/{bad}/sample").status_code in (400, 404)
    assert client.get("/api/books/abc").status_code == 422
    assert client.get("/api/books/1/chapters/..%2f..%2fx/audio").status_code in (404, 422)


# ------------------------------------------------------------------ playback, bookmarks

def test_playback_and_bookmarks(done_book):
    c, b = done_book
    base = f"/api/books/{b['id']}"
    assert c.get(f"{base}/playback").json()["position"] == 0
    shelf = lambda: next(x for x in c.get("/api/books").json() if x["id"] == b["id"])  # noqa: E731
    assert shelf()["listened"] is None                       # never played
    assert c.put(f"{base}/playback", json={"chapter": 1, "position": 12.5}).json()["position"] == 12.5
    assert c.get(f"{base}").json()["playback"]["chapter"] == 1
    ch0 = c.get(f"{base}").json()["chapters"][0]["duration"]
    assert abs(shelf()["listened"] - (ch0 + 12.5)) < 1e-6    # chapter 0 whole + 12.5 s into chapter 1
    assert c.put(f"{base}/playback", json={"chapter": 99, "position": 1}).status_code == 400
    assert c.put(f"{base}/playback", json={"chapter": 0, "position": -1}).status_code == 400
    assert c.put(f"{base}/playback", json={"chapter": "x"}).status_code == 400
    bm = c.post(f"{base}/bookmarks", json={"chapter": 2, "position": 3.0, "note": "here"})
    assert bm.status_code == 201
    c.post(f"{base}/bookmarks", json={"chapter": 0, "position": 1.0})
    assert [x["chapter"] for x in c.get(f"{base}/bookmarks").json()] == [0, 2]
    assert c.post(f"{base}/bookmarks", json={"chapter": 50, "position": 1}).status_code == 400
    assert c.delete(f"{base}/bookmarks/{bm.json()['id']}").status_code == 200
    assert c.delete(f"{base}/bookmarks/{bm.json()['id']}").status_code == 404
    assert c.get("/api/books/9999/playback").status_code == 404


# ------------------------------------------------------------------ voices, formats, page

def test_voices_samples_formats_and_page(client):
    v = client.get("/api/voices").json()
    assert any(x["id"] == "af_heart" and "Heart" in x["label"] for x in v)
    assert all(x["style"] for x in v)                       # every voice has measured style words
    s = client.get("/api/voices/af_heart/sample?speed=1.0")
    assert s.status_code == 200 and s.content[4:8] == b"ftyp"
    cached = list((config.get_settings().cache_dir / "samples").glob("*.m4a"))
    assert len(cached) == 1
    mtime = cached[0].stat().st_mtime_ns
    assert client.get("/api/voices/af_heart/sample?speed=1.0").status_code == 200
    assert cached[0].stat().st_mtime_ns == mtime                                  # second request is served from the cache
    assert client.get("/api/voices/nobody/sample").status_code == 400
    assert client.get("/api/voices/af_heart/sample?speed=7").status_code == 400
    f = client.get("/api/formats").json()
    assert {"EPUB", "PDF"} <= {x["name"] for x in f} and all("available" in x for x in f)
    page = client.get("/")
    assert page.status_code == 200 and "Book Buddy" in page.text and "<script" in page.text
    assert not re.search(r"(src|href)=[\"']?https?:", page.text) and "@import" not in page.text      # no CDN, no external assets
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert client.get("/api/session").json() == {"auth_required": False, "authenticated": True, "version": client.get("/api/session").json()["version"]}


# ------------------------------------------------------------------ settings and keys

def test_settings_roundtrip_and_donate_url(client, env):
    s = client.get("/api/settings").json()
    assert s["donate_url"] == "https://github.com/sponsors/reed-and-write" and s["llm"]["order"] == ["anthropic", "openai"]
    r = client.put("/api/settings", json={"voice": "bf_emma", "speed": 1.25, "format": "mp3", "watch_inbox": "~/Inbox",
                                          "donate_url": "https://example.org/donate", "llm_order": ["openai", "anthropic"]})
    assert r.status_code == 200
    s = client.get("/api/settings").json()
    assert (s["voice"], s["speed"], s["format"], s["donate_url"], s["llm"]["order"]) == ("bf_emma", 1.25, "mp3", "https://example.org/donate", ["openai", "anthropic"])
    assert s["watch_inbox"] == "~/Inbox"            # home folders shown as ~/...
    assert config.get_settings().donate_url == "https://example.org/donate"
    assert client.put("/api/settings", json={"donate_url": "javascript:alert(1)"}).status_code == 400
    assert client.put("/api/settings", json={"voice": "nobody"}).status_code == 400
    assert client.put("/api/settings", json={"llm_order": ["skynet"]}).status_code == 400
    assert client.put("/api/settings", json={"bogus": 1}).status_code == 400
    assert client.put("/api/settings", json={"donate_url": ""}).json()["donate_url"] == "https://github.com/sponsors/reed-and-write"


def test_settings_keep_existing_config_comments(client, env):
    cfg = Path(os.environ["BOOKBUDDY_CONFIG_DIR"]) / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('# my notes\nvoice = "af_heart"\n\n[notifiers]\nenabled = ["status-file"]   # keep\n')
    config.reload_settings()
    client.put("/api/settings", json={"speed": 1.5})
    text = cfg.read_text()
    assert "# my notes" in text and 'enabled = ["status-file"]   # keep' in text and "speed = 1.5" in text


def test_keys_are_written_0600_and_never_echoed(client, env):
    secret = "sk-" + "ant-api03-" + "SECRETSECRETSECRET1234"
    r = client.put("/api/settings/keys", json={"anthropic": secret})
    assert r.status_code == 200
    assert r.json()["anthropic"] == {"set": True, "last4": "1234", "source": "config"} and r.json()["openai"] == {"set": False}
    cfg = Path(os.environ["BOOKBUDDY_CONFIG_DIR"]) / "config.toml"
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600 and secret in cfg.read_text()
    for path in ("/api/settings", "/api/books", "/api/jobs", "/api/formats", "/api/voices"):
        assert secret not in client.get(path).text and "SECRETSECRET" not in client.get(path).text
    assert "SECRETSECRET" not in client.put("/api/settings", json={"speed": 1.0}).text
    assert client.get("/api/settings").json()["llm"]["keys"]["anthropic"]["last4"] == "1234"
    d = client.delete("/api/settings/keys/anthropic")
    assert d.status_code == 200 and d.json()["anthropic"] == {"set": False}
    assert secret not in cfg.read_text() and stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert client.put("/api/settings/keys", json={"gemini": "x" * 20}).status_code == 400
    assert client.put("/api/settings/keys", json={"openai": "short"}).status_code == 400
    assert client.put("/api/settings/keys", json={"openai": "has space inside key"}).status_code == 400
    assert client.put("/api/settings/keys", json={}).status_code == 400
    assert client.delete("/api/settings/keys/gemini").status_code == 404


def test_env_key_is_reported_but_not_removable(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-ENVKEY-9876abcd")
    k = client.get("/api/settings").json()["llm"]["keys"]["openai"]
    assert k == {"set": True, "last4": "abcd", "source": "env"}
    assert "note" in client.delete("/api/settings/keys/openai").json()


def test_a_saved_key_makes_the_provider_available_for_auto(client):
    from bookbuddy.llm_clean import resolve_order
    assert resolve_order("auto") == []                                 # no key anywhere -> no LLM pass
    assert client.get("/api/settings").json()["llm"]["enabled"] is False
    client.put("/api/settings/keys", json={"openai": "sk-test-key-for-openai-1234"})
    assert client.get("/api/settings").json()["llm"]["enabled"] is True   # saving a key is the opt-in
    assert resolve_order("auto") == ["openai"]


def test_env_key_alone_keeps_the_llm_pass_off(client, monkeypatch):
    from bookbuddy.llm_clean import resolve_order
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-" + "ant-" + "ENVKEY0000aaaa")
    s = client.get("/api/settings").json()
    assert s["llm"]["enabled"] is False and s["llm_enabled"] is False
    assert s["llm"]["keys"]["anthropic"]["set"] is True              # reported, but not used
    assert resolve_order("auto") == []


def test_llm_enabled_can_be_toggled_via_settings(client, monkeypatch):
    from bookbuddy.llm_clean import resolve_order
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-" + "ant-" + "ENVKEY0000aaaa")
    assert client.put("/api/settings", json={"llm_enabled": True}).json()["llm"]["enabled"] is True
    assert resolve_order("auto") == ["anthropic"]
    assert client.put("/api/settings", json={"llm_enabled": False}).json()["llm"]["enabled"] is False
    assert resolve_order("auto") == []
    assert client.put("/api/settings", json={"llm_enabled": "yes"}).status_code == 400


# ------------------------------------------------------------------ security

def test_non_loopback_requires_a_token(env):
    with pytest.raises(ServerConfigError, match="token"):
        check_bind("0.0.0.0", None)
    with pytest.raises(ServerConfigError):
        create_app(host="192.168.1.5")
    check_bind("127.0.0.1", None)
    check_bind("::1", None)
    check_bind("0.0.0.0", "secret")
    create_app(host="0.0.0.0", token="secret", start_worker=False)


def test_cli_serve_refuses_non_loopback_without_token(env, capsys):
    from bookbuddy.cli import main
    assert main(["serve", "--host", "0.0.0.0", "--port", "1"]) == 2
    assert "token" in capsys.readouterr().err


def test_token_required_on_every_api_call(env, epub):
    app = create_app(token="s3cret-token", engine_factory=fake_factory, host="0.0.0.0")
    with TestClient(app, base_url="http://10.0.0.5:8765") as c:
        assert c.get("/").status_code == 200                           # the page itself loads, to ask for the token
        s = c.get("/api/session").json()
        assert s["auth_required"] and not s["authenticated"]
        for method, path in [("get", "/api/books"), ("get", "/api/books/1"), ("get", "/api/settings"), ("get", "/api/jobs"),
                             ("get", "/api/voices"), ("get", "/api/formats"), ("get", "/api/events"), ("put", "/api/settings"),
                             ("put", "/api/settings/keys"), ("post", "/api/books"), ("delete", "/api/books/1"),
                             ("post", "/api/jobs/1/cancel"), ("get", "/api/books/1/cover"), ("get", "/api/books/1/chapters/0/audio"),
                             ("get", "/api/voices/af_heart/sample")]:
            r = getattr(c, method)(path)
            assert r.status_code == 401, (method, path, r.status_code)
        assert c.get("/api/books", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/api/books", headers={"Authorization": "Basic s3cret-token"}).status_code == 401
        assert c.get("/api/books", headers={"Authorization": "Bearer s3cret-token"}).status_code == 200
        assert c.post("/api/login", json={"token": "nope"}).status_code == 401
        assert c.get("/api/books").status_code == 401
        ok = c.post("/api/login", json={"token": "s3cret-token"})
        assert ok.status_code == 200 and "httponly" in ok.headers["set-cookie"].lower() and "samesite=strict" in ok.headers["set-cookie"].lower()
        assert c.get("/api/books").status_code == 200                  # the cookie carries the session (page, <audio>, SSE)
        assert upload(c, epub).status_code == 201
        c.post("/api/logout")
        assert c.get("/api/books").status_code == 401


def test_loopback_without_token_rejects_foreign_host_and_cross_origin_posts(client):
    assert client.get("/api/books", headers={"Host": "evil.example"}).status_code == 403          # DNS rebinding
    assert client.get("/api/books", headers={"Host": "localhost:8765"}).status_code == 200
    r = client.put("/api/settings", json={"speed": 1.1}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403                                                                   # a web page posting at your server
    assert client.put("/api/settings", json={"speed": 1.1}, headers={"Origin": "http://127.0.0.1"}).status_code == 200


def test_no_cors_headers_ever(client):
    r = client.get("/api/books", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
    r = client.options("/api/books", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


# ------------------------------------------------------------------ live server: SSE

@pytest.fixture
def live(env):
    import uvicorn
    app = create_app(engine_factory=fake_factory)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(15)


def test_sse_streams_progress_and_chapter_events(live, epub):
    got = []
    ready = threading.Event()

    def reader():
        with httpx.stream("GET", live + "/api/events", timeout=30) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            ev = None
            for line in r.iter_lines():
                if line.startswith("event:"):
                    ev = line[6:].strip()
                    if ev == "hello":
                        ready.set()
                elif line.startswith("data:") and ev:
                    got.append((ev, json.loads(line[5:])))
                    if ev == "done":
                        return

    t = threading.Thread(target=reader)
    t.start()
    assert ready.wait(10)
    with open(epub, "rb") as f:
        r = httpx.post(live + "/api/books", files={"file": ("my book.epub", f)})
    assert r.status_code == 201
    t.join(30)
    kinds = [k for k, _ in got]
    assert {"job", "book", "chapter", "planned", "done"} <= set(kinds)
    ready_chapters = [d["index"] for k, d in got if k == "chapter" and d.get("status") == "ready"]
    assert ready_chapters == [0, 1, 2]
    states = [d["job"]["state"] for k, d in got if k == "job"]
    assert "voicing" in states and "assembling" in states
    # chapter 1 became ready before the "done" event (progressive), with a Range request working over real HTTP
    assert kinds.index("done") > max(i for i, (k, d) in enumerate(got) if k == "chapter" and d.get("index") == 0 and d.get("status") == "ready")
    bid = r.json()["book_id"]
    rr = httpx.get(live + f"/api/books/{bid}/chapters/0/audio", headers={"Range": "bytes=0-99"})
    assert rr.status_code == 206 and len(rr.content) == 100


def test_sse_requires_token_and_cookie_works(env):
    import uvicorn
    app = create_app(token="tok-1234567", engine_factory=fake_factory, host="127.0.0.1")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    try:
        assert httpx.get(f"http://127.0.0.1:{port}/api/events").status_code == 401
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/events", headers={"Authorization": "Bearer tok-1234567"}, timeout=10) as r:
            assert r.status_code == 200
            assert next(r.iter_lines()) == "retry: 3000"
    finally:
        server.should_exit = True
        t.join(15)
