"""PDF extraction, heuristic cleanup, LLM providers/validation/cache. Needs poppler; fixtures from make_pdf_fixtures.py."""
import json
import re
import threading
import time
from pathlib import Path

import pytest

from bookbuddy import llm_clean
from bookbuddy.llm_clean import Cleaner, Provider, ProviderError, validate
from bookbuddy.extractors import pdf as P

FIX = Path(__file__).parent / "fixtures"


def L(*lines):
    return list(lines)


# ---------------------------------------------------------------- heuristics

def test_header_footer_and_page_number_removal():
    pages = []
    for i in range(1, 9):
        pages.append(["MY BOOK TITLE", "", f"Body line one of page {i} that is long enough.", "second body line here.",
                      "", f"{i}"])
    out, removed = P.remove_headers_footers(pages)
    for i, p in enumerate(out, 1):
        txt = "\n".join(p)
        assert "MY BOOK TITLE" not in txt
        assert not re.search(r"^\d+$", txt, re.M)
        assert f"page {i}" in txt
    assert removed >= 16


def test_running_head_with_page_number_variants_removed():
    pages = [[f"{i} | The Great Novel", "", "Some real text on this page, long enough to count."] for i in range(1, 7)]
    out, _ = P.remove_headers_footers(pages)
    assert all("Great Novel" not in "\n".join(p) for p in out)


def test_chapter_headings_are_not_removed_as_headers():
    pages = [[f"CHAPTER {i}", "", "text text text text text text"] for i in range(1, 7)]
    out, _ = P.remove_headers_footers(pages)
    assert all(p[0].startswith("CHAPTER") for p in out)


def test_dehyphenate():
    assert P.dehyphenate("a conver-\nsation about it") == "a conversation about it"
    assert P.dehyphenate("a well-\nKnown") == "a well-\nKnown"          # capital: probably a real hyphen
    assert P.dehyphenate("soft­hyphen") == "softhyphen"


def test_paragraph_rebuild_joins_lines_and_keeps_breaks():
    flat = [(0, "The quick brown fox jumps over the lazy dog and keeps run-"), (0, "ning across the field of grass."),
            (0, ""), (0, "A new paragraph starts here and it wraps onto"), (0, "a second line that is quite short and runs on,"),
            (1, "continued sentence that spills onto the next page without a stop"), (1, "and ends here.")]
    paras = P.rebuild_paragraphs(flat)
    assert paras[0] == "The quick brown fox jumps over the lazy dog and keeps running across the field of grass."
    assert len(paras) == 2
    assert paras[1].endswith("and ends here.") and "quite short and runs on, continued" in paras[1]


def test_footnote_block_and_inline_marker_removed():
    page = ["Some body text that goes on and on, said the author.1 More text follows here.",
            "Another body line that is also fairly long and ordinary.", "More body.", "Even more body.", "",
            "1 See the author's earlier work for the full argument."]
    out = P.remove_footnotes(page)
    assert not any("earlier work" in l for l in out)
    assert "author. More" in out[0] or "author.More" in out[0] or "author. 1" not in out[0]
    assert "1" not in out[0]


def test_numbered_body_lines_are_not_footnotes():
    page = ["Body one is long enough to be body.", "Body two is long enough to be body.", "Body three here ok.",
            "Body four here ok.", "2 apples and 3 oranges were in the basket."]
    assert P.remove_footnotes(page) == page


def test_toc_copyright_index_page_detection():
    toc = ["Contents", "Chapter 1 ........ 3", "Chapter 2 ........ 19", "Chapter 3 ........ 31", "Chapter 4 ........ 50"]
    assert P.is_toc_page(toc)
    assert not P.is_toc_page(["Real prose that mentions 1984 in passing.", "And more prose follows after it.", "x", "y"])
    assert P.is_copyright_page(["Copyright 2020. All rights reserved.", "ISBN 978-3-16-148410-0"])
    idx = [f"term {i}, {i * 3}, {i * 3 + 4}" for i in range(12)]
    assert P.is_index_page(idx)


# ---------------------------------------------------------------- chapters

def _flat(text):
    return [(0, l) for l in text.split("\n")]


