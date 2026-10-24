"""Inspect endpoint (stage + metadata, no job), upload_id consumption, expiry, static assets, no external URLs in the page."""
import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bookbuddy import config, intake, preview
from bookbuddy.server import create_app
from helpers import SAMPLE_CHAPTERS, make_epub
from test_server import LOCAL, fake_factory, wait_done

FIX = Path(__file__).parent / "fixtures"
WEB = Path(preview.__file__).parent / "web"


@pytest.fixture
def client(env):
    with TestClient(create_app(engine_factory=fake_factory), base_url=LOCAL) as c:
        yield c


def inspect(c, path, name=None, **form):
    with open(path, "rb") as f:
        return c.post("/api/inspect", files={"file": (name or Path(path).name, f, "application/octet-stream")}, data=form)


@pytest.mark.parametrize("name", ["alice.epub", "alice.docx", "alice.fb2", "alice.fb2.zip", "alice.md", "alice.odt", "alice.rtf",
                                  "alice_gutenberg.txt", "alice_noise.html", "sample_book.pdf"])
def test_inspect_each_native_format(client, name):
    r = inspect(client, FIX / name)
    assert r.status_code == 201, r.text
    d = r.json()
    assert re.fullmatch(r"[0-9a-f]{32}", d["upload_id"])
    assert d["title"] == "Alice's Adventures in Wonderland" and d["author"] == "Lewis Carroll"
    assert d["chapters"] >= 1 and d["words"] > 100 and not d["needs_ocr"] and d["format"]
    assert d["audio_seconds"] == round(d["words"] / preview.WORDS_PER_MINUTE * 60 / config.get_settings().speed)
    assert not any("path" in k for k in d)
    assert client.get("/api/books").json() == [] and client.get("/api/jobs").json() == []     # nothing was queued


def test_audio_estimate_follows_speed(client):
    slow = inspect(client, FIX / "alice.epub", speed="1.0").json()["audio_seconds"]
    fast = inspect(client, FIX / "alice.epub", speed="2.0").json()["audio_seconds"]
    assert abs(slow / fast - 2) < 0.01
    assert 9000 < slow < 10000                                # the measured 2 h 36 m book


def test_inspect_cover(client):
    d = inspect(client, FIX / "alice.epub").json()
    assert d["has_cover"]
    r = client.get(f"/api/inspect/{d['upload_id']}/cover")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/") and len(r.content) > 100
    no_cover = inspect(client, FIX / "alice.md").json()
    assert client.get(f"/api/inspect/{no_cover['upload_id']}/cover").status_code == 404


def test_scanned_returns_what_is_cheap(client):
    d = inspect(client, FIX / "scanned.pdf").json()
    assert d["needs_ocr"] and d["chapters"] is None and d["words"] is None and d["audio_seconds"] is None and d["title"]
    d = inspect(client, FIX / "book_pages.cbz").json()
    assert d["needs_ocr"] and d["format"] == "CBZ"


def test_bad_extension_empty_drm_and_unreadable(client, tmp_path):
    bad = tmp_path / "x.exe"
    bad.write_bytes(b"MZ....")
    r = inspect(client, bad)
    assert r.status_code == 400 and "unsupported" in r.json()["detail"]
    empty = tmp_path / "e.epub"
    empty.write_bytes(b"")
    assert inspect(client, empty).status_code == 400
    r = inspect(client, FIX / "drm_encrypted.epub")
    assert r.status_code == 400 and "DRM" in r.json()["detail"]
    junk = tmp_path / "junk.epub"
    junk.write_bytes(b"this is not an epub at all")
    assert inspect(client, junk).status_code == 400
    assert not list(intake.uploads_dir().glob("*/*"))                                         # failures leave nothing staged


def test_too_large(env):
    (env / "config").mkdir(exist_ok=True)
    (env / "config" / "config.toml").write_text("[web]\nmax_upload_mb = 1\n")
    config.reload_settings()
    with TestClient(create_app(engine_factory=fake_factory), base_url=LOCAL) as c:
        r = c.post("/api/inspect", files={"file": ("big.txt", b"x" * (2 << 20))})
        assert r.status_code == 413
        assert not list(intake.uploads_dir().glob("*/*"))                       # nothing half-written is left behind
        assert c.post("/api/inspect", files={"file": ("ok.txt", b"x" * 1000)}).status_code in (201, 400)


