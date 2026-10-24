"use strict";
/* Book Buddy page. Plain JS, no build step, no external requests. Talks only to this server's /api. */
(() => {
const $ = id => document.getElementById(id);
const body = document.body, frame = $("frame");
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)");

/* ------------------------------------------------------------------ small helpers */
function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "on") Object.entries(v).forEach(([ev, f]) => e.addEventListener(ev, f));
    else if (k === "class") e.className = v;
    else if (k === "dataset") Object.assign(e.dataset, v);
    else if (k === "attrs") Object.entries(v).forEach(([a, x]) => e.setAttribute(a, x));
    else e[k] = v;
  }
  kids.flat().forEach(k => k != null && k !== false && e.append(k));
  return e;
}
const store = {
  get(k, d) { try { const v = localStorage.getItem("bb." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("bb." + k, JSON.stringify(v)); } catch { /* private window: fine */ } },
};
class AuthError extends Error {}
async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  if (r.status === 401) { showLogin(); throw new AuthError("login needed"); }
  if (!r.ok) {
    let m = r.statusText;
    try { const j = await r.json(); m = typeof j.detail === "string" ? j.detail : m; } catch { /* keep status text */ }
    const e = new Error(m); e.status = r.status; throw e;
  }
  return r.status === 204 ? null : r.json();
}
const jsonOpts = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function fmtHM(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h} h ${String(m).padStart(2, "0")} m` : `${Math.max(1, m)} min`;
}
function fmtAbout(sec) {
  sec = Math.round((sec || 0) / 60) * 60;
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `about ${h} h ${String(m).padStart(2, "0")} m` : `about ${Math.max(1, m)} min`;
}
function fmtLeft(sec) {
  if (!sec || sec < 0) return "";
  if (sec < 90) return "about a minute left";
  if (sec < 3600) return `about ${Math.round(sec / 60)} min left`;
  return `about ${Math.floor(sec / 3600)} h ${Math.round((sec % 3600) / 60)} min left`;
}
function clock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
let toastTimer = null;
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
}
function swallow(e) { if (!(e instanceof AuthError)) toast(e.message || "Something went wrong."); }

/* ------------------------------------------------------------------ covers */
function coverEl(b, size) {
  const spine = () => el("div", { class: `cover ${size} c${(b.id || 0) % 4}`, textContent: b.title || "" });
  if (!b.has_cover) return spine();
  const img = el("img", { class: `cover ${size}`, src: `/api/books/${b.id}/cover`, alt: "", loading: "lazy" });
  img.addEventListener("error", () => img.replaceWith(spine()));
  return img;
}

/* ------------------------------------------------------------------ state */
const S = {
  books: [], voices: [], settings: null, formats: [], session: null,
  detail: null,            // the open book's /api/books/{id}
  detailId: null, bookmarks: [],
  ready: false,
};
const voiceById = id => S.voices.find(v => v.id === id);
const voiceName = id => (voiceById(id) || { name: id }).name;
const voiceLabel = v => `${v.name} · ${v.style ? v.style + ", " : ""}${v.accent}`;
const SPEEDS = [0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0];
const speedText = s => `${Number(s).toFixed(s % 1 === 0 || (s * 10) % 1 === 0 ? 1 : 2)}×`;
const FORMAT_LABEL = { m4b: "Audiobook (M4B)", mp3: "MP3 files" };
const ACTIVE = new Set(["queued", "extracting", "ocr", "cleaning", "voicing", "assembling"]);

/* ------------------------------------------------------------------ panels: collapse, vanish at the top, snap to the next */
function visibleCol() { return [...frame.querySelectorAll(".col")].find(c => c.offsetParent !== null); }
let lastTop = 0, snapping = false;
function hideGone() {
  const top = frame.getBoundingClientRect().top, down = frame.scrollTop > lastTop;
  lastTop = frame.scrollTop;
  const col = visibleCol();
  frame.querySelectorAll(".panel").forEach(p => {
    if (!col || !col.contains(p)) { p.classList.remove("gone"); return; }
    const r = p.getBoundingClientRect(), gone = r.height > 0 && r.top < top - Math.min(60, r.height * 0.25);
    if (gone && !p.classList.contains("gone") && down && !snapping) {
      const next = p.nextElementSibling;
      if (next && next.classList.contains("panel")) {
        snapping = true;
        frame.scrollTo({ top: next.offsetTop - col.offsetTop, behavior: reduceMotion.matches ? "instant" : "smooth" });
        setTimeout(() => { snapping = false; }, 450);
      }
    }
    p.classList.toggle("gone", gone);
  });
}
function padEnd() {            // room under the last panel so every panel can reach the top of the band
  frame.querySelectorAll(".col").forEach(c => {
    const last = c.querySelector(":scope > .panel:last-child");
    if (last) c.style.paddingBottom = Math.max(14, frame.clientHeight - last.offsetHeight) + "px";
  });
}
function relayout() { padEnd(); hideGone(); }
const chev = '<svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 8l7 8 7-8" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
function makeCollapsible() {
  frame.querySelectorAll(".panel > h2").forEach(h => {
    h.insertAdjacentHTML("beforeend", `<span class="chev">${chev}</span>`);
    h.tabIndex = 0; h.setAttribute("role", "button");
    const panel = h.parentElement;
    h.setAttribute("aria-expanded", "true");
    const toggle = () => {
      panel.classList.toggle("collapsed");
      h.setAttribute("aria-expanded", String(!panel.classList.contains("collapsed")));
      relayout();
    };
    h.addEventListener("click", toggle);
    h.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
  });
}
frame.addEventListener("scroll", hideGone, { passive: true });
addEventListener("resize", relayout);

/* ------------------------------------------------------------------ routing */
function route() {
  const m = /^#book\/(\d+)$/.exec(location.hash);
  if (S.session && S.session.auth_required && !S.session.authenticated) { setView("login"); return; }
  if (m) { openBook(+m[1]); } else { setView("home"); }
}
function setView(v) {
  body.classList.remove("view-home", "view-book", "view-login");
  body.classList.add("view-" + v);
  frame.scrollTop = 0; lastTop = 0;
  requestAnimationFrame(relayout);
}
addEventListener("hashchange", route);

/* ------------------------------------------------------------------ login (token mode) */
function showLogin() { S.ready = false; setView("login"); }
$("loginForm").addEventListener("submit", async ev => {
  ev.preventDefault();
  const err = $("loginErr"); err.hidden = true;
  try {
    await api("/api/login", jsonOpts("POST", { token: $("tokenInput").value }));
    $("tokenInput").value = "";
    await start();
  } catch (e) { err.textContent = (e instanceof AuthError || e.status === 401) ? "That token is not right. Check it and try again." : "Could not reach Book Buddy."; err.hidden = false; }
});

/* ------------------------------------------------------------------ shelf */
function statusLine(b) {
  const j = b.job, total = b.chapters_total || 0, ready = b.chapters_ready || 0;
  if (b.status === "ready" || (j && j.state === "done")) {
    const parts = [];
    if (total) parts.push(plural(total, "chapter"));
    if (b.total_duration) parts.push(fmtHM(b.total_duration));
    if (b.total_duration && b.listened > 0) parts.push(`${Math.max(1, Math.min(100, Math.round(b.listened / b.total_duration * 100)))}% listened`);
    return parts.join(" · ") || "Ready";
  }
  if (b.status === "failed" || (j && j.state === "failed")) return "Something went wrong" + (b.error ? " · " + b.error : "");
  if (b.status === "cancelled" || (j && j.state === "cancelled")) return "Cancelled";
  const st = j ? j.state : "queued";
  switch (st) {
    case "queued": return "Waiting";
    case "extracting": return "Reading the book";
    case "ocr": return "Reading the scanned pages";
    case "cleaning": return "Cleaning up the text";
    case "assembling": return "Putting the audiobook together";
    case "voicing": {
      const left = fmtLeft(j.eta);
      return ["Voicing", total ? `${ready} of ${total} chapters ready` : "", left].filter(Boolean).join(" · ");
    }
    default: return "Working";
  }
}
let openMenu = null;
function closeMenu() { if (openMenu) { openMenu.remove(); openMenu = null; } }
document.addEventListener("click", e => { if (openMenu && !openMenu.contains(e.target) && !e.target.closest(".more")) closeMenu(); });
function shelfMenu(b, anchor) {
  closeMenu();
  const j = b.job, items = [];
  if (j && ACTIVE.has(j.state)) items.push(["Cancel", async () => { await api(`/api/jobs/${j.id}/cancel`, { method: "POST" }); }]);
  if (j && (j.state === "failed" || j.state === "cancelled")) items.push(["Try again", async () => { await api(`/api/jobs/${j.id}/retry`, { method: "POST" }); }]);
  if (!(j && j.state !== "queued" && ACTIVE.has(j.state))) {
    items.push(["Delete", async () => {
      if (!confirm(`Move "${b.title}" to the trash?`)) return;
      await api(`/api/books/${b.id}`, { method: "DELETE" });
    }, true]);
  }
  const menu = el("div", { class: "menu", attrs: { role: "menu" } }, items.map(([label, fn, danger]) =>
    el("button", { type: "button", textContent: label, class: danger ? "danger" : "", attrs: { role: "menuitem" },
      on: { click: async () => { closeMenu(); try { await fn(); await loadBooks(); } catch (e) { swallow(e); } } } })));
  anchor.parentElement.append(menu); openMenu = menu;
  menu.querySelector("button").focus();
}
function renderShelf() {
  const shelf = $("shelf");
  $("shelfLoading").hidden = true;
  $("shelfEmpty").hidden = S.books.length > 0;
  closeMenu();
  shelf.replaceChildren(...S.books.map(b => {
    const j = b.job, active = j && ACTIVE.has(j.state) && b.status !== "ready";
    const failed = b.status === "failed";
    const t = el("div", { class: "t" }, el("b", { textContent: b.title, title: b.title }),
      el("div", { class: "muted" + (failed ? " err-line" : ""), textContent: statusLine(b) }));
    if (active && j.state === "voicing") t.append(el("div", { class: "prog" }, el("i", { style: `width:${Math.round((j.progress || 0) * 100)}%` })));
    const more = el("button", { class: "more", type: "button", title: "More", attrs: { "aria-label": `More for ${b.title}`, "aria-haspopup": "menu" },
      textContent: "⋯", on: { click: () => shelfMenu(b, more) } });
    const canOpen = b.chapters_ready > 0;
    return el("div", { class: "item", attrs: { role: "listitem" } }, coverEl(b, "sm"), t,
      canOpen ? el("a", { class: "btn s", href: `#book/${b.id}`, textContent: "Open", attrs: { "aria-label": `Open ${b.title}` } }) : null, more);
  }));
  relayout();
}
async function loadBooks() {
  S.books = await api("/api/books");
  renderShelf();
  if (S.detailId != null && body.classList.contains("view-book")) loadDetail(S.detailId, false);
}
let refreshTimer = null;
function refreshSoon() { clearTimeout(refreshTimer); refreshTimer = setTimeout(() => loadBooks().catch(() => {}), 300); }

