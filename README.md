# Book Buddy

Turn books and documents into natural-sounding audiobooks, locally and free. EPUB, PDF, TXT, Markdown, HTML, DOCX,
ODT, RTF and FB2 out of the box, plus MOBI/AZW/KFX and friends through Calibre; Kokoro TTS on the GPU (CPU works), ffmpeg for chaptered M4B. Nothing leaves the machine unless you explicitly
turn on the optional text-cleanup pass (PDFs and scans; off by default, see [Privacy](#privacy-ai-cleanup-is-opt-in)) with your own Claude or OpenAI key, or an LLM provider plugin you installed yourself.

This version adds a local server: a library and job queue (SQLite), one worker that owns the GPU, an HTTP API with live
progress, and chapters you can play while the rest of the book is still being voiced. A bare test page ships with it;
the designed page comes later.

## Install
Python 3.11-3.12 (torch/Kokoro do not support newer yet). With uv:

    uv venv --python 3.12 .venv && source .venv/bin/activate
    uv pip install --index-url https://download.pytorch.org/whl/cu128 --extra-index-url https://pypi.org/simple torch   # NVIDIA RTX 50xx needs CUDA 12.8+; other GPUs/CPU: plain `torch`
    uv pip install -e ".[dev]"
    bookbuddy --version

Needs `ffmpeg` and (for PDFs) poppler (`pdftotext`, `pdfinfo`, `pdftoppm`). Kokoro weights download once into the
Hugging Face cache (set `[paths] hf_home` in `config.toml` to put them elsewhere).

## Formats
`bookbuddy formats` lists them and says which work right now (`--json` for machines). Every format becomes the same
chaptered, anchored `Book`, so the rest of the pipeline does not care where the text came from.

| Format | Extensions | How | Optional dependency |
|---|---|---|---|
| EPUB | .epub | ebooklib; TOC chapters, front/back matter stripped | none |
| PDF | .pdf | poppler `pdftotext`, bookmarks/heading detection, optional LLM cleanup; scanned pages are OCR'd (see [OCR](#ocr-scanned-books)) | poppler-utils (+ Tesseract for scans) |
| Plain text | .txt | encoding detection (UTF-8/BOM, charset-normalizer, Windows-1252, Latin-1); Project Gutenberg header/footer stripped; "Chapter N" style headings, else ~3,000-word "Part N" sections | none |
| Markdown | .md, .markdown | headings pick the chapter level (a lone `#` is the title, `##` the chapters); YAML front matter title/author; links keep their text, images, footnotes and code are dropped | none |
| HTML | .html, .htm, .xhtml | main-content extraction (nav, header, footer, aside, scripts, cookie banners dropped); `<h1>/<h2>` chapters; `<title>` and `<meta name=author>` | none |
| Word | .docx | python-docx; Heading styles are chapters; headers, footers, footnotes dropped; first portrait image before any text is the cover | none |
| OpenDocument | .odt | content.xml / meta.xml parsed directly; outline levels are chapters; footnotes dropped | none |
| RTF | .rtf | striprtf, then the plain-text path | none |
| FictionBook 2 | .fb2, .fb2.zip | title-info, `<section>` titles are chapters, cover from `<binary>`, notes body dropped | none |
| Kindle and others | .mobi .azw .azw3 .kfx .prc .lit .pdb .lrf .snb .tcr .cbc | `ebook-convert` converts to EPUB (cached by file hash), then the EPUB reader | **Calibre** (free, calibre-ebook.com; KFX also needs Calibre's KFX Input plugin) |
| Scanned books | image folder, .zip, .cbz, .cbr, .djvu (and scanned PDFs) | OCR with Tesseract (or ocrmypdf for PDFs), then the PDF cleanup | **Tesseract**; .cbr needs unrar / 7-zip / bsdtar; .djvu needs DjVuLibre |

Calibre is GPL, so Book Buddy only runs it as an external program (`ebook-convert` on `PATH`) and never imports it.
Without it these formats fail with "This format needs Calibre (free). Install it from calibre-ebook.com, then try
again." (On Arch, `scripts/install-calibre.sh` runs `sudo pacman -S calibre`.)

**DRM is never removed.** Book Buddy recognises protected files and stops with "This book is DRM-protected. Book Buddy
can't read DRM-protected files." Detected: EPUBs whose `META-INF/encryption.xml` encrypts content (font obfuscation
is fine) or that carry Adobe ADEPT `rights.xml` / Apple FairPlay `sinf.xml`; PDFs that need a password or that poppler
refuses to extract (owner-password-only PDFs that poppler can read are fine); Kindle files whose MOBI header says
encrypted, Topaz books, KFX DRMION containers; and any Calibre failure that mentions DRM.

Text options in `config.toml` (Markdown, HTML, DOCX, ODT):

    [extract]
    code_blocks = "skip"           # or "announce" (reads "Code block omitted.")
    tables = "skip"                # or "read" (each row read as a comma-separated line)

## OCR (scanned books)
Pages that are pictures of text are read automatically, so a scanned PDF "just works"; mixed PDFs OCR only the image
pages. Image folders (natural file-name order), `.zip`, `.cbz`, `.cbr` and `.djvu` always go through Tesseract.

- **Install:** Tesseract plus a language pack (Arch `tesseract tesseract-data-eng`, Debian/Ubuntu `tesseract-ocr`,
  macOS `brew install tesseract`). Optional for better PDF results: `uv pip install -e ".[ocr]"` (ocrmypdf, which also
  needs Ghostscript and qpdf; add `unpaper` and it cleans page images too). `.cbr` needs `unrar` (or 7-zip / bsdtar),
  `.djvu` needs DjVuLibre. A missing tool or language pack stops the job with a message naming what to install.
- **Backends:** `auto` uses ocrmypdf for PDFs when it and its tools are present (deskew, keeps a searchable PDF as
  `searchable.pdf` in the job's work folder and in the cache), otherwise Tesseract on pages rendered at 300 dpi, run in
  parallel on the CPU (the GPU stays free for speech). If ocrmypdf fails in `auto` mode, Tesseract takes over.
- **Cleanup:** the usual header/page-number/hyphenation cleanup, plus OCR fixes (`ﬁ` ligatures, `|` for `I`, stray
  marks and one-letter lines, common misreadings such as `tbe`, broken chapter numerals), then the optional LLM pass.
- **Cache:** OCR text is cached per page by image hash (and the searchable PDF per file), so a resumed job does not
  redo pages. Progress is reported as `ocr_plan` / `ocr_progress` events, so notifications include the OCR time.

      [ocr]
      lang = "eng"                 # Tesseract codes, e.g. "eng+deu"
      backend = "auto"             # auto | ocrmypdf | tesseract
      workers = 0                  # 0 = half the CPU cores, at most 8

  Environment: `BOOKBUDDY_OCR_LANG`, `BOOKBUDDY_OCR_BACKEND`, `BOOKBUDDY_OCR_WORKERS`; flags `--ocr-lang`,
  `--ocr-backend`, `--ocr-workers`. `bookbuddy formats` shows which backend is active.

## Use
    bookbuddy listen book.epub|book.pdf|book.txt|book.docx|... [--voice af_heart] [--speed 1.0] [--format m4b|mp3] [--out DIR]
                                        [--max-chapters N] [--keep-work] [--device cuda|cpu]
                                        [--llm auto|none|PROVIDER] [--local-only] [-v]
    bookbuddy watch [--inbox DIR] [--out DIR] [--once] [--dry-run] ...
    bookbuddy formats [--json]           # what is supported and what works right now
    bookbuddy serve [--host H] [--port 8765] [--token T]   # web page + HTTP API + the worker (see "Server")
    bookbuddy queue [list|cancel ID|retry ID]              # inspect and manage the job queue
    bookbuddy keys [set PROVIDER|remove PROVIDER|status]   # API key for the optional AI cleanup (set turns it on)

Output defaults to `~/BookBuddy/<Title> - <Author>.m4b` (AAC 64 kbps mono, chapters, cover, tags). `--format mp3`
writes a folder of per-chapter MP3s. Output is written to a hidden `.part` name and renamed when complete, so a
sync client never uploads a half-written file. An existing output is renamed `.old-<time>`, never overwritten.

Rerunning after a crash resumes: each synthesized chunk is saved in the work dir; the M4B is only assembled when
every chunk exists; the work dir is removed after success unless `--keep-work`. The work dir also holds `book.json`
(the extracted book with paragraph anchors) and `chunks.json` (which paragraph and character range each audio chunk
voices).

## Server
    bookbuddy serve                      # http://localhost:8765, localhost only, no token needed
    bookbuddy serve --host 0.0.0.0 --token SECRET   # any other address REQUIRES a token (it refuses to start without one)

`serve` runs the single worker (it owns the GPU, voices one book at a time and resumes interrupted jobs on start) and the
HTTP API. While it runs, `bookbuddy watch` and `bookbuddy listen` put their books into the same queue instead of
converting on their own (`listen --now` always converts in the foreground). Only one server runs per data folder.

- **Progressive listening:** every chapter is encoded to its own AAC `.m4a` the moment its last chunk is voiced, and is
  playable straight away (with Range support, so seeking works). The M4B is joined from those same chapter files at the end,
  nothing is synthesized twice. Watched/CLI books still land in the outbox as before.
- **Access:** token via `Authorization: Bearer ...` or the cookie set after entering it once on the page. Compared in
  constant time; CORS is off. Without a token the server only answers requests addressed to localhost and refuses
  cross-origin writes. Uploads are size-limited (`[web] max_upload_mb`, default 1024), checked against the format list, and
  stored under a sanitised name inside the data folder. Deleting a book moves its files to a trash folder in the data folder.
- **LLM cleanup (bring your own key, off by default):** see [Privacy](#privacy-ai-cleanup-is-opt-in). Paste a key in the page
  (written to `config.toml`, mode 0600, shown back only as "set ...last4") or run `bookbuddy keys set anthropic|openai`; either
  turns the cleanup on. Claude is tried first (`claude-haiku-5-5`), then OpenAI; models are overridable with
  `[llm.anthropic] model = ...`. Keys are masked in every log line.
- **Donate link:** `[web] donate_url` (default GitHub Sponsors) is shown on the page.
- **systemd:** `systemd/install-server.sh` installs a user unit that runs `bookbuddy serve` (not installed by default).

| Endpoint | |
|---|---|
| `POST /api/books` | multipart upload (`file`, `voice`, `speed`, `format`, `llm=on\|off`) -> `{book_id, job_id}` |
| `GET /api/books`, `GET /api/books/{id}`, `DELETE /api/books/{id}` | library, one book with chapters/status, move to trash |
| `GET /api/books/{id}/chapters/{n}/audio` | the chapter's m4a (409 until ready; `Range` supported) |
| `GET /api/books/{id}/download?format=m4b\|zip`, `GET /api/books/{id}/cover` | finished book / per-chapter files / cover |
| `GET\|PUT /api/books/{id}/playback`, `GET\|POST /api/books/{id}/bookmarks`, `DELETE .../bookmarks/{bid}` | position and bookmarks |
| `GET /api/jobs`, `POST /api/jobs/{id}/cancel`, `POST /api/jobs/{id}/retry` | the queue |
| `GET /api/events` | Server-Sent Events: `job`, `book`, `chapter`, `progress`, `planned`, `ocr`, `llm`, `done`, `failed`, `cancelled`, `deleted` |
| `GET /api/voices`, `GET /api/voices/{voice}/sample?speed=` | voices with friendly names; a cached "Hear it" sample |
| `GET /api/formats` | the format list with availability |
| `GET\|PUT /api/settings`, `PUT /api/settings/keys`, `DELETE /api/settings/keys/{provider}` | settings (never returns key values) and API keys |
| `POST /api/login`, `GET /api/session` | token login (cookie) and session state |

## Configuration
`~/.config/bookbuddy/config.toml` (platformdirs; `BOOKBUDDY_CONFIG` points at another file). Everything is optional.
Priority: defaults < config.toml < `BOOKBUDDY_*` environment variables < command-line flags.

    voice = "af_heart"
    speed = 1.0
    format = "m4b"                 # or "mp3"
    output_dir = "~/BookBuddy"     # finished audiobooks (the default watch inbox is ~/BookBuddy/inbox)
    engine = "kokoro"

    [paths]                        # defaults: ~/.local/share/bookbuddy/{work,logs}, ~/.cache/bookbuddy
    # work_dir, log_dir, cache_dir, hf_home

    [watch]
    # inbox = "~/BookBuddy/inbox"  outbox = "~/BookBuddy"  poll = 5  settle = 10

    [llm]                          # PDF text cleanup: OFF unless enabled (see Privacy)
    enabled = false                # true when you save a key in Book Buddy, or set it yourself
    provider = "auto"              # auto | none | <provider name>
    order = ["anthropic", "openai"]  # what "auto" tries, in order (a keyed provider without a key is skipped)
    workers = 5
    min_score = 0.5                # sections that already look clean are not sent (0 = send all)
    [llm.<provider>]               # keyword settings passed to that provider
    # [llm.anthropic] api_key = "..."  model = "claude-haiku-5-5"      [llm.openai] api_key = "..."  model = "..."

    [ocr]                          # scanned input (see "OCR" above)
    lang = "eng"
    backend = "auto"               # auto | ocrmypdf | tesseract
    workers = 0                    # 0 = auto

    [web]                          # bookbuddy serve
    host = "127.0.0.1"
    port = 8765
    # token = "..."                (required when host is not localhost)
    max_upload_mb = 1024
    donate_url = "https://github.com/sponsors/reed-and-write"

    [notifiers]
    enabled = ["desktop", "status-file"]
    [notifiers.<name>]             # settings passed to that notifier

## Plugins
Four extension points, each a registry in `bookbuddy/registry.py`. Built-ins register in code; third-party plugins
register through setuptools entry points or as drop-in files.

| Registry | Entry-point group | A plugin is |
|---|---|---|
| extractors | `bookbuddy.extractors` | module/object with `extensions`, `can_handle(path)`, `extract(path, opts) -> Book` |
| engines | `bookbuddy.engines` | class `(voice, speed, device)` with `synth(text) -> float32 array`, `sample_rate` |
| notifiers | `bookbuddy.notifiers` | `BaseNotifier` subclass with `send(kind, title, message, status)` |
| LLM providers | `bookbuddy.llm_providers` | `llm_clean.Provider` subclass with `run(prompt) -> str` |

    # a plugin package's pyproject.toml
    [project.entry-points."bookbuddy.notifiers"]
    my-notifier = "my_pkg.notify:MyNotifier"

Or, with no packaging, drop a `.py` file into `~/.config/bookbuddy/plugins/` that defines any of the dicts
`EXTRACTORS`, `ENGINES`, `NOTIFIERS`, `LLM_PROVIDERS` (name -> object). Plugin files run with your privileges: only
add ones you trust. A plugin that fails to load is logged and skipped. Third-party extractors are asked before the
built-in ones, so a plugin can override a built-in format.

An extractor returns a `Book` (`bookbuddy/book.py`): chapters of paragraphs, each paragraph addressable as
(chapter index, paragraph index) with character offsets, which is what future annotations and read-along will hang on.

## Watcher
Drop any supported book (see Formats) into the inbox folder.

    systemd/install.sh /path/to/bookbuddy     # generates and enables the systemd user unit (no sudo)
    journalctl --user -u bookbuddy-watcher -f
    systemctl --user disable --now bookbuddy-watcher

- Polls the inbox every 5 s; a file is processed only after its size and mtime are unchanged for 10 s and no
  sync/partial temp sibling exists. One book at a time. Books already waiting at start-up are processed.
- Success: source -> `inbox/done/`. Failure: source -> `inbox/failed/` + `<name>.error.txt`. Nothing is ever deleted.
- Start/finish/fail notifications go to the enabled notifiers (desktop and a `STATUS.txt` line by default).
  A notification failure is logged and ignored. `bookbuddy watch --dry-run` prints them instead of sending.

## PDF support
`.pdf` goes through the same chunking/TTS/assembly/checkpoint/watcher code; only extraction differs
(`bookbuddy/extractors/pdf.py`, dispatched in the extractor registry).

1. `pdftotext -bbox-layout` (poppler) gives text in poppler's reading order plus line geometry, so paragraphs
   (indents, gaps) and two-column layouts survive. `pdfinfo` gives title/author (author fallbacks, in order: "Author: X" / "by X" / "Title, by X" / a name under the
   title on the first three pages, then a "Title - Author.pdf" filename, then "Unknown"); page 1 rendered by `pdftoppm` is the cover.
2. Pages with almost no text but a picture on them (scans) are OCR'd first; see [OCR](#ocr-scanned-books). If OCR
   cannot run (no Tesseract) the job fails with a "needs OCR" note in `inbox/failed/` saying what to install.
3. Always-on local cleanup: TOC/copyright/index pages dropped, repeated headers/footers and page numbers removed,
   footnote blocks and their inline marks removed, line-break hyphens joined, lines rebuilt into paragraphs, front/back
   matter stripped. Chapters come from the PDF bookmarks (outline) when present, else heading patterns (Chapter N,
   CHAPTER I, Part N, Prologue..., a 1,2,3 numbered run, all-caps lines), else fixed ~3000-word "Part N" sections.
4. Optional LLM pass (`bookbuddy/llm_clean.py`) for leftovers, through whichever provider plugins are installed
   (`--llm auto` tries `llm.order` in order, then nothing; `--llm none` / `--local-only` skips it). A provider that
   errors is skipped for the rest of the run; the job never fails because of it. Prompts forbid summarising/rewriting;
   output is rejected (the un-LLM'd chunk is kept) when it is under 70% or over 110% of the input length, contains
   commentary, introduces more than ~6% new words, or moves a block of text. Chunks are ~4.5k characters, cached by
   content hash so reruns are free. The provider used is logged, shown in the CLI summary and in the "ready" notification.
   - Only chunks that need it are sent: `needs_cleanup_score` counts leftover-debris signals (digit-only lines, glued
     footnote marks, broken sentences, short fragments, `word- word` hyphen breaks, repeated header lines, table-like
     runs/columns, citation residue) per ~4.5k chars; chunks scoring below `llm.min_score` (default 0.5; `0` =
     send everything) keep the heuristic text. The log reports cached / skipped / sent.
   - Sent chunks run in a thread pool (`llm.workers`, default 5; output order is deterministic, cache writes are
     atomic). A progress line with ETA is logged every `llm.progress_every` (10) chunks.
   - An output rejected as too short/long/commentary is retried once with a stricter instruction, then the heuristic
     text is kept. If 6 retries in a run all fail, retrying is switched off for that run (no retry storms).
   - Events through `opts.on_event`: `llm_plan` (chunks, cached, skipped, to_send, workers, provider, est_seconds) and
     `llm_progress` (done, total, eta_seconds). The watcher turns `llm_plan` into a "PDF cleanup first: N of M sections,
     about X min, then voicing" plan message (cleanup runs before the normal voicing plan, which follows it).
   Settings: the `[llm]` table (`provider`, `order`, `timeout`, `workers`, `min_score`, `progress_every`).

### Privacy: AI cleanup is opt-in
The AI cleanup pass is **off by default** (`[llm] enabled = false`). With it off, nothing you convert is ever sent anywhere,
whatever else is on your machine: an `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` that merely sits in your environment is **not**
used and does not switch anything on. It turns on only when you do one of these on purpose:

- save a key through Book Buddy itself: the settings page (`PUT /api/settings/keys`) or `bookbuddy keys set anthropic|openai`
  (both also set `enabled = true`);
- set `enabled = true` under `[llm]` in `config.toml` (or `PUT /api/settings` with `{"llm_enabled": true}`); keys from the
  environment are then used too;
- name a provider for one run: `bookbuddy listen book.pdf --llm anthropic`.

`GET /api/settings` shows `llm.enabled`. `--local-only` (or `--llm none`) forces the pass off for that run, even when it is
enabled. Local plugin providers you install yourself follow the same switch. When the pass runs, the extracted text of a PDF
or scan is sent to the provider you chose; EPUB and other text formats never contact any model.

Test PDFs are rebuilt with `python tests/make_pdf_fixtures.py` and the other formats' fixtures with
`python tests/make_format_fixtures.py` (dev extras: reportlab, odfpy; public-domain Alice text).

## Develop
    source .venv/bin/activate && pytest        # ~340 tests, ~2 min, no GPU needed (fake TTS engine)

Layout: `bookbuddy/{book,registry,config,config_edit,pipeline,jobs,intake,worker,server,voices,watcher,cli,notify,llm_clean,llm_providers,textnorm,logs}.py`, `bookbuddy/library/`, `bookbuddy/web/`,
`bookbuddy/extractors/{epub,pdf,txt,markdown,html,docx,odt,rtf,fb2,calibre,images,cbz,cbr,djvu,scanned,drm,common}.py`, `bookbuddy/ocr/`, `bookbuddy/engines/{kokoro,fake}.py`, `bookbuddy/notifiers/{desktop,statusfile}.py`,
`systemd/`, `tests/`. Text rules (abbreviations, years, ordinals, currency, footnote markers) live in `textnorm.py`.
Tests never read your real config: `tests/conftest.py` points the config/data/cache dirs at a temp folder.

License: MIT.

## Contributors
Book Buddy is maintained by @reed-and-write; contributions welcome, contributors keep copyright of their work under MIT.
