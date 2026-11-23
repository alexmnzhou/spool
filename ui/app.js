// Spool UI. Talks to Python through window.pywebview.api (see main.py);
// Python pushes download events back through window.spool.onEvent.
"use strict";

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;

const state = {
  settings: {},
  running: false,
  previewToken: 0,
  previewUrl: "",
  info: null,          // last successful preview
  duration: null,      // seconds, when known
  boards: [],          // storyboards, largest first
  start: 0,            // clip bounds, seconds
  end: null,           // null = to the end
  lastPath: null,
  logLines: 0,
};

// ----------------------------------------------------------- helpers --

function fmtTime(sec) {
  if (sec == null || isNaN(sec)) return "";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function parseTime(str) {
  str = (str || "").trim();
  if (!str) return null;
  const parts = str.split(":");
  if (parts.length > 3 || parts.some((p) => p === "" || isNaN(Number(p)))) return NaN;
  return parts.reduce((acc, p) => acc * 60 + Number(p), 0);
}

function fmtCount(n) {
  if (n == null) return "";
  if (n >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
  return String(n);
}

function fmtDate(yyyymmdd) {
  if (!yyyymmdd || yyyymmdd.length !== 8) return "";
  const d = new Date(+yyyymmdd.slice(0, 4), +yyyymmdd.slice(4, 6) - 1, +yyyymmdd.slice(6, 8));
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function looksLikeUrl(s) {
  return /^(https?:\/\/)?([\w-]+\.)+[\w-]{2,}(\/\S*)?$/i.test(s.trim());
}

function baseName(path) {
  return (path || "").split(/[\\/]/).pop();
}

function setFieldError(field, msg) {
  const el = $("err-" + field);
  if (el) el.textContent = msg || "";
  const holder = { url: $("urlField"), start: $("start")?.parentElement, end: $("end")?.parentElement }[field];
  if (holder) holder.classList.toggle("invalid", !!msg);
}

function clearErrors() {
  ["url", "outdir", "start", "end", "cookiefile"].forEach((f) => setFieldError(f, ""));
}

let saveTimer = null;
function saveSettings() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => api().save_settings(state.settings), 250);
}

// -------------------------------------------------------- segmented --

function bindSegmented(el, value, onChange) {
  const set = (v) => {
    el.querySelectorAll("button").forEach((b) => {
      const on = b.dataset.value === v;
      b.classList.toggle("on", on);
      b.setAttribute("aria-checked", on);
    });
  };
  set(value);
  el.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || state.running) return;
    set(b.dataset.value);
    onChange(b.dataset.value);
  });
  return set;
}

// ---------------------------------------------------------- preview --

let previewTimer = null;

function schedulePreview(delay) {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(loadPreview, delay);
}

async function loadPreview(force = false) {
  const url = $("url").value.trim();
  $("clearUrl").hidden = !url;
  if (!url) {
    state.previewUrl = ""; state.info = null;
    setDuration(null, []);
    $("preview").dataset.state = "empty";
    updateGoLabel();
    return;
  }
  if (!looksLikeUrl(url)) return;
  if (url === state.previewUrl && !force) return;

  const token = ++state.previewToken;
  state.previewUrl = url;
  $("preview").dataset.state = "loading";
  $("thumb").classList.remove("loaded");
  $("thumbBox").classList.remove("playlist");

  const s = state.settings;
  let res;
  try {
    res = await api().preview({
      url, whole_playlist: s.whole_playlist,
      cookie_source: s.cookie_source, browser: s.browser, cookiefile: s.cookiefile,
    });
  } catch (e) {
    res = { ok: false, error: String(e) };
  }
  if (token !== state.previewToken) return;        // a newer link superseded this one

  if (!res.ok) {
    state.info = null;
    $("pvError").textContent = res.error || "Unknown error";
    $("preview").dataset.state = "error";
    setDuration(null, []);
    updateGoLabel();
    return;
  }
  state.info = res;
  renderPreview(res);
}

