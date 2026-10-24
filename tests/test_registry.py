"""Registries: built-ins, entry points (a real dist-info on sys.path), drop-in plugin files, error isolation."""
import importlib
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

from bookbuddy import config, registry
from bookbuddy.book import Book, Chapter
from bookbuddy.extractors import extract_book, find_extractor, supported_extensions, UnsupportedFormat
from bookbuddy.notifiers import BaseNotifier
from bookbuddy.notify import Notifier
from bookbuddy.pipeline import Options, PipelineError, convert
from bookbuddy.registry import Registry
from helpers import SAMPLE_CHAPTERS, make_epub

TXT_PLUGIN = '''
from pathlib import Path
from bookbuddy.book import Book, Chapter

extensions = (".txt",)

def can_handle(path):
    return Path(path).suffix.lower() == ".txt"

def extract(path, opts=None):
    paras = [p.strip() for p in Path(path).read_text().split("\\n\\n") if p.strip()]
    return Book(Path(path).stem, "Plain Author", [Chapter("Text", paras)])
'''


def make_dist(tmp_path, monkeypatch, name, entry_points, modules):
    """Create an installed-looking distribution (dist-info + modules) on sys.path."""
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    d = site / f"{name}-1.0.dist-info"
    d.mkdir()
    (d / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
    (d / "entry_points.txt").write_text(entry_points)
    for fname, src in modules.items():
        (site / fname).write_text(textwrap.dedent(src))
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()
    registry.reset_all()
    return site


def test_builtins_registered():
    registry.ensure_builtins()
    assert {"epub", "pdf"} <= set(registry.extractors.names())
    assert {"kokoro", "fake"} <= set(registry.engines.names())
    assert {"desktop", "status-file"} <= set(registry.notifiers.names())
    assert supported_extensions() >= {".epub", ".pdf"}


def test_extractor_dispatch_and_unsupported(tmp_path):
    assert find_extractor(Path("a.EPUB")).__name__.endswith("epub")
    assert find_extractor(Path("a.pdf")).__name__.endswith("pdf")
    assert find_extractor(Path("a.xyz")) is None
    with pytest.raises(UnsupportedFormat, match=r"\.xyz"):
        extract_book(tmp_path / "a.xyz")
    f = tmp_path / "a.xyz"
    f.write_text("x")
    with pytest.raises(PipelineError, match="unsupported file type"):
        convert(f, Options(out_dir=tmp_path / "o", work_root=tmp_path / "w"))


def test_entry_point_extractor_engine_notifier_are_discovered(tmp_path, monkeypatch):
    make_dist(tmp_path, monkeypatch, "bb-test-plugin", textwrap.dedent('''\
        [bookbuddy.extractors]
        txt = bb_txt_plugin
        [bookbuddy.engines]
        beep = bb_other_plugin:BeepEngine
        [bookbuddy.notifiers]
        memo = bb_other_plugin:MemoNotifier
        [bookbuddy.llm_providers]
        echo = bb_other_plugin:EchoProvider
        '''), {
        "bb_txt_plugin.py": TXT_PLUGIN,
        "bb_other_plugin.py": '''
            import numpy as np
            from bookbuddy.notifiers import BaseNotifier
            from bookbuddy.llm_clean import Provider
            class BeepEngine:
                sample_rate = 24000
                def __init__(self, voice, speed, device=None): pass
                def synth(self, text): return (0.1 * np.sin(np.arange(24000) / 5)).astype("float32")
            SENT = []
            class MemoNotifier(BaseNotifier):
                def send(self, kind, title, message, status): SENT.append((kind, self.settings.get("tag"), status))
            class EchoProvider(Provider):
                name = "echo"
                def run(self, prompt): return prompt
            ''',
    })
    assert {"txt"} <= set(registry.extractors.names()) and "beep" in registry.engines.names()
    assert "memo" in registry.notifiers.names() and "echo" in registry.llm_providers.names()
    assert ".txt" in supported_extensions()

    txt = tmp_path / "story.txt"
    txt.write_text("It was a quiet evening in the valley.\n\nThe river ran slowly past the mill.")
    assert extract_book(txt).author == "Plain Author"

    # the whole pipeline with only plugins: extractor + engine + notifier
    from bb_other_plugin import SENT
    res = convert(txt, Options(out_dir=tmp_path / "out", work_root=tmp_path / "work",
                               engine_factory=lambda v, s, d=None: registry.engines.get("beep")(v, s, d)))
    assert res.path.exists() and res.author == "Plain Author"
    n = Notifier(tmp_path / "o", enabled=["memo"], settings={"memo": {"tag": "T"}})
    n.started("story.txt")
    assert SENT == [("started", "T", "START | story.txt")]


def test_plugin_overrides_builtin_extractor(tmp_path, monkeypatch):
    make_dist(tmp_path, monkeypatch, "bb-pdf2", "[bookbuddy.extractors]\nmypdf = bb_pdf2\n", {"bb_pdf2.py": '''
        from pathlib import Path
        from bookbuddy.book import Book, Chapter
        extensions = (".pdf",)
        def can_handle(p): return Path(p).suffix == ".pdf"
        def extract(p, opts=None): return Book("custom", "me", [Chapter("c", ["x"])])
        '''})
    assert extract_book(tmp_path / "x.pdf").title == "custom"


def test_broken_plugins_are_skipped_not_fatal(tmp_path, monkeypatch, caplog):
    make_dist(tmp_path, monkeypatch, "bb-broken", "[bookbuddy.extractors]\nbad = nonexistent_module_xyz\n"
              "good = bb_good\nraiser = bb_raiser\n", {
        "bb_good.py": TXT_PLUGIN,
        "bb_raiser.py": "extensions = ('.zzz',)\ndef can_handle(p): raise RuntimeError('boom')\ndef extract(p, o=None): pass\n"})
    caplog.set_level("WARNING")
    assert "bad" not in registry.extractors.names() and "good" in registry.extractors.names()
    assert any("nonexistent_module_xyz" in r.message or "bad" in r.message for r in caplog.records)
    f = tmp_path / "a.txt"
    f.write_text("hello world, this is text.")
    assert find_extractor(f).__name__ == "bb_good"           # the raising can_handle did not hide it


def test_drop_in_plugin_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "cfgdir"
    (cfg / "plugins").mkdir(parents=True)
    monkeypatch.setenv("BOOKBUDDY_CONFIG_DIR", str(cfg))
    (cfg / "plugins" / "mine.py").write_text(TXT_PLUGIN + '''
from bookbuddy.notifiers import BaseNotifier
class Loud(BaseNotifier):
    def send(self, kind, title, message, status): print("LOUD", kind)
NOTIFIERS = {"loud": Loud}
''')
    (cfg / "plugins" / "broken.py").write_text("raise SystemExit('nope')")
    (cfg / "plugins" / "_skipped.py").write_text("NOTIFIERS = {'skipped': object}")
    registry.reset_all()
    assert "loud" in registry.notifiers.names() and "skipped" not in registry.notifiers.names()


def test_drop_in_extractor_module_object(tmp_path, monkeypatch):
    cfg = tmp_path / "cfgdir"
    (cfg / "plugins").mkdir(parents=True)
    monkeypatch.setenv("BOOKBUDDY_CONFIG_DIR", str(cfg))
    (cfg / "plugins" / "txt.py").write_text(TXT_PLUGIN + "\nimport sys\nEXTRACTORS = {'txt': sys.modules[__name__]}\n")
    registry.reset_all()
    f = tmp_path / "n.txt"
    f.write_text("One paragraph here.")
    assert extract_book(f).title == "n"
    assert ".txt" in supported_extensions()


def test_notifier_enabled_but_missing_is_logged_not_fatal(tmp_path, caplog):
    caplog.set_level("WARNING")
    n = Notifier(tmp_path, enabled=["status-file", "does-not-exist"])
    n.started("a.epub")
    assert (tmp_path / "STATUS.txt").read_text().strip().endswith("START | a.epub")
    assert any("does-not-exist" in r.message for r in caplog.records)


def test_notifier_plugin_failure_is_swallowed(tmp_path):
    class Bad(BaseNotifier):
        def send(self, *a): raise RuntimeError("down")
    registry.notifiers.register("bad", Bad)
    n = Notifier(tmp_path, enabled=["bad", "status-file"])
    n.failed("a.epub", "oops")
    assert "FAIL | a.epub | oops" in (tmp_path / "STATUS.txt").read_text()


def test_notifiers_from_config_settings(tmp_path):
    got = []

    class Rec(BaseNotifier):
        def send(self, kind, title, message, status): got.append((self.settings, kind))
    registry.notifiers.register("rec", Rec)
    config.set_settings(config.parse_settings({"notifiers": {"enabled": ["rec"], "rec": {"k": "v"}}}, env={}))
    Notifier(tmp_path).started("a")
    assert got == [({"k": "v"}, "started")]


def test_engine_lookup_by_name_and_setting():
    from bookbuddy.engines import make_engine
    e = make_engine("v", 1.0, name="fake")
    assert isinstance(e.synth("hello there"), np.ndarray)
    config.set_settings(config.parse_settings({}, env={"BOOKBUDDY_ENGINE": "fake"}))
    assert type(make_engine("v", 1.0)).__name__ == "FakeEngine"
    with pytest.raises(KeyError, match="no engine named"):
        make_engine("v", 1.0, name="nope")


def test_registry_class_basics():
    r = Registry("thing", "bookbuddy.nothing")
    r.register("a", 1, builtin=True)
    r.register("a", 2)
    r.register("b", 3, builtin=True)
    assert r.get("a") == 2 and r.get("b") == 3 and [n for n, _ in r.items()] == ["a", "b"]