/* ------------------------------------------------------------------ live events (SSE), reconnecting */
let es = null, esTimer = null;
function listen() {
  if (es) es.close();
  es = new EventSource("/api/events");
  es.addEventListener("hello", () => { if (S.ready) loadBooks().catch(() => {}); });
  for (const ev of ["job", "book", "chapter", "done", "failed", "cancelled", "deleted", "planned", "ocr", "llm"]) es.addEventListener(ev, refreshSoon);
  es.addEventListener("progress", e => {
    let d; try { d = JSON.parse(e.data); } catch { return; }
    const b = S.books.find(x => x.id === d.book_id);
    if (b && b.job) { b.job.progress = d.progress; b.job.eta = d.eta; renderShelf(); }
  });
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) { clearTimeout(esTimer); esTimer = setTimeout(listen, 3000); }
  };
}

/* ------------------------------------------------------------------ Add a book */
const addp = $("addp");
let staged = null, makeBusy = false, addTimer = null, opts = { speed: 1.0, format: "m4b", llm: false };
function setAdd(st) {
  addp.dataset.state = st;
  setTimeout(relayout, 400);
}
function llmAvailable() {
  const s = S.settings;
  return !!(s && s.llm && s.llm.enabled && Object.values(s.llm.keys || {}).some(k => k.set));
}
function showAddErr(msg) { const e = $("addErr"); e.textContent = msg || ""; e.hidden = !msg; }
function allowedExts() { return S.formats.flatMap(f => f.extensions); }
function applyAccept() {
  $("fileInput").accept = S.formats.filter(f => f.available).flatMap(f => f.extensions).join(",");
}
function checkChosen(f) {
  const name = f.name.toLowerCase();
  const fmt = S.formats.find(x => x.extensions.some(e => name.endsWith(e)));
  if (!fmt) return `Book Buddy can't read ${/\.[^.]+$/.test(name) ? "." + name.split(".").pop() : "this"} files. Try EPUB, PDF, MOBI, DOCX or TXT.`;
  if (!fmt.available) return `${fmt.name} files need ${fmt.needs || "another program"} installed first.` + (fmt.note ? ` (${fmt.note})` : "");
  const max = S.settings && S.settings.max_upload_mb;
  if (max && f.size > max * 1048576) return `That file is larger than the ${max} MB limit.`;
  if (f.size === 0) return "That file is empty.";
  return null;
}
async function chooseFile(f) {
  if (!f || makeBusy) return;
  showAddErr("");
  const bad = checkChosen(f);
  if (bad) { showAddErr(bad); return; }
  const big = $("dropBig"), sub = $("dropSub"), bigOld = big.textContent, subOld = sub.innerHTML;
  big.textContent = "Reading your book…"; sub.textContent = f.name;
  const fd = new FormData();
  fd.append("file", f); fd.append("speed", String(opts.speed));
  try {
    const d = await api("/api/inspect", { method: "POST", body: fd });
    staged = d;
    fillFileCard(d);
    setAdd("file");
  } catch (e) {
    if (!(e instanceof AuthError)) showAddErr(e.message || "Book Buddy could not read this file.");
  } finally { big.textContent = bigOld; sub.innerHTML = subOld; $("fileInput").value = ""; }
}
function fillFileCard(d) {
  const stem = (d.title || d.filename || "").slice(0, 60);
  $("fileCover").replaceChildren(d.has_cover
    ? el("img", { class: "cover xs", src: `/api/inspect/${d.upload_id}/cover`, alt: "", on: { error: e => e.target.replaceWith(el("div", { class: "cover xs c0", textContent: stem })) } })
    : el("div", { class: "cover xs c" + (d.filename.length % 4), textContent: stem }));
  $("fileName").textContent = d.filename;
  updateFileMeta();
  const s = S.settings;
  opts = { speed: s ? s.speed : 1.0, format: s ? s.format : "m4b", llm: llmAvailable() };
  $("voiceSel").value = s ? s.voice : "af_heart";
  buildOptSelects();
  showOpts();
  $("optsEdit").hidden = true; $("optsLine").hidden = false;
  $("makeBtn").disabled = false; $("makeErr").hidden = true;
}
function updateFileMeta() {
  const d = staged; if (!d) return;
  const bits = [];
  if (d.needs_ocr) {
    if (d.author) bits.push(d.author);
    bits.push("Scanned book · chapters found while reading");
  } else {
    if (d.author) bits.push(d.author);
    if (d.chapters != null) bits.push(plural(d.chapters, "chapter"));
    if (d.words) bits.push(fmtAbout(d.words / d.words_per_minute * 60 / opts.speed) + " of audio");
  }
  $("fileMeta").textContent = bits.join(" · ");
}
function buildOptSelects() {
  $("speedSel").replaceChildren(...SPEEDS.map(s => el("option", { value: s, textContent: `Speed ${speedText(s)}` })));
  if (!SPEEDS.includes(opts.speed)) $("speedSel").prepend(el("option", { value: opts.speed, textContent: `Speed ${speedText(opts.speed)}` }));
  $("speedSel").value = opts.speed;
  $("formatSel").replaceChildren(...Object.entries(FORMAT_LABEL).map(([v, l]) => el("option", { value: v, textContent: l })));
  $("formatSel").value = opts.format;
  $("llmRow").hidden = !llmAvailable();
  $("llmChk").checked = opts.llm;
}
function showOpts() {
  const line = $("optsLine");
  line.replaceChildren(`Speed ${speedText(opts.speed)} · ${FORMAT_LABEL[opts.format] || opts.format} · Text cleanup: ${opts.llm ? "AI" : "basic"} · `,
    el("button", { type: "button", class: "link linkbtn", textContent: "change", on: { click: () => { $("optsEdit").hidden = false; line.hidden = true; $("speedSel").focus(); } } }));
}
$("speedSel").addEventListener("change", () => { opts.speed = +$("speedSel").value; updateFileMeta(); stopSample(); });
$("formatSel").addEventListener("change", () => { opts.format = $("formatSel").value; });
$("llmChk").addEventListener("change", () => { opts.llm = $("llmChk").checked; });
$("optsDone").addEventListener("click", () => { showOpts(); $("optsEdit").hidden = true; $("optsLine").hidden = false; $("optsLine").querySelector("button").focus(); });