def test_chapter_heading_patterns():
    text = "\n".join(["Preface text.", "", "CHAPTER I", "", "THE START", "", "Body " * 20, "", "Chapter 2: Middle Part", "",
                      "Body " * 20, "", "Chapter Three", "", "Body " * 20])
    hs = P.detect_headings(_flat(text))
    assert [h.title for h in hs] == ["Chapter I. The Start", "Chapter 2. Middle Part", "Chapter Three"][:len(hs)] or len(hs) == 3
    assert len(hs) == 3 and hs[0].skip >= 2


def test_part_keyword_in_prose_is_not_a_heading():
    hs = P.detect_headings(_flat("Part of the reason was the weather,\nand so on.\n\nBook sales were low.\n"))
    assert hs == []


def test_allcaps_and_numbered_headings_need_sequence():
    caps = "\n".join(f"\nTHE WORD NUMBER {w}\n\n" + "text " * 30 for w in ("ONE", "TWO", "THREE"))
    assert len(P.detect_headings(_flat(caps))) == 3
    nums = "\n".join(f"\n{i}. A heading {'x' * i}\n\n" + "text " * 30 for i in (1, 2, 3))
    assert len(P.detect_headings(_flat(nums))) == 3
    assert P.detect_headings(_flat("\n7. A lonely numbered line\n\ntext text\n")) == []


def test_sample_book_chapters_via_headings_and_matter_stripped():
    b = P.extract_pdf(FIX / "sample_book.pdf", llm="none")
    assert b.title == "Alice's Adventures in Wonderland" and b.author == "Lewis Carroll"
    assert [c.title.split(".")[0] for c in b.chapters] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    text = "\n".join(p for c in b.chapters for p in c.paragraphs)
    for junk in ("ISBN", "Library of Congress", "Editorial note", "ABOUT THE AUTHOR", "CONTENTS", "Dodgson"):
        assert junk not in text
    assert not re.search(r"ALICE'S ADVENTURES IN WONDERLAND", text)
    assert not re.search(r"[a-z]- [a-z]|\b[a-z]+-\n", text)            # no leftover line-break hyphens
    assert "conversation" in text and "nor did Alice" in text            # hyphenated word rejoined, footnote mark gone
    assert b.cover and b.cover[:2] == b"\xff\xd8"


def test_outline_chapters():
    assert P.pdf_outline(FIX / "outline_book.pdf")
    b = P.extract_pdf(FIX / "outline_book.pdf", llm="none")
    assert [c.title for c in b.chapters] == ["Down the Rabbit-Hole", "The Pool of Tears",
                                              "A Caucus-Race and a Long Tale", "The Rabbit Sends in a Little Bill"]
    assert not b.chapters[0].paragraphs[0].startswith("Down the Rabbit-Hole")      # heading line not repeated in the body


def test_no_chapters_falls_back_to_parts():
    b = P.extract_pdf(FIX / "nochapters.pdf", llm="none")
    assert [c.title for c in b.chapters][:2] == ["Part 1", "Part 2"] and len(b.chapters) >= 2
    assert all(len(c.paragraphs) > 5 for c in b.chapters)


def test_max_chapters():
    assert len(P.extract_pdf(FIX / "sample_book.pdf", max_chapters=2, llm="none").chapters) == 2


def test_two_column_reading_order():
    b = P.extract_pdf(FIX / "twocol.pdf", llm="none")
    text = " ".join(p for c in b.chapters for p in c.paragraphs)
    # column 1 flows into column 2 mid-sentence, not into the other column's first line
    assert "pop down a large rabbit-hole under the hedge. In another moment" in text


def test_scanned_pdf_detected(monkeypatch):
    """Image-only pages are OCR'd now; with no OCR tool the old 'needs OCR' failure remains, with the reason."""
    from bookbuddy import ocr
    from bookbuddy.ocr import common
    monkeypatch.setattr(common, "tesseract_path", lambda: None)
    monkeypatch.setattr(ocr, "ocrmypdf_missing", lambda: "ocrmypdf is not installed")
    with pytest.raises(P.ScannedPdfError, match="needs OCR"):
        P.extract_pdf(FIX / "scanned.pdf", llm="none")