function renderPreview(info) {
  const img = $("thumb");
  img.onload = () => img.classList.add("loaded");
  img.onerror = () => img.classList.remove("loaded");
  img.src = info.thumbnail || "";

  $("pvTitle").textContent = info.title;
  $("pvTitle").title = info.title;
  $("pvChannel").textContent = info.channel || "";

  const badge = $("durBadge");
  badge.classList.remove("live");
  if (info.kind === "playlist") {
    $("thumbBox").classList.add("playlist");
    badge.textContent = `${info.count} videos`;
    $("pvStats").textContent = "Playlist · downloads every video";
    setDuration(null, []);
  } else {
    if (info.is_live) { badge.textContent = "LIVE"; badge.classList.add("live"); }
    else badge.textContent = fmtTime(info.duration);
    $("pvStats").textContent = [info.views != null ? `${fmtCount(info.views)} views` : "", fmtDate(info.upload_date)]
      .filter(Boolean).join(" · ");
    setDuration(info.is_live ? null : info.duration, info.storyboards || []);
  }
  $("preview").dataset.state = "ready";
  updateClipAvailability();
  updateGoLabel();
}

// ------------------------------------------------------- timeline --

function setDuration(duration, boards) {
  state.duration = duration || null;
  state.boards = boards;
  const has = !!state.duration;
  $("timeline").classList.toggle("disabled", !has);
  $("ticks").classList.toggle("disabled", !has);
  $("timeline").classList.toggle("no-frames", !boards.length);
  $("tickEnd").textContent = has ? fmtTime(state.duration) : "";

  // A new video resets the clip to the whole thing, shown as real times rather
  // than placeholders that only look like values.
  if (has) {
    state.start = 0;
    state.end = state.duration;
    syncInputsFromState();
    setFieldError("start", ""); setFieldError("end", "");
  }
  renderFilmstrip();
  renderRange();
}

// Storyboards are sprite sheets: `cols x rows` frames per image, one frame every
// `interval` seconds. Work out which image and which cell holds time t.
function frameAt(board, t) {
  const per = board.cols * board.rows;
  const total = Math.max(1, Math.ceil((state.duration || 0) / board.interval));
  let idx = Math.min(Math.floor(Math.max(0, t) / board.interval), total - 1);
  let sheet = Math.floor(idx / per);
  if (sheet >= board.urls.length) { sheet = board.urls.length - 1; idx = sheet * per + per - 1; }
  const cell = idx - sheet * per;
  return { url: board.urls[sheet], col: cell % board.cols, row: Math.floor(cell / board.cols) };
}

function paintFrame(el, board, t, w, h) {
  const f = frameAt(board, t);
  el.style.backgroundImage = `url("${f.url}")`;
  el.style.backgroundSize = `${board.cols * w}px ${board.rows * h}px`;
  el.style.backgroundPosition = `-${f.col * w}px -${f.row * h}px`;
}

function renderFilmstrip() {
  const strip = $("filmstrip");
  strip.innerHTML = "";
  if (!state.duration || !state.boards.length) return;
  // The mid-sized board is plenty for thumbnails this small.
  const board = state.boards[Math.min(1, state.boards.length - 1)];
  const W = strip.clientWidth || 700, H = strip.clientHeight || 58;
  const cellW = H * 16 / 9;
  const n = Math.max(1, Math.round(W / cellW));
  const w = W / n;
  const seen = new Set();
  for (let i = 0; i < n; i++) {
    const d = document.createElement("div");
    const t = ((i + 0.5) / n) * state.duration;
    const f = frameAt(board, t);
    d.style.backgroundImage = `url("${f.url}")`;
    d.style.backgroundSize = `${board.cols * w}px ${board.rows * H}px`;
    d.style.backgroundPosition = `-${f.col * w}px -${f.row * H}px`;
    strip.appendChild(d);
    if (!seen.has(f.url)) {
      seen.add(f.url);
      const img = new Image();
      img.onload = () => strip.querySelectorAll("div").forEach((c) => {
        if (c.style.backgroundImage.includes(f.url)) c.classList.add("loaded");
      });
      img.src = f.url;
    }
  }
}

function clipEnd() {
  return state.end != null ? state.end : state.duration;
}

function renderRange() {
  const D = state.duration;
  const s = state.start || 0;
  const e = clipEnd();
  if (D) {
    const ps = (s / D) * 100, pe = ((e ?? D) / D) * 100;
    $("hStart").style.left = ps + "%";
    $("hEnd").style.left = pe + "%";
    $("range").style.left = ps + "%";
    $("range").style.width = Math.max(0, pe - ps) + "%";
    $("shadeL").style.width = ps + "%";
    $("shadeR").style.width = Math.max(0, 100 - pe) + "%";
  }
  // Length pill: only when there's a real length to show.
  const len = e != null ? e - s : null;
  $("clipLen").hidden = !(len > 0);
  $("clipLenText").textContent = len > 0 ? `Length ${fmtTime(len)}` : "";
  updateGoLabel();
}