def test_traversal_names_and_ids(client):
    with open(FIX / "alice.md", "rb") as f:
        r = client.post("/api/inspect", files={"file": ("../../../etc/evil.md", f)})
    assert r.status_code == 201
    staged = list(intake.uploads_dir().glob("*/evil.md"))
    assert len(staged) == 1 and staged[0].parent.parent == intake.uploads_dir()
    for bad in ("..", "%2e%2e", "../x", "a" * 32, "0" * 31, "G" * 32, "x/y"):
        for call in (lambda u: client.get(f"/api/inspect/{u}/cover"), lambda u: client.delete(f"/api/inspect/{u}")):
            assert call(bad).status_code in (404, 405)
    r = client.post("/api/books", data={"upload_id": "../../etc"})
    assert r.status_code == 404
    assert client.delete("/api/inspect/" + "0" * 32).status_code == 404


def test_discard(client):
    d = inspect(client, FIX / "alice.epub").json()
    folder = intake.uploads_dir() / d["upload_id"]
    assert folder.is_dir()
    assert client.delete(f"/api/inspect/{d['upload_id']}").json() == {"ok": True}
    assert not folder.exists()
    assert client.delete(f"/api/inspect/{d['upload_id']}").status_code == 404
    assert client.post("/api/books", data={"upload_id": d["upload_id"]}).status_code == 404


def test_make_audiobook_consumes_the_staged_upload_once(client):
    d = inspect(client, FIX / "alice_formats.epub").json()
    r = client.post("/api/books", data={"upload_id": d["upload_id"], "voice": "bf_emma", "speed": "1.1", "format": "m4b", "llm": "off"})
    assert r.status_code == 201, r.text
    b = wait_done(client, r.json()["book_id"])
    assert b["status"] == "ready" and b["voice"] == "bf_emma" and b["speed"] == 1.1
    assert len(list(intake.uploads_dir().iterdir())) == 1                                   # no second copy was made
    folder = intake.uploads_dir() / d["upload_id"]
    assert not (folder / preview.META_NAME).exists()
    again = client.post("/api/books", data={"upload_id": d["upload_id"]})
    assert again.status_code == 404
    assert client.delete(f"/api/inspect/{d['upload_id']}").status_code == 404                # a queued book can't be discarded this way
    assert len(client.get("/api/books").json()) == 1


def test_queued_book_shows_the_inspected_title_at_once(env):
    with TestClient(create_app(engine_factory=fake_factory, start_worker=False), base_url=LOCAL) as c:
        d = inspect(c, FIX / "alice_formats.epub").json()
        assert c.post("/api/books", data={"upload_id": d["upload_id"]}).status_code == 201
        assert c.get("/api/books").json()[0]["title"] == "Alice's Adventures in Wonderland"


def test_books_needs_exactly_one_of_file_or_upload_id(client):
    assert client.post("/api/books", data={"voice": "af_heart"}).status_code == 422
    d = inspect(client, FIX / "alice.md").json()
    with open(FIX / "alice.md", "rb") as f:
        r = client.post("/api/books", files={"file": ("a.md", f)}, data={"upload_id": d["upload_id"]})
    assert r.status_code == 400


def test_plain_file_upload_still_works(client):
    with open(FIX / "alice.md", "rb") as f:
        r = client.post("/api/books", files={"file": ("alice.md", f)})
    assert r.status_code == 201