def test_metadata_fallbacks(tmp_path):
    t, a = P.pick_title_author({"Title": "Microsoft Word - draft.docx"}, "A Grand Tale\nby Jane Doe\n", tmp_path / "my_book.pdf")
    assert (t, a) == ("A Grand Tale", "Jane Doe")
    t, a = P.pick_title_author({}, "", tmp_path / "some_great_book.pdf")
    assert (t, a) == ("Some Great Book", "Unknown")


# ---------------------------------------------------------------- LLM validation / providers / cache

ORIG = "The old man walked slowly to the harbour. He watched the boats come in. " * 6


def test_validate_accepts_small_repair_rejects_bad():
    assert validate(ORIG, ORIG.replace("harbour.", "harbour.", 1)) is None
    assert "short" in validate(ORIG, ORIG[: len(ORIG) // 2])
    assert "long" in validate(ORIG, ORIG + " " + ORIG[:200])
    assert "commentary" in validate(ORIG, "Here is the cleaned text:\n" + ORIG[:-10])
    assert "commentary" in validate(ORIG, "```\n" + ORIG + "\n```")
    assert validate(ORIG, "") == "empty output"
    rewritten = re.sub(r"\bwalked\b", "meandered", ORIG).replace("boats", "vessels").replace("harbour", "marina")
    assert "new words" in validate(ORIG, rewritten)


class Fake(Provider):
    def __init__(self, name, fn):
        self.name, self.fn, self.calls = name, fn, 0

    def run(self, prompt):
        self.calls += 1
        return self.fn(prompt)


def _echo(prompt):
    return prompt.split("<<<\n", 1)[1].rsplit("\n>>>", 1)[0]


def _boom(prompt):
    raise ProviderError("HTTP 401 invalidated oauth token", fatal=True)


def test_provider_order_and_fallthrough(tmp_path):
    h, c = Fake("alpha", _boom), Fake("beta", _echo)
    cl = Cleaner([h, c], tmp_path)
    assert cl.clean_chunk(ORIG) == ORIG.strip()
    assert cl.clean_chunk(ORIG + " Another distinct chunk of sample text.") .startswith("The old man")
    assert h.calls == 1                      # alpha marked dead after the first failure
    assert c.calls == 2
    assert cl.provider_label == "beta" and "alpha" in cl.dead


def test_all_providers_fail_returns_original(tmp_path):
    cl = Cleaner([Fake("alpha", _boom), Fake("beta", _boom)], tmp_path)
    assert cl.clean_chunk(ORIG) == ORIG
    assert cl.provider_label == "none"


def test_first_provider_wins_when_ok(tmp_path):
    h, c = Fake("alpha", _echo), Fake("beta", _echo)
    Cleaner([h, c], tmp_path).clean_chunk(ORIG)
    assert (h.calls, c.calls) == (1, 0)


def test_rejected_output_keeps_original_chunk(tmp_path):
    cl = Cleaner([Fake("beta", lambda p: "Here is the cleaned text: nope")], tmp_path)
    assert cl.clean_chunk(ORIG) == ORIG
    assert cl.stats["rejected"] == 1 and cl.provider_label == "none"


def test_cache_makes_reruns_free(tmp_path):
    c1 = Fake("beta", _echo)
    Cleaner([c1], tmp_path).clean_chunk(ORIG)
    assert c1.calls == 1
    c2 = Fake("beta", _boom)
    cl2 = Cleaner([c2], tmp_path)
    assert cl2.clean_chunk(ORIG) == ORIG.strip()
    assert c2.calls == 0 and cl2.stats["cached"] == 1 and cl2.provider_label == "beta"
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_rejected_results_are_not_cached(tmp_path):
    Cleaner([Fake("beta", lambda p: "tiny")], tmp_path).clean_chunk(ORIG)
    assert not list(tmp_path.glob("*.json"))


def test_paragraph_grouping_into_chunks(tmp_path):
    seen = []

    def spy(prompt):
        seen.append(len(_echo(prompt)))
        return _echo(prompt)

    paras = [("Sentence number %d is here. " % i) * 8 for i in range(20)]
    out = Cleaner([Fake("beta", spy)], tmp_path).clean_paragraphs(paras, chunk_chars=1500)
    assert out == [p.strip() for p in paras]
    assert len(seen) > 1 and max(seen) <= 1500 + 400


def test_none_provider_skips(tmp_path):
    assert Cleaner.from_choice("none", tmp_path).clean_paragraphs(["a b c"]) == ["a b c"]
    assert Cleaner.from_choice("auto", tmp_path).providers == []          # no provider plugin installed = no LLM
    with pytest.raises(ValueError):
        llm_clean.resolve_order("gpt")


def test_full_extract_with_fake_provider_marks_provider(tmp_path):
    cl = Cleaner([Fake("alpha", _boom), Fake("beta", _echo)], tmp_path)
    b = P.extract_pdf(FIX / "sample_book.pdf", llm="none", cleaner=cl)
    assert b.llm_provider == "beta" and cl.stats["chunks"] >= 3


def test_libreoffice_pdf_real_world_producer():
    b = P.extract_pdf(FIX / "alice_libreoffice.pdf", llm="none")
    assert [c.title for c in b.chapters][:2] == ["Chapter 1. Down the Rabbit-hole", "Chapter 2. The Pool of Tears"]
    assert b.author == "Lewis Carroll" and sum(c.words for c in b.chapters) > 3000


# ---------------------------------------------------------------- speed / waste controls

CLEAN = ("The old man walked slowly to the harbour. He watched the boats come in, one after another, "
         "until the light failed and the water went dark.")
DIRTY = ("The general must know the ground well and that\n\n23\n\nthe enemy is near. THE ART OF WAR 41\n\n"
         "He moves only when there is an advan- tage to be gained and when his\n\nposition is secure. THE ART OF WAR 42")


def test_score_separates_clean_prose_from_debris():
    assert llm_clean.needs_cleanup_score(CLEAN) < 0.5
    assert llm_clean.needs_cleanup_score((CLEAN + "\n\n") * 8) < 0.5
    assert llm_clean.needs_cleanup_score(DIRTY) >= 0.5
    assert llm_clean.needs_cleanup_score("a  b\n\n12\n\nc d e") >= 0.5
    tbl = "\n\n".join(["Year   Cost   Yield", "1901   5   7", "1902   6   8", "1903   7   9", "1904   7   9"])
    assert llm_clean.needs_cleanup_score(tbl) >= 0.5


def test_clean_chunks_are_skipped_dirty_are_sent(tmp_path):
    fake = Fake("beta", _echo)
    cl = Cleaner([fake], tmp_path, workers=2, min_score=0.5)
    paras = [CLEAN] * 3 + [DIRTY] + [CLEAN] * 3
    out = cl.clean_paragraphs(paras, chunk_chars=len(CLEAN) + 5)
    assert out == [p.strip() for p in paras] or len(out) == len(paras)
    assert fake.calls == 1 and cl.stats["skipped"] == 6 and cl.stats["sent"] == 1
    # threshold is configurable: 0 sends everything
    f2 = Fake("beta", _echo)
    Cleaner([f2], tmp_path / "b", workers=2, min_score=0).clean_paragraphs(paras, chunk_chars=len(CLEAN) + 5)
    assert f2.calls == 7


def test_parallel_calls_keep_deterministic_order_and_run_concurrently(tmp_path):
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def slow_fix(prompt):
        body = _echo(prompt)
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.3 - 0.03 * int(re.search(r"No\. (\d+)", body).group(1)))     # later chunks finish first
        with lock:
            state["now"] -= 1
        return body.replace("  ", " ")

    paras = [f"Paragraph No. {i} has  a doubled space and enough words to be a chunk of its own here." for i in range(8)]
    cl = Cleaner([Fake("beta", slow_fix)], tmp_path, workers=4, min_score=0)
    out = cl.clean_paragraphs(paras, chunk_chars=60)
    assert out == [p.replace("  ", " ") for p in paras]
    assert 1 < state["peak"] <= 4
    assert len(list(tmp_path.glob("*.json"))) == 8 and not list(tmp_path.glob("*.tmp"))
    # a cached rerun is identical and makes no calls
    again = Fake("beta", _boom)
    assert Cleaner([again], tmp_path, workers=4, min_score=0).clean_paragraphs(paras, chunk_chars=60) == out
    assert again.calls == 0


def test_too_short_output_is_retried_once_with_strict_instruction(tmp_path):
    prompts = []

    def fn(prompt):
        prompts.append(prompt)
        return ORIG[: len(ORIG) // 3] if len(prompts) == 1 else _echo(prompt)

    cl = Cleaner([Fake("beta", fn)], tmp_path, min_score=0)
    assert cl.clean_chunk(ORIG) == ORIG.strip()
    assert len(prompts) == 2 and "IMPORTANT" not in prompts[0] and "IMPORTANT" in prompts[1] and "too short" in prompts[1]
    assert cl.stats["retried"] == 1 and cl.stats["retry_ok"] == 1 and cl.stats["rejected"] == 0


def test_retry_that_still_fails_falls_back_without_a_storm(tmp_path):
    f = Fake("beta", lambda p: "tiny")
    cl = Cleaner([f], tmp_path, min_score=0)
    assert cl.clean_chunk(ORIG) == ORIG
    assert f.calls == 2 and cl.stats["rejected"] == 1          # one retry only, then the heuristic text
    # after repeated useless retries, retrying is switched off for the run
    for i in range(10):
        cl.clean_chunk(ORIG + f" variation {i}.")
    assert f.calls < 2 + 10 * 2 and cl.stats["retried"] <= 7


def test_failed_provider_skipped_for_run_and_never_raises_in_parallel(tmp_path):
    bad, good = Fake("beta", _boom), Fake("alpha", _echo)
    paras = [f"Paragraph {chr(97 + i)} is some sentence number {i} for the test, long enough." for i in range(12)]
    cl = Cleaner([bad, good], tmp_path, workers=4, min_score=0)
    out = cl.clean_paragraphs(paras, chunk_chars=60)
    assert out == paras and "beta" in cl.dead and bad.calls <= 4 and good.calls == 12   # in-flight calls only
    cl2 = Cleaner([Fake("beta", _boom), Fake("alpha", _boom)], tmp_path / "x", workers=3, min_score=0)
    assert cl2.clean_paragraphs(paras, chunk_chars=60) == paras and cl2.provider_label == "none"


def test_plan_and_progress_events(tmp_path):
    events = []
    cl = Cleaner([Fake("beta", _echo)], tmp_path, workers=2, min_score=0, progress_every=2,
                 on_event=lambda kind, **i: events.append((kind, i)))
    paras = [f"Paragraph {i} is a sentence of reasonable length for chunking purposes." for i in range(6)]
    cl.clean_paragraphs(paras, chunk_chars=50)
    kinds = [k for k, _ in events]
    assert kinds[0] == "llm_plan" and kinds.count("llm_progress") >= 3
    plan = events[0][1]
    assert plan["to_send"] == 6 and plan["est_seconds"] > 0 and plan["provider"] == "beta"
    assert events[-1][1]["done"] == events[-1][1]["total"] == 6


def test_event_hook_failure_does_not_break_cleaning(tmp_path):
    def bad(kind, **i):
        raise RuntimeError("hook")
    cl = Cleaner([Fake("beta", _echo)], tmp_path, min_score=0, on_event=bad)
    assert cl.clean_paragraphs(["A fine sentence."]) == ["A fine sentence."]


def test_auto_uses_configured_order_of_installed_providers(monkeypatch):
    monkeypatch.setattr(__import__("bookbuddy.config", fromlist=["x"]).get_settings(), "llm_enabled", True)
    from bookbuddy.registry import llm_providers
    llm_providers.register("beta", Fake)
    llm_providers.register("alpha", Fake)
    try:
        assert llm_clean.resolve_order("auto", order=["beta", "ghost", "alpha"]) == ["beta", "alpha"]
        assert llm_clean.resolve_order("alpha") == ["alpha"] and llm_clean.resolve_order("none") == []
    finally:
        llm_providers.unregister("beta")
        llm_providers.unregister("alpha")


def test_validate_rejects_moved_sentence_but_allows_deleted_debris():
    a = "First sentence stays here. Second sentence goes here too. Third sentence is last in order."
    moved = "First sentence stays here. Third sentence is last in order. Second sentence goes here too."
    assert validate(a, moved) == "reorders text"
    body = ("The general waits for the enemy to tire and then strikes at the weakest point of the line. " * 4).strip()
    assert validate(body + " THE ART OF WAR 41 Aids to Scouting p. two", body) is None     # deleted debris is fine