function syncInputsFromState() {
  $("start").value = fmtTime(state.start || 0);
  $("end").value = state.end != null ? fmtTime(state.end) : "";
}

function bindTimeline() {
  const tl = $("timeline");
  const hover = $("hover");
  let drag = null;

  const timeAt = (clientX) => {
    const r = tl.getBoundingClientRect();
    const x = Math.min(Math.max(clientX - r.left, 0), r.width);
    return Math.round((x / r.width) * state.duration);
  };

  const showHover = (t) => {
    if (!state.duration) return;
    const pct = (t / state.duration) * 100;
    hover.style.left = `clamp(84px, ${pct}%, calc(100% - 84px))`;
    $("hoverTime").textContent = fmtTime(t);
    if (state.boards.length) paintFrame($("hoverFrame"), state.boards[0], t, 160, 90);
    tl.classList.add("hovering");
  };

  const setBound = (which, t) => {
    const D = state.duration;
    if (which === "start") {
      state.start = Math.min(Math.max(0, t), (clipEnd() ?? D) - 1);
    } else {
      state.end = Math.max(Math.min(D, t), state.start + 1);
    }
    syncInputsFromState();
    setFieldError("start", ""); setFieldError("end", "");
    renderRange();
  };

  tl.addEventListener("pointerdown", (e) => {
    if (!state.duration || state.running) return;
    const t = timeAt(e.clientX);
    let which = e.target.closest(".handle")?.dataset.which;
    if (!which) {
      // Clicking the track moves whichever handle is nearer.
      which = Math.abs(t - state.start) <= Math.abs(t - (clipEnd() ?? state.duration)) ? "start" : "end";
      setBound(which, t);
    }
    drag = which;
    $(which === "start" ? "hStart" : "hEnd").classList.add("dragging");
    tl.setPointerCapture(e.pointerId);
    showHover(which === "start" ? state.start : clipEnd());
  });
  tl.addEventListener("pointermove", (e) => {
    if (!state.duration) return;
    const t = timeAt(e.clientX);
    if (drag) {
      setBound(drag, t);
      showHover(drag === "start" ? state.start : clipEnd());
    } else {
      showHover(t);
    }
  });
  const endDrag = (e) => {
    if (!drag) return;
    $("hStart").classList.remove("dragging"); $("hEnd").classList.remove("dragging");
    drag = null;
    try { tl.releasePointerCapture(e.pointerId); } catch (_) {}
  };
  tl.addEventListener("pointerup", endDrag);
  tl.addEventListener("pointercancel", endDrag);
  tl.addEventListener("pointerleave", () => { if (!drag) tl.classList.remove("hovering"); });

  // Typed times drive the handles too.
  const onType = (which) => {
    const raw = $(which).value;
    const t = parseTime(raw);
    if (raw.trim() && (t == null || isNaN(t))) {
      setFieldError(which, "Use a time like 1:30 or 1:02:03.");
      return;
    }
    setFieldError(which, "");
    if (which === "start") state.start = t || 0;
    else state.end = t;
    if (state.duration) {
      if (state.start > state.duration) setFieldError("start", `The video is only ${fmtTime(state.duration)} long.`);
      if (state.end != null && state.end > state.duration) setFieldError("end", `The video is only ${fmtTime(state.duration)} long.`);
    }
    if (state.end != null && state.end <= state.start) setFieldError("end", "The clip has to end after it starts.");
    renderRange();
  };
  $("start").addEventListener("input", () => onType("start"));
  $("end").addEventListener("input", () => onType("end"));
  ["start", "end"].forEach((id) => $(id).addEventListener("blur", () => {
    const t = parseTime($(id).value);
    if (t != null && !isNaN(t)) $(id).value = fmtTime(t);      // tidy "90" into "1:30"
  }));

  window.addEventListener("resize", () => { clearTimeout(bindTimeline._rt); bindTimeline._rt = setTimeout(renderFilmstrip, 150); });
}

function updateClipAvailability() {
  const isPlaylist = state.info?.kind === "playlist";
  const isLive = !!state.info?.is_live;
  $("clipSwitch").classList.toggle("disabled", isPlaylist || isLive);
  if ((isPlaylist || isLive) && $("clipToggle").checked) {
    $("clipToggle").checked = false;
    $("clipPanel").hidden = true;
  }
}

// ------------------------------------------------------------- dock --