function discardStaged() {
  const d = staged; staged = null;
  if (d) fetch(`/api/inspect/${d.upload_id}`, { method: "DELETE", credentials: "same-origin", keepalive: true }).catch(() => {});
}
$("fileX").addEventListener("click", () => { discardStaged(); stopSample(); setAdd("empty"); $("drop").focus(); });
addEventListener("pagehide", () => { if (staged && addp.dataset.state === "file") discardStaged(); });

const drop = $("drop");
drop.addEventListener("click", () => $("fileInput").click());
drop.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("fileInput").click(); } });
$("fileInput").addEventListener("change", e => chooseFile(e.target.files[0]));
["dragenter", "dragover"].forEach(t => addp.addEventListener(t, e => { if (hasFiles(e)) { e.preventDefault(); drop.classList.add("over"); } }));
["dragleave", "drop"].forEach(t => addp.addEventListener(t, () => drop.classList.remove("over")));
addp.addEventListener("drop", e => { if (hasFiles(e)) { e.preventDefault(); chooseFile(e.dataTransfer.files[0]); } });
function hasFiles(e) { return e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files"); }
// a file dropped beside the panel must not navigate the browser away
["dragover", "drop"].forEach(t => addEventListener(t, e => { if (hasFiles(e)) e.preventDefault(); }));

$("makeBtn").addEventListener("click", async () => {
  if (!staged || makeBusy) return;
  makeBusy = true; $("makeBtn").disabled = true; $("makeErr").hidden = true; stopSample();
  const fd = new FormData();
  fd.append("upload_id", staged.upload_id); fd.append("voice", $("voiceSel").value);
  fd.append("speed", String(opts.speed)); fd.append("format", opts.format); fd.append("llm", opts.llm ? "on" : "off");
  const title = staged.title || staged.filename.replace(/\.[^.]+$/, "");
  try {
    await api("/api/books", { method: "POST", body: fd });
    staged = null;
    $("doneTitle").textContent = `${title} is on your shelf`;
    setAdd("added");
    loadBooks().catch(() => {});
    clearTimeout(addTimer); addTimer = setTimeout(() => setAdd("empty"), 4000);
  } catch (e) {
    if (e.status === 404 || e.status === 410) { staged = null; setAdd("empty"); showAddErr("That upload is no longer available. Choose the file again."); }
    else { $("makeBtn").disabled = false; const m = $("makeErr"); m.textContent = e.message || "Could not start the audiobook."; m.hidden = false; }
  } finally { makeBusy = false; }
});

/* voices + Hear it */
const sample = $("sample"), audio = $("audio");
function fillVoices(sel) {   // grouped by gender so the label can stay short, as in the design
  const groups = [["female", "Female voices"], ["male", "Male voices"]].map(([g, label]) => {
    const opts = S.voices.filter(v => v.gender === g).map(v => el("option", { value: v.id, textContent: voiceLabel(v) }));
    return opts.length ? el("optgroup", { label }, ...opts) : null;
  }).filter(Boolean);
  const rest = S.voices.filter(v => v.gender !== "female" && v.gender !== "male").map(v => el("option", { value: v.id, textContent: voiceLabel(v) }));
  sel.replaceChildren(...groups, ...rest);
}
function stopSample() { sample.pause(); sample.removeAttribute("src"); sample.load(); setHear("Hear it", "play"); }
function setHear(text, icon) {
  const b = $("hearBtn"); b.querySelector("span").textContent = text; b.querySelector("use").setAttribute("href", "#" + icon);
}
let hearing = false;
$("hearBtn").addEventListener("click", async () => {
  if (hearing || !sample.paused) { hearing = false; stopSample(); return; }
  audio.pause();
  hearing = true; setHear("Loading…", "pause");
  sample.src = `/api/voices/${encodeURIComponent($("voiceSel").value)}/sample?speed=${opts.speed}`;
  try { await sample.play(); setHear("Stop", "pause"); } catch { if (hearing) toast("Could not play the sample."); stopSample(); }
  hearing = false;
});
$("voiceSel").addEventListener("change", stopSample);
sample.addEventListener("ended", stopSample);
sample.addEventListener("error", () => { if (sample.getAttribute("src")) { toast("Could not make the voice sample."); stopSample(); } });

/* ------------------------------------------------------------------ Book page and player */
const P = { book: null, chapters: [], cur: -1, sleepAt: null, sleepEnd: false, sleepTimer: null, lastSave: 0, startAt: null };
let rate = store.get("rate", 1);

async function openBook(id) {
  setView("book");
  S.detailId = id;
  await loadDetail(id, true);
}
async function loadDetail(id, first) {
  let b;
  try { b = await api(`/api/books/${id}`); }
  catch (e) { if (e.status === 404) { location.hash = ""; toast("That book is no longer on the shelf."); } else swallow(e); return; }
  if (S.detailId !== id) return;
  S.detail = b;
  try { S.bookmarks = await api(`/api/books/${id}/bookmarks`); } catch { S.bookmarks = []; }
  const same = P.book === id;
  if (!same || first) {
    if (!same) { audio.pause(); }
    P.book = same ? P.book : id;
    if (!same) { P.cur = -1; P.startAt = null; prepareResume(b); }
  }
  P.chapters = b.chapters;
  renderBookPage();
}
function prepareResume(b) {
  const ready = b.chapters.filter(c => c.status === "ready");
  if (!ready.length) { P.cur = -1; return; }
  let ch = b.playback && b.playback.chapter != null ? b.playback.chapter : ready[0].index;
  let pos = b.playback && b.playback.position || 0;
  if (!ready.find(c => c.index === ch)) { ch = ready[0].index; pos = 0; }
  loadChapter(ch, pos, false);
}
function loadChapter(idx, pos, play) {
  const b = S.detail; if (!b) return;
  const c = b.chapters.find(x => x.index === idx); if (!c || c.status !== "ready") return;
  P.cur = idx; P.playingBook = b;
  const src = `/api/books/${b.id}/chapters/${idx}/audio`;
  if (!audio.src.endsWith(src) || audio.dataset.book !== String(b.id)) { audio.src = src; audio.dataset.book = String(b.id); }
  audio.preload = "metadata";
  audio.playbackRate = rate;
  const seek = () => { try { audio.currentTime = Math.max(0, Math.min(pos || 0, (audio.duration || c.duration || 1e9) - 0.1)); } catch { /* not seekable yet */ } updateTimes(); };
  if (audio.readyState >= 1) seek(); else audio.addEventListener("loadedmetadata", seek, { once: true });
  if (play) { sample.pause(); audio.play().catch(() => {}); }
  renderBookPage(); updatePlayer();
}
function chapterAt(idx) { return (S.detail ? S.detail.chapters : []).find(c => c.index === idx); }
function nextReady(from, dir) {
  const cs = (S.detail ? S.detail.chapters : []);
  const i = cs.findIndex(c => c.index === from);
  for (let k = i + dir; k >= 0 && k < cs.length; k += dir) if (cs[k].status === "ready") return cs[k].index;
  return null;
}

function renderBookPage() {
  const b = S.detail; if (!b) return;
  const chs = b.chapters;
  const dur = c => c.duration || 0;
  const readyDur = chs.filter(c => c.status === "ready").reduce((a, c) => a + dur(c), 0);
  const total = b.total_duration || readyDur;
  const posNow = b.id === P.book && P.cur >= 0 ? (chs.filter(c => c.index < P.cur && c.status === "ready").reduce((a, c) => a + dur(c), 0) + (audio.currentTime || 0)) : 0;
  const left = Math.max(0, total - posNow);
  // info panel
  const dlLink = (fmt, text) => { const a = el("a", { class: "link", href: `/api/books/${b.id}/download?format=${fmt}`, download: "" });
    a.innerHTML = `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><use href="#down"/></svg> ${text}`; return a; };
  const links = [];
  if (b.download_kind === "m4b") links.push(dlLink("m4b", "Download M4B"));
  if (b.chapters_ready) links.push(dlLink("zip", "Download chapters (zip)"));
  $("bookInfo").replaceChildren(
    coverEl(b, "lg"),
    el("div", { style: "min-width:0" }, el("h1", { textContent: b.title }),
      el("div", { class: "muted", textContent: [b.author && b.author !== "Unknown" ? b.author : "", `read by ${voiceName(b.voice)}`].filter(Boolean).join(" · ") }),
      el("div", { class: "meta muted" },
        el("span", { textContent: total ? `${fmtHM(total)}${b.status === "ready" ? "" : " so far"}${P.cur >= 0 ? ` · ${fmtHM(left)} left` : ""}` : "Still starting" }),
        el("span", { textContent: `${b.chapters_ready || 0} of ${b.chapters_total || chs.length} chapters ready` })),
      el("div", { class: "dl" }, links)));
  // now playing heading
  const cur = chapterAt(P.cur);
  $("nowTitle").firstChild.textContent = cur ? `Now playing · ${cur.title}` : "Now playing";
  // chapters
  const marksBy = {}; S.bookmarks.forEach(m => (marksBy[m.chapter] ||= []).push(m));
  $("chapters").replaceChildren(...chs.map((c, i) => {
    const ready = c.status === "ready", now = ready && c.index === P.cur && P.book === b.id;
    const row = el("div", { class: "ch" + (now ? " now" : "") + (ready ? "" : " pending locked") });
    row.append(el("span", { class: "n", textContent: String(i + 1) }));
    const t = el("button", { class: "t", type: "button", disabled: !ready, textContent: (now ? "▶ " : "") + c.title,
      attrs: { "aria-label": ready ? `Play ${c.title}` : `${c.title} is not ready yet` }, on: { click: () => loadChapter(c.index, 0, true) } });
    row.append(t);
    if (ready) row.append(now ? el("span", { textContent: clock(c.duration) }) : el("span", { class: "ok", textContent: `✓ ${clock(c.duration)}` }));
    else row.append(el("span", { textContent: c.status === "voicing" ? "voicing…" : "waiting" }));
    const ms = marksBy[c.index];
    if (ms) row.append(el("div", { class: "marks" }, ms.map(m => el("span", { class: "mark" },
      el("button", { class: "go", type: "button", textContent: `Bookmark ${clock(m.position)}`, on: { click: () => loadChapter(c.index, m.position, true) } }),
      el("button", { type: "button", textContent: "×", attrs: { "aria-label": "Remove bookmark" }, on: { click: async () => {
        try { await api(`/api/books/${b.id}/bookmarks/${m.id}`, { method: "DELETE" }); S.bookmarks = S.bookmarks.filter(x => x.id !== m.id); renderBookPage(); } catch (e) { swallow(e); } } } })))));
    return row;
  }));
  const hasReady = chs.some(c => c.status === "ready");
  ["playBtn", "backBtn", "fwdBtn", "markChip"].forEach(id => { $(id).disabled = !hasReady; });
  $("prevBtn").disabled = P.cur < 0 || nextReady(P.cur, -1) == null;
  $("nextBtn").disabled = P.cur < 0 || nextReady(P.cur, 1) == null;
  $("speedChip").textContent = speedText(rate);
  updateTimes(); updatePlayIcon(); relayout();
}

function updateTimes() {
  const c = chapterAt(P.cur), d = (audio.duration && isFinite(audio.duration)) ? audio.duration : (c ? c.duration : 0) || 0;
  const t = audio.currentTime || 0, frac = d ? Math.min(1, t / d) : 0;
  $("tCur").textContent = clock(t); $("tLeft").textContent = "−" + clock(Math.max(0, d - t));
  $("scrubFill").style.inset = `0 ${(1 - frac) * 100}% 0 0`; $("scrubKnob").style.left = frac * 100 + "%";
  const s = $("scrub"); s.setAttribute("aria-valuenow", String(Math.round(frac * 100))); s.setAttribute("aria-valuetext", `${clock(t)} of ${clock(d)}`);
  $("plProg").style.width = frac * 100 + "%";
}
function updatePlayIcon() {
  const playing = !audio.paused && !audio.ended;
  for (const id of ["playBtn", "plPlay"]) {
    const b = $(id); b.querySelector("use").setAttribute("href", playing ? "#pause" : "#play"); b.setAttribute("aria-label", playing ? "Pause" : "Play");
  }
}
function updatePlayer() {
  const b = P.playingBook, c = b && b.chapters.find(x => x.index === P.cur);
  if (!b || !c) return;
  $("plCover").replaceChildren(coverEl(b, "xs"));
  $("plTitle").textContent = b.title;
  $("plSub").textContent = `Ch ${b.chapters.indexOf(c) + 1} · ${c.title}`;
  $("plLink").href = `#book/${b.id}`;
}

function savePosition(force) {
  const b = P.playingBook; if (!b || P.cur < 0 || !audio.src) return;
  const now = Date.now();
  if (!force && now - P.lastSave < 5000) return;
  P.lastSave = now;
  fetch(`/api/books/${b.id}/playback`, { ...jsonOpts("PUT", { chapter: P.cur, position: audio.currentTime || 0 }), credentials: "same-origin", keepalive: true }).catch(() => {});
}
audio.addEventListener("timeupdate", () => { updateTimes(); savePosition(false); if (S.detail && P.book === S.detail.id) refreshHeaderMeta(); });
let metaT = 0;
function refreshHeaderMeta() { if (Date.now() - metaT > 15000) { metaT = Date.now(); renderBookPage(); } }
audio.addEventListener("play", () => { body.classList.add("playing"); updatePlayIcon(); relayout(); stopSample(); });
audio.addEventListener("pause", () => { updatePlayIcon(); savePosition(true); });
audio.addEventListener("loadedmetadata", updateTimes);
audio.addEventListener("ended", () => {
  if (P.sleepEnd) { P.sleepEnd = false; setSleep(0); updatePlayIcon(); return; }
  const n = nextReady(P.cur, 1);
  if (n != null) loadChapter(n, 0, true);
  else { body.classList.remove("playing"); updatePlayIcon(); savePosition(true); relayout(); }
});
audio.addEventListener("error", () => { if (audio.getAttribute("src")) toast("That chapter could not be played."); });
addEventListener("pagehide", () => savePosition(true));
document.addEventListener("visibilitychange", () => { if (document.hidden) savePosition(true); });

function toggle() {
  if (P.cur < 0) { const first = S.detail && S.detail.chapters.find(c => c.status === "ready"); if (first) loadChapter(first.index, 0, true); return; }
  if (audio.paused) { sample.pause(); audio.play().catch(() => {}); } else audio.pause();
}
function seekBy(d) { audio.currentTime = Math.max(0, Math.min((audio.duration || 1e9), audio.currentTime + d)); updateTimes(); savePosition(true); }
$("playBtn").addEventListener("click", toggle);
$("plPlay").addEventListener("click", toggle);
$("backBtn").addEventListener("click", () => seekBy(-30));
$("plBack").addEventListener("click", () => seekBy(-30));
$("fwdBtn").addEventListener("click", () => seekBy(30));
$("prevBtn").addEventListener("click", () => { if (audio.currentTime > 5) { audio.currentTime = 0; return; } const n = nextReady(P.cur, -1); if (n != null) loadChapter(n, 0, !audio.paused); });
$("nextBtn").addEventListener("click", () => { const n = nextReady(P.cur, 1); if (n != null) loadChapter(n, 0, !audio.paused); });
// scrub
const scrub = $("scrub");
function scrubTo(clientX) {
  const r = scrub.getBoundingClientRect(), f = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
  if (audio.duration && isFinite(audio.duration)) { audio.currentTime = f * audio.duration; updateTimes(); }
}
let scrubbing = false;
scrub.addEventListener("pointerdown", e => { scrubbing = true; scrub.setPointerCapture(e.pointerId); scrubTo(e.clientX); });
scrub.addEventListener("pointermove", e => { if (scrubbing) scrubTo(e.clientX); });
scrub.addEventListener("pointerup", () => { scrubbing = false; savePosition(true); });
scrub.addEventListener("keydown", e => {
  if (e.key === "ArrowRight") { seekBy(10); e.preventDefault(); } else if (e.key === "ArrowLeft") { seekBy(-10); e.preventDefault(); }
});
// speed, sleep timer, bookmark
$("speedChip").addEventListener("click", () => {
  const list = [0.8, 1, 1.25, 1.5, 1.75, 2], i = list.findIndex(x => x > rate + 1e-6);
  rate = i < 0 ? list[0] : list[i]; store.set("rate", rate); audio.playbackRate = rate; $("speedChip").textContent = speedText(rate);
});
audio.addEventListener("loadeddata", () => { audio.playbackRate = rate; });
const SLEEP = [0, 15, 30, 45, 60, "end"];
let sleepI = 0;
function setSleep(i) {
  sleepI = i; clearTimeout(P.sleepTimer); P.sleepEnd = false;
  const v = SLEEP[i], chip = $("sleepChip");
  if (!v) { chip.textContent = "☾ Sleep timer"; return; }
  if (v === "end") { P.sleepEnd = true; chip.textContent = "☾ End of chapter"; return; }
  chip.textContent = `☾ ${v} min`;
  P.sleepTimer = setTimeout(() => { audio.pause(); setSleep(0); }, v * 60000);
}
$("sleepChip").addEventListener("click", () => setSleep((sleepI + 1) % SLEEP.length));
$("markChip").addEventListener("click", async () => {
  const b = S.detail; if (!b || P.cur < 0) return;
  try {
    const m = await api(`/api/books/${b.id}/bookmarks`, jsonOpts("POST", { chapter: P.cur, position: audio.currentTime || 0 }));
    S.bookmarks.push(m); S.bookmarks.sort((x, y) => x.chapter - y.chapter || x.position - y.position);
    renderBookPage();
    const chip = $("markChip"); chip.textContent = "Bookmarked ✓"; setTimeout(() => { chip.textContent = "Bookmark"; }, 2000);
  } catch (e) { swallow(e); }
});

/* ------------------------------------------------------------------ Settings drawer */
const drawer = $("drawer");
let lastFocus = null;
function fillSettingSelects() {
  fillVoices($("sVoice"));
  $("sSpeed").replaceChildren(...SPEEDS.map(s => el("option", { value: s, textContent: speedText(s) })));
  $("sFormat").replaceChildren(...Object.entries(FORMAT_LABEL).map(([v, l]) => el("option", { value: v, textContent: l })));
}
function renderSettings() {
  const s = S.settings; if (!s) return;
  $("sVoice").value = s.voice;
  if (!SPEEDS.includes(s.speed)) $("sSpeed").prepend(el("option", { value: s.speed, textContent: speedText(s.speed) }));
  $("sSpeed").value = s.speed; $("sFormat").value = s.format;
  $("sInbox").value = s.watch_inbox; $("sOutbox").value = s.watch_outbox;
  renderKeys();
}
function renderKeys() {   // keys save on their own; redraw only these rows so unsaved drawer edits survive
  const s = S.settings;
  $("llmToggle").setAttribute("aria-checked", String(!!s.llm.enabled));
  document.querySelectorAll(".keyf").forEach(f => {
    const p = f.dataset.provider, k = s.llm.keys[p] || { set: false }, name = p === "anthropic" ? "Claude API key" : "OpenAI API key";
    f.replaceChildren(el("span", { textContent: name }));
    if (k.set) {
      f.append(el("span", { class: "keyin" }, el("span", { class: "set", textContent: `•••• ${k.last4} ✓` }),
        k.source === "config" ? el("button", { type: "button", class: "link", textContent: "Remove", on: { click: () => keyAction(p, "remove") } }) : el("span", { class: "muted", textContent: "from environment" })));
    } else {
      const inp = el("input", { type: "password", placeholder: "not set", autocomplete: "off", spellcheck: false, attrs: { "aria-label": name } });
      inp.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); keyAction(p, "set", inp.value); } });
      f.append(el("span", { class: "keyin" }, inp, el("button", { type: "button", class: "link", textContent: "Set", on: { click: () => keyAction(p, "set", inp.value) } })));
    }
  });
}
async function keyAction(p, what, value) {
  const err = $("keyErr"); err.hidden = true;
  try {
    if (what === "set") { if (!value || !value.trim()) return; await api("/api/settings/keys", jsonOpts("PUT", { [p]: value.trim() })); }
    else await api(`/api/settings/keys/${p}`, { method: "DELETE" });
    S.settings = await api("/api/settings"); renderKeys();
  } catch (e) { if (!(e instanceof AuthError)) { err.textContent = e.message; err.hidden = false; } }
}
$("llmToggle").addEventListener("click", () => { const t = $("llmToggle"); t.setAttribute("aria-checked", String(t.getAttribute("aria-checked") !== "true")); });
function openSettings() {
  lastFocus = document.activeElement; renderSettings(); $("setMsg").textContent = ""; $("keyErr").hidden = true;
  body.classList.add("settings"); drawer.setAttribute("aria-hidden", "false"); $("setClose").focus();
}
function closeSettings() {
  body.classList.remove("settings"); drawer.setAttribute("aria-hidden", "true");
  if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
}
document.querySelectorAll("[data-open-settings]").forEach(b => b.addEventListener("click", openSettings));
$("setClose").addEventListener("click", closeSettings);
$("scrim").addEventListener("click", closeSettings);
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { if (body.classList.contains("settings")) closeSettings(); else closeMenu(); }
  if (e.key === "Tab" && body.classList.contains("settings")) {         // keep focus inside the drawer
    const f = [...drawer.querySelectorAll("button, input, select, [tabindex]")].filter(x => !x.disabled && x.offsetParent !== null);
    if (f.length) {
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }
});
$("setSave").addEventListener("click", async () => {
  const msg = $("setMsg"); msg.textContent = "";
  try {
    S.settings = await api("/api/settings", jsonOpts("PUT", {
      voice: $("sVoice").value, speed: +$("sSpeed").value, format: $("sFormat").value,
      watch_inbox: $("sInbox").value.trim(), watch_outbox: $("sOutbox").value.trim(),
      llm_enabled: $("llmToggle").getAttribute("aria-checked") === "true",
    }));
    renderSettings(); applyDonate();
    msg.textContent = "Saved ✓";
    if (addp.dataset.state === "file") { $("llmRow").hidden = !llmAvailable(); }
  } catch (e) { if (!(e instanceof AuthError)) msg.textContent = e.message; }
});
function applyDonate() {
  const a = $("donateBtn"), u = S.settings && S.settings.donate_url;
  if (u && /^https?:\/\//.test(u)) { a.href = u; a.hidden = false; } else a.hidden = true;
}

/* ------------------------------------------------------------------ start */
async function start() {
  let sess;
  try { sess = await fetch("/api/session", { credentials: "same-origin" }).then(r => r.json()); }
  catch { $("shelfLoading").firstElementChild.textContent = "Can't reach Book Buddy. Is it still running?"; setTimeout(start, 3000); return; }
  S.session = sess;
  if (sess.auth_required && !sess.authenticated) { showLogin(); return; }
  try {
    [S.voices, S.settings, S.formats] = await Promise.all([api("/api/voices"), api("/api/settings"), api("/api/formats")]);
    fillVoices($("voiceSel")); fillSettingSelects(); applyAccept(); applyDonate();
    S.books = await api("/api/books");
  } catch (e) {
    if (!(e instanceof AuthError)) { $("shelfLoading").firstElementChild.textContent = "Can't load your books right now. Trying again…"; setTimeout(start, 3000); }
    return;
  }
  S.ready = true;
  renderShelf(); listen(); route();
}
makeCollapsible();
start();
})();