def test_expired_upload_is_gone_and_swept(client, monkeypatch):
    d = inspect(client, FIX / "alice.epub").json()
    fresh = inspect(client, FIX / "alice.md").json()
    mp = intake.uploads_dir() / d["upload_id"] / preview.META_NAME
    meta = json.loads(mp.read_text())
    meta["created"] = time.time() - 25 * 3600
    mp.write_text(json.dumps(meta))
    assert client.get(f"/api/inspect/{d['upload_id']}/cover").status_code == 410            # expired -> 410 and removed
    assert not (intake.uploads_dir() / d["upload_id"]).exists()
    # sweep removes expired ones, keeps fresh ones and watcher/CLI staged files
    old = inspect(client, FIX / "alice.epub").json()
    mp = intake.uploads_dir() / old["upload_id"] / preview.META_NAME
    meta = json.loads(mp.read_text())
    meta["created"] = time.time() - 30 * 3600
    mp.write_text(json.dumps(meta))
    cli_staged, _, _ = intake.stage_path(FIX / "alice.md")
    assert preview.cleanup_expired() == 1
    assert not (intake.uploads_dir() / old["upload_id"]).exists()
    assert (intake.uploads_dir() / fresh["upload_id"]).exists() and cli_staged.exists()
    r = client.post("/api/books", data={"upload_id": old["upload_id"]})
    assert r.status_code == 404


def test_sweep_runs_on_server_start(env):
    d = intake.uploads_dir() / ("a" * 32)
    d.mkdir(parents=True)
    (d / "x.txt").write_text("hello")
    (d / preview.META_NAME).write_text(json.dumps({"created": time.time() - 48 * 3600, "filename": "x.txt"}))
    with TestClient(create_app(engine_factory=fake_factory), base_url=LOCAL) as c:
        deadline = time.time() + 5
        while d.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert not d.exists()


def test_inspect_needs_token_when_auth_is_on(env):
    app = create_app(token="s3cret-token", engine_factory=fake_factory, host="0.0.0.0")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert inspect(c, FIX / "alice.md").status_code == 401
        assert c.get("/api/inspect/" + "0" * 32 + "/cover").status_code == 401
        assert c.delete("/api/inspect/" + "0" * 32).status_code == 401
        assert c.post("/api/books", data={"upload_id": "0" * 32}).status_code == 401
        h = {"Authorization": "Bearer s3cret-token"}
        with open(FIX / "alice.md", "rb") as f:
            r = c.post("/api/inspect", files={"file": ("alice.md", f)}, headers=h)
        assert r.status_code == 201
        assert c.post("/api/books", data={"upload_id": r.json()["upload_id"], "llm": "off"}, headers=h).status_code == 201


# ------------------------------------------------------------------ the page and its static files

@pytest.mark.parametrize("path,ctype", [("/", "text/html"), ("/app.css", "text/css"), ("/app.js", "text/javascript"),
                                        ("/fonts/EBGaramond-VF.woff2", "font/woff2"), ("/fonts/EBGaramond-Italic-VF.woff2", "font/woff2"),
                                        ("/fonts/SourceSans3VF-Upright.woff2", "font/woff2"), ("/art/fresco.webp", "image/webp"),
                                        ("/fonts/OFL-EBGaramond.txt", "text/plain"), ("/fonts/OFL-SourceSans3.txt", "text/plain")])
def test_static_assets_have_the_right_types(client, path, ctype):
    r = client.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith(ctype), r.headers
    assert r.headers["x-content-type-options"] == "nosniff" and len(r.content) > 100


def test_static_routes_refuse_odd_names(client):
    for p in ("/fonts/..%2fapp.js", "/fonts/.hidden", "/fonts/nope.woff2", "/fonts/a%00b", "/art/fresco.webp/x"):
        assert client.get(p).status_code in (404, 400)


def test_page_works_without_a_token_to_ask_for_one(env):
    with TestClient(create_app(token="s3cret-token", engine_factory=fake_factory, host="0.0.0.0"), base_url=LOCAL) as c:
        for p in ("/", "/app.css", "/app.js", "/art/fresco.webp", "/fonts/EBGaramond-VF.woff2"):
            assert c.get(p).status_code == 200


def test_no_external_urls_in_web_files():
    files = [p for p in WEB.rglob("*") if p.is_file() and p.suffix in (".html", ".css", ".js", ".svg")]
    assert {p.name for p in files} >= {"index.html", "app.css", "app.js"}
    for p in files:
        text = p.read_text(encoding="utf-8")
        assert not re.search(r"https?://", text), f"{p.name} contains an external URL"
        assert "googleapis" not in text and "gstatic" not in text and "//cdn" not in text


def test_web_files_are_packaged():
    import tomllib
    pkg = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["bookbuddy"]
    assert {"web/*", "web/fonts/*", "web/art/*"} <= set(pkg)