function updateGoLabel() {
  if (state.running) return;
  const clip = $("clipToggle").checked;
  const audio = state.settings.format === "Audio Only (MP3)";
  $("goLabel").textContent = clip ? (audio ? "Download clip as MP3" : "Download clip") :
                             audio ? "Download MP3" : "Download";
}

function setRunning(on) {
  state.running = on;
  document.body.classList.toggle("running", on);
  const btn = $("goBtn");
  btn.classList.toggle("cancel", on);
  if (on) {
    $("goLabel").textContent = "Cancel";
    $("dock").dataset.state = "running";
    $("statusText").textContent = "Starting…";
    $("pct").textContent = "";
    $("detail").textContent = $("speed").textContent = $("eta").textContent = "";
    $("bar").classList.add("indeterminate");
    $("fill").style.width = "0%";
  } else {
    updateGoLabel();
  }
  ["url", "start", "end"].forEach((id) => ($(id).disabled = on));
}

function showResult(p) {
  const dock = $("dock");
  dock.dataset.state = p.state;
  state.lastPath = p.path || null;
  const icon = { done: "#i-check", error: "#i-alert", cancelled: "#i-ban" }[p.state];
  $("resultIcon").querySelector("use").setAttribute("href", icon);
  if (p.state === "done") {
    $("resultTitle").textContent = "Saved";
    $("resultSub").textContent = p.path ? baseName(p.path) : "";
    $("resultSub").title = p.path || "";
  } else if (p.state === "error") {
    $("resultTitle").textContent = "Download failed";
    $("resultSub").textContent = p.message || "";
    $("resultSub").title = p.message || "";
  } else {
    $("resultTitle").textContent = "Cancelled";
    $("resultSub").textContent = "Partial files were cleaned up.";
  }
  $("openBtn").hidden = !(p.state === "done" && p.path);
  $("revealBtn").hidden = !(p.state === "done" && p.path);
  $("showLogBtn").hidden = p.state !== "error";
}

// ------------------------------------------------------------- log --

function appendLog(msg) {
  const pre = $("log");
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30;
  msg.split("\n").forEach((line) => {
    const span = document.createElement("span");
    if (/^(Error|ERROR)/.test(line)) span.className = "e";
    else if (/^(Warning|Hint)/.test(line)) span.className = "w";
    else if (/^Done!/.test(line)) span.className = "ok";
    else if (/^\[(youtube|info|jsc)/.test(line)) span.className = "dim";
    span.textContent = line + "\n";
    pre.appendChild(span);
  });
  state.logLines++;
  $("logCount").textContent = state.logLines;
  // Keep the DOM from growing without bound over a long session.
  while (pre.childNodes.length > 1500) pre.removeChild(pre.firstChild);
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

// ----------------------------------------------------------- events --

window.spool = {
  onEvent(name, p) {
    switch (name) {
      case "log":
        appendLog(p.msg);
        break;
      case "status":
        if (state.running) $("statusText").textContent = p.text;
        break;
      case "progress": {
        if (!state.running) break;
        const bar = $("bar");
        if (p.fraction == null) {
          bar.classList.add("indeterminate");
          $("pct").textContent = "";
        } else {
          bar.classList.remove("indeterminate");
          $("fill").style.width = (p.fraction * 100).toFixed(1) + "%";
          $("pct").textContent = Math.floor(p.fraction * 100) + "%";
        }
        $("detail").textContent = p.detail || "";
        $("speed").textContent = p.speed || "";
        $("eta").textContent = p.eta ? `${p.eta} left` : "";
        break;
      }
      case "finished":
        setRunning(false);
        showResult(p);
        break;
    }
  },
};

// ----------------------------------------------------------- start --

async function go() {
  if (state.running) {
    $("goLabel").textContent = "Cancelling…";
    api().cancel();
    return;
  }
  clearErrors();
  const s = state.settings;
  const clip = $("clipToggle").checked;
  const req = {
    url: $("url").value.trim(),
    outdir: s.outdir,
    format: s.format,
    whole_playlist: s.whole_playlist,
    precise: s.precise,
    cookie_source: s.cookie_source,
    browser: s.browser,
    cookiefile: s.cookiefile,
    clip,
    start: clip ? $("start").value : "",
    end: clip ? $("end").value : "",
  };
  if (clip && !req.start.trim() && !req.end.trim()) {
    setFieldError("start", "Set a start or end time — or drag the handles.");
    return;
  }
  const res = await api().start(req);
  if (!res.ok) {
    if (res.field === "cookiefile") { openDrawer(); }
    setFieldError(res.field || "url", res.error);
    return;
  }
  setRunning(true);
}

// ------------------------------------------------------------ drawer --

function openDrawer() { document.querySelector(".app").classList.add("drawer-open"); }
function closeDrawer() { document.querySelector(".app").classList.remove("drawer-open"); }

function showCookieOpts(source) {
  document.querySelectorAll(".cookie-opt").forEach((el) => el.classList.toggle("show", el.dataset.for === source));
}

// ------------------------------------------------------------- init --

async function init() {
  const boot = await api().init();
  state.settings = boot.settings;
  const s = state.settings;

  // Format
  bindSegmented($("format"), s.format, (v) => { s.format = v; saveSettings(); updateGoLabel(); });

  // Folder
  $("folderPath").textContent = s.outdir;
  $("folderBtn").title = s.outdir;
  $("folderBtn").addEventListener("click", async () => {
    if (state.running) return;
    const p = await api().pick_folder(s.outdir);
    if (p) {
      s.outdir = p; $("folderPath").textContent = p; $("folderBtn").title = p;
      setFieldError("outdir", ""); saveSettings(); updateGoLabel();
    }
  });

  // Toggles
  $("clipToggle").addEventListener("change", () => {
    $("clipPanel").hidden = !$("clipToggle").checked;
    if ($("clipToggle").checked) requestAnimationFrame(renderFilmstrip);
    updateGoLabel();
  });
  $("playlistToggle").checked = !!s.whole_playlist;
  $("playlistToggle").addEventListener("change", () => {
    s.whole_playlist = $("playlistToggle").checked; saveSettings();
    if (state.previewUrl) loadPreview(true);       // changes what the link resolves to
  });
  $("precise").checked = !!s.precise;
  $("precise").addEventListener("change", () => { s.precise = $("precise").checked; saveSettings(); });

  // URL
  const url = $("url");
  url.addEventListener("input", () => { setFieldError("url", ""); $("clearUrl").hidden = !url.value; schedulePreview(700); });
  url.addEventListener("paste", () => schedulePreview(30));
  url.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.ctrlKey ? go() : loadPreview(true); } });
  $("clearUrl").addEventListener("click", () => { url.value = ""; url.focus(); loadPreview(); });
  $("pasteBtn").addEventListener("click", async () => {
    const text = (await api().paste() || "").trim();
    if (text) { url.value = text; setFieldError("url", ""); loadPreview(true); }
    url.focus();
  });

  bindTimeline();

  // Dock
  $("goBtn").addEventListener("click", go);
  $("openBtn").addEventListener("click", () => state.lastPath && api().open_path(state.lastPath));
  $("revealBtn").addEventListener("click", () => state.lastPath && api().reveal(state.lastPath));
  $("showLogBtn").addEventListener("click", () => {
    $("logBox").open = true;
    $("logBox").scrollIntoView({ behavior: "smooth", block: "end" });
  });

  // Settings drawer
  $("settingsBtn").addEventListener("click", openDrawer);
  $("closeDrawer").addEventListener("click", closeDrawer);
  $("scrim").addEventListener("click", closeDrawer);
  bindSegmented($("cookieSource"), s.cookie_source, (v) => {
    s.cookie_source = v; showCookieOpts(v); setFieldError("cookiefile", ""); saveSettings();
  });
  showCookieOpts(s.cookie_source);
  const sel = $("browser");
  boot.browsers.forEach((b) => sel.add(new Option(b, b)));
  sel.value = s.browser;
  sel.addEventListener("change", () => { s.browser = sel.value; saveSettings(); });
  if (s.cookiefile) $("cookieFilePath").textContent = s.cookiefile;
  $("cookieFileBtn").addEventListener("click", async () => {
    const p = await api().pick_cookie_file();
    if (p) { s.cookiefile = p; $("cookieFilePath").textContent = p; setFieldError("cookiefile", ""); saveSettings(); }
  });
  $("about").textContent = `yt-dlp ${boot.versions.yt_dlp} · Python ${boot.versions.python}`;

  // Keyboard
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDrawer();
    if (e.key === "Enter" && e.ctrlKey && document.activeElement !== url) go();
  });

  setDuration(null, []);
  updateGoLabel();
  url.focus();
}

if (window.pywebview && window.pywebview.api && window.pywebview.api.init) init();
else window.addEventListener("pywebviewready", init, { once: true });
