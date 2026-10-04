/* MeshCore Channel Cracker — front-end logic.
   Single page, no build step. Three views (Pending, Channel, Config) switched
   in the main pane; a sidebar lists pending + cracked channels. Message views
   are virtualized and paginated so thousands of rows never choke the DOM. */

"use strict";

const $ = (id) => document.getElementById(id);

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}
async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return r.json();
}
function esc(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }
function hex2(n) { return "0x" + (n || 0).toString(16).padStart(2, "0"); }
function fmtInt(n) { return (n || 0).toLocaleString(); }
function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function fmtBig(n) {
  if (n >= 1e12) return (n / 1e12).toFixed(1) + "T";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
}

let toastTimer = null;
function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), 3200);
}

/* ---------------- App state ---------------- */

const state = {
  view: "pending",          // pending | channel | config
  activeChannel: null,
  config: null,
  pending: [],
  channels: [],
  cracking: false,
};

/* ---------------- Theme ---------------- */

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("cracker-theme"); } catch (e) {}
  if (saved === "light" || saved === "dark") document.documentElement.setAttribute("data-theme", saved);
  updateThemeIcon();
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme");
  const isDark = cur ? cur === "dark" : !matchMedia("(prefers-color-scheme: light)").matches;
  const next = isDark ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("cracker-theme", next); } catch (e) {}
  updateThemeIcon();
}
function updateThemeIcon() {
  const cur = document.documentElement.getAttribute("data-theme");
  const isDark = cur ? cur === "dark" : !matchMedia("(prefers-color-scheme: light)").matches;
  $("theme-btn").textContent = isDark ? "☀" : "☽";
}

/* ---------------- Top bar / config header ---------------- */

async function refreshConfig() {
  const cfg = await getJSON("/api/config");
  state.config = cfg;
  // engine badge
  const b = $("engine-badge");
  b.textContent = cfg.engine.toUpperCase();
  b.className = "badge " + cfg.engine;
  b.title = cfg.gpu.name ? cfg.gpu.name : (cfg.engine === "cpu" ? "CPU fallback" : "");
  // mode badge
  const m = $("mode-badge");
  m.innerHTML = '<span class="dot"></span>' + (cfg.live ? "LIVE" : "OFFLINE");
  m.className = "badge " + (cfg.live ? "live" : "offline");
  // db path
  $("db-path").textContent = cfg.db_path;
  $("db-path").title = cfg.db_path;
  return cfg;
}

/* ---------------- Sidebar ---------------- */

function renderSidebar() {
  // nav items
  const pendingCount = state.pending.length;
  $("nav-pending").classList.toggle("active", state.view === "pending");
  $("nav-pending-count").textContent = pendingCount;
  $("nav-config").classList.toggle("active", state.view === "config");

  const el = $("nav-channels");
  if (!state.channels.length) {
    el.innerHTML = '<div class="empty" style="padding:8px">none yet</div>';
    return;
  }
  el.innerHTML = "";
  for (const c of state.channels) {
    const btn = document.createElement("button");
    btn.className = "nav-item" + (state.view === "channel" && state.activeChannel === c.channel_name ? " active" : "");
    btn.innerHTML = `<span class="nm">${esc(c.channel_name)}</span>`
      + `<span class="count">${fmtInt(c.msg_count)}</span>`;
    btn.onclick = () => openChannel(c.channel_name);
    el.appendChild(btn);
  }
}

/* ---------------- View switching ---------------- */

function setView(v) {
  state.view = v;
  for (const id of ["view-pending", "view-channel", "view-config"]) {
    $(id).style.display = ("view-" + v === id) ? "" : "none";
  }
  renderSidebar();
}

function showPending() { setView("pending"); refreshPending(); }
function showConfig() { setView("config"); renderConfig(); }

/* ---------------- Pending view ---------------- */

async function refreshPending() {
  try {
    state.pending = await getJSON("/api/pending");
  } catch (e) { state.pending = []; }
  renderSidebar();
  const el = $("pending-body");
  if (!state.pending.length) {
    el.innerHTML = '<div class="empty">No pending channels — everything decodes.</div>';
    return;
  }
  let html = "<table><thead><tr><th>hash</th><th>packets</th><th>undecoded</th><th></th></tr></thead><tbody>";
  for (const p of state.pending) {
    html += `<tr>
      <td><code class="hash">${hex2(p.hash)}</code> <span class="faint">(${p.hash})</span></td>
      <td>${fmtInt(p.packet_count)}</td>
      <td>${fmtInt(p.undecoded_count)}</td>
      <td class="nowrap"><button class="sm crack-btn" data-hash="${p.hash}">Crack</button></td>
    </tr>`;
  }
  html += "</tbody></table>";
  el.innerHTML = html;
  for (const btn of el.querySelectorAll(".crack-btn")) {
    btn.onclick = () => startCrack(parseInt(btn.dataset.hash, 10));
  }
}

/* ---------------- Crack actions + progress ---------------- */

function setCrackButtons(disabled) {
  // Queue accepts jobs anytime; no need to disable the crack buttons.
}

async function startCrack(hash) {
  const res = await postJSON("/api/crack", { hash });
  if (res.started === false) { toast(res.error || "could not queue", true); return; }
  toast(res.position > 1 ? `Queued (#${res.position})` : "Cracking…");
  pollStatus();
}

async function startCrackHash() {
  const h = parseInt($("m-hash").value, 10);
  if (isNaN(h) || h < 0 || h > 255) { toast("Enter a hash 0–255", true); return; }
  startCrack(h);
}

async function startCrackPacket() {
  const hex = $("p-hex").value.trim();
  if (!hex) { toast("Paste packet hex first", true); return; }
  const res = await postJSON("/api/crack/packet", { hex });
  if (res.started === false) { toast(res.error || "could not queue", true); return; }
  toast("Queued pasted packet");
  pollStatus();
}

async function cancelJob(id) {
  await postJSON("/api/crack/cancel", { job_id: id });
  pollStatus();
}

async function cancelAllJobs() {
  await postJSON("/api/crack/cancel", { all: true });
  pollStatus();
}

const JOB_STATE = { queued: "queued", running: "running", done: "done",
                    canceled: "canceled", failed: "failed" };

function renderQueue(st) {
  const el = $("queue");
  const active = st.active;
  const queued = st.queued || [];
  const recent = st.recent || [];
  const rows = [];
  if (active) rows.push(jobRow(active, true));
  for (const j of queued) rows.push(jobRow(j, true));
  // show a few recent finished jobs for context (no cancel)
  for (const j of recent.slice(0, 4)) rows.push(jobRow(j, false));
  el.innerHTML = rows.length ? `<div class="queue-list">${rows.join("")}</div>` : "";
  $("q-cancel-all").style.display = (active || queued.length) ? "" : "none";
  for (const b of el.querySelectorAll("[data-cancel]")) {
    b.onclick = () => cancelJob(parseInt(b.getAttribute("data-cancel"), 10));
  }
}

function jobRow(j, cancelable) {
  const srcTag = j.source && j.source !== "manual" ? ` <span class="muted">(${esc(j.source)})</span>` : "";
  let right = `<span class="qstate ${j.status}">${j.status}</span>`;
  if (j.status === "done" && j.result && j.result.cracked) {
    right = `<span class="qstate done">✅ ${esc(j.result.channel_name)}</span>`;
  } else if (j.status === "done") {
    right = `<span class="qstate failed">not found</span>`;
  }
  if (cancelable && (j.status === "queued" || j.status === "running")) {
    right += ` <button class="qcancel" data-cancel="${j.id}" title="Cancel">✕</button>`;
  }
  return `<div class="qrow"><span>${esc(j.label)}${srcTag}</span><span>${right}</span></div>`;
}

function renderProgress(st) {
  const el = $("progress");
  if (st.running) {
    el.className = "progress running";
    const total = fmtInt(st.total || 0);
    el.innerHTML = `<span class="spin">◐</span>`
      + `<div class="bar"><span style="width:${st.max_length ? Math.min(100, (st.length / st.max_length) * 100) : 0}%"></span></div>`
      + `<span class="nowrap">${(st.engine || "").toUpperCase()} · hash ${st.target_hash} · len ${st.length}/${st.max_length || "?"} · ${total} tried · ${(st.elapsed || 0).toFixed(1)}s</span>`;
  } else if (st.result) {
    const r = st.result;
    if (r.cracked) {
      el.className = "progress ok";
      const via = r.method === "dictionary" ? "catalog"
        : (r.method === "bruteforce" ? `${(st.engine || "").toUpperCase()} brute-force` : "");
      el.innerHTML = `✅ Cracked <b>${esc(r.channel_name)}</b> (${hex2(r.channel_hash)})`
        + (via ? ` via ${via}` : "") + ` — ${fmtInt(r.decoded_count)} messages decoded.`;
    } else {
      el.className = "progress err";
      el.innerHTML = `❌ Hash ${r.channel_hash} not cracked`
        + (r.error ? ` — ${esc(r.error)}` : " (search exhausted).");
    }
  } else {
    el.className = "progress";
    el.textContent = "Idle.";
  }
}

let _pollScheduled = false;
async function pollStatus() {
  let st;
  try { st = await getJSON("/api/crack/status"); } catch (e) { st = { running: false }; }
  renderProgress(st);
  renderQueue(st);
  const busy = st.running || (st.queued && st.queued.length) || st.active;
  if (busy) {
    if (!_pollScheduled) { _pollScheduled = true; setTimeout(() => { _pollScheduled = false; pollStatus(); }, 500); }
  } else {
    state.cracking = false;
    await refreshChannels();
    await refreshPending();
    if (state.view === "channel" && state.activeChannel) {
      msgView.reload();  // a crack may have decoded new rows for the open channel
    }
  }
}

/* ---------------- Channels list (cracked) ---------------- */

async function refreshChannels() {
  try {
    state.channels = await getJSON("/api/channels");
  } catch (e) { state.channels = []; }
  renderSidebar();
}

/* ---------------- Channel message view (virtualized) ---------------- */

const msgView = (function () {
  const ROW_EST = 34;        // estimated row height (px) for the spacer math
  const WINDOW = 60;         // rows rendered around the viewport
  const PAGE = 80;           // rows fetched per page
  let channel = null;
  let search = "";
  let rows = [];             // newest-first
  let total = 0;
  let oldestId = null;       // cursor for paging back
  let newestId = null;       // cursor for incremental live fetch
  let hasMore = true;
  let loading = false;
  let avgRow = ROW_EST;
  let winStart = 0;
  let scrollEl, listEl, topSpacer, bottomSpacer, rowsHost;

  function mount() {
    scrollEl = $("msg-scroll");
    listEl = $("msg-list");
    scrollEl.onscroll = onScroll;
  }

  async function open(name) {
    channel = name;
    search = $("msg-search").value.trim();
    rows = []; total = 0; oldestId = null; newestId = null; hasMore = true; winStart = 0;
    $("msg-title").textContent = name;
    $("msg-list").innerHTML = '<div class="loading">Loading…</div>';
    await loadOlder(true);
  }

  async function reload() { if (channel) await open(channel); }

  function url(params) {
    const u = new URLSearchParams({ channel });
    if (search) u.set("search", search);
    for (const k in params) if (params[k] != null) u.set(k, params[k]);
    return "/api/channels/messages?" + u.toString();
  }

  async function loadOlder(first) {
    if (loading || (!first && !hasMore)) return;
    loading = true;
    try {
      const data = await getJSON(url({ limit: PAGE, before_id: first ? null : oldestId }));
      total = data.total;
      hasMore = data.has_more;
      if (data.messages.length) {
        rows = rows.concat(data.messages);           // older rows appended (newest-first list)
        oldestId = data.oldest_id;
        if (first) newestId = data.newest_id;
      } else {
        hasMore = false;
      }
    } catch (e) {
      toast("Failed to load messages: " + e.message, true);
      hasMore = false;
    } finally {
      loading = false;
    }
    renderStats();
    render();
  }

  // Fetch rows newer than our newest cursor and prepend them (live updates).
  async function loadNewer() {
    if (loading || !channel || newestId == null) return;
    try {
      const data = await getJSON(url({ limit: 200, after_id: newestId }));
      if (data.messages && data.messages.length) {
        // messages are newest-first; prepend preserving order
        rows = data.messages.concat(rows);
        newestId = data.newest_id;
        total = data.total;
        renderStats();
        render();
      } else if (data.total !== total) {
        total = data.total; renderStats();
      }
    } catch (e) { /* ignore transient poll errors */ }
  }

  function renderStats() {
    const s = search ? ` matching “${esc(search)}”` : "";
    $("msg-stats").innerHTML = `${fmtInt(total)} message${total === 1 ? "" : "s"}${s}`;
  }

  function onScroll() {
    const st = scrollEl.scrollTop;
    const newWin = Math.max(0, Math.floor(st / avgRow) - 8);
    if (Math.abs(newWin - winStart) >= 8) { winStart = newWin; render(); }
    // Near the bottom: fetch older rows.
    if (st + scrollEl.clientHeight >= scrollEl.scrollHeight - 400) loadOlder(false);
  }

  function render() {
    if (!rows.length) {
      listEl.innerHTML = '<div class="empty">' + (search ? "No messages match." : "No messages yet.") + "</div>";
      return;
    }
    const start = Math.min(winStart, Math.max(0, rows.length - WINDOW));
    const end = Math.min(rows.length, start + WINDOW);
    let html = `<div style="height:${start * avgRow}px"></div>`;
    for (let i = start; i < end; i++) html += rowHTML(rows[i]);
    html += `<div style="height:${(rows.length - end) * avgRow}px"></div>`;
    if (loading) html += '<div class="loading">Loading more…</div>';
    listEl.innerHTML = html;
    measure(start, end);
  }

  function measure(start, end) {
    // Refine avgRow from rendered rows so the spacers stay roughly accurate.
    const sample = listEl.querySelectorAll(".msg");
    if (sample.length) {
      let h = 0;
      for (const r of sample) h += r.offsetHeight;
      const a = h / sample.length;
      if (a > 0 && Math.abs(a - avgRow) > 2) avgRow = a;
    }
  }

  function rowHTML(m) {
    let text = esc(m.text || "");
    if (search) {
      try {
        const re = new RegExp("(" + search.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "ig");
        text = esc(m.text || "").replace(re, "<mark>$1</mark>");
      } catch (e) {}
    }
    return `<div class="msg">`
      + `<span class="sender">${esc(m.sender || "?")}</span>`
      + `<span class="text">${text}</span>`
      + `<span class="ts">${fmtTime(m.timestamp)}</span></div>`;
  }

  function doSearch() { if (channel) open(channel); }

  return { mount, open, reload, loadNewer, doSearch };
})();

function openChannel(name) {
  state.activeChannel = name;
  setView("channel");
  msgView.open(name);
}

/* ---------------- Config view ---------------- */

function renderConfig() {
  const cfg = state.config;
  if (!cfg) return;
  const s = cfg.settings;

  // engine
  $("cfg-engine").value = s.engine;
  $("cfg-engine-gpu").textContent = cfg.gpu.available
    ? "GPU available" + (cfg.gpu.name ? ` (${cfg.gpu.name})` : "")
    : (cfg.gpu.forced_cpu ? "GPU disabled (--cpu)" : "No GPU detected");
  $("cfg-engine-gpu").className = "field-hint " + (cfg.gpu.available ? "" : "warn-note");
  $("cfg-engine-opt-gpu").disabled = !cfg.gpu.available;

  // brute-force params
  $("cfg-charset").value = s.charset;
  $("cfg-maxlen").value = s.max_length;
  updateSearchEstimate();

  // wordlist
  $("wl-total").textContent = fmtInt(cfg.wordlist.total);
  $("wl-builtin").textContent = fmtInt(cfg.wordlist.builtin);
  $("wl-catalog").textContent = fmtInt(cfg.wordlist.catalog);
  $("cfg-catalog").checked = cfg.wordlist.catalog_enabled;
  const files = cfg.wordlist.custom_files || [];
  $("wl-custom").innerHTML = files.length
    ? files.map((f) => `<div class="faint mono" style="font-size:12px">+ ${esc(f)}</div>`).join("")
    : '<span class="faint">none</span>';

  // auto-crack
  $("cfg-auto").checked = s.auto_crack;
  $("cfg-auto-dict").checked = s.auto_dict_only;
  $("cfg-auto-dict").disabled = !s.auto_crack;
  $("auto-state").textContent = cfg.auto_running ? "running" : "idle";
}

function updateSearchEstimate() {
  const charset = $("cfg-charset").value || "";
  const maxlen = parseInt($("cfg-maxlen").value, 10) || 1;
  const base = new Set([...charset].filter((c) => !/\s/.test(c))).size;
  let total = 0;
  for (let L = 1; L <= maxlen; L++) total += Math.pow(base, L);
  $("cfg-estimate").textContent = base ? `${fmtBig(total)} candidates (${base} chars)` : "—";
  const warn = total > (state.config ? state.config.search_warn_threshold : 5e9);
  $("cfg-estimate-warn").style.display = warn ? "" : "none";
}

async function saveEngine() {
  await applyConfig({ engine: $("cfg-engine").value });
}
async function saveBrute() {
  await applyConfig({
    charset: $("cfg-charset").value,
    max_length: parseInt($("cfg-maxlen").value, 10),
  });
  toast("Brute-force defaults saved");
}
async function toggleCatalog() {
  await applyConfig({ use_catalog: $("cfg-catalog").checked });
}
async function toggleAuto() {
  await applyConfig({ auto_crack: $("cfg-auto").checked });
  pollStatus();  // reflect auto-queued jobs immediately
}
async function toggleAutoDict() {
  await applyConfig({ auto_dict_only: $("cfg-auto-dict").checked });
}

async function applyConfig(patch) {
  try {
    state.config = await postJSON("/api/config", patch);
    await refreshConfig();
    renderConfig();
  } catch (e) { toast("Save failed: " + e.message, true); }
}

async function addWordlist() {
  const text = $("wl-text").value.trim();
  const path = $("wl-path").value.trim();
  if (!text && !path) { toast("Paste words or enter a path", true); return; }
  const res = await postJSON("/api/wordlist", { text: text || null, path: path || null });
  if (res.ok === false) { toast(res.error || "failed", true); return; }
  toast(`Added ${fmtInt(res.added)} candidates`);
  $("wl-text").value = ""; $("wl-path").value = "";
  await refreshConfig();
  renderConfig();
}

/* ---------------- Keyboard ---------------- */

function onKey(e) {
  if (e.target.matches("input, textarea, select")) {
    if (e.key === "Enter" && e.target.id === "msg-search") { e.preventDefault(); msgView.doSearch(); }
    if (e.key === "Enter" && e.target.id === "m-hash") { e.preventDefault(); startCrackHash(); }
    if (e.key === "Escape") e.target.blur();
    return;
  }
  if (e.key === "/") { e.preventDefault(); if (state.view === "channel") $("msg-search").focus(); }
  else if (e.key === "p") showPending();
  else if (e.key === "c") showConfig();
  else if (e.key === "t") toggleTheme();
}

/* ---------------- Init ---------------- */

async function init() {
  initTheme();
  msgView.mount();

  $("theme-btn").onclick = toggleTheme;
  $("nav-pending").onclick = showPending;
  $("nav-config").onclick = showConfig;
  $("m-go").onclick = startCrackHash;
  $("p-go").onclick = startCrackPacket;
  $("msg-search-btn").onclick = () => msgView.doSearch();
  $("cfg-engine").onchange = saveEngine;
  $("cfg-charset").oninput = updateSearchEstimate;
  $("cfg-maxlen").oninput = updateSearchEstimate;
  $("cfg-charset").onchange = saveBrute;
  $("cfg-maxlen").onchange = saveBrute;
  $("cfg-save-brute").onclick = saveBrute;
  $("cfg-catalog").onchange = toggleCatalog;
  $("cfg-auto").onchange = toggleAuto;
  $("cfg-auto-dict").onchange = toggleAutoDict;
  $("q-cancel-all").onclick = cancelAllJobs;
  $("wl-add").onclick = addWordlist;
  document.addEventListener("keydown", onKey);

  await refreshConfig();
  await refreshChannels();
  setView("pending");
  await refreshPending();
  await pollStatus();                 // reflect any in-flight / last result

  // Periodic incremental refresh (polling, but cheap).
  setInterval(() => {
    if (state.cracking) return;
    refreshPending();
    refreshChannels();
    if (state.view === "channel") msgView.loadNewer();
  }, 5000);
}

document.addEventListener("DOMContentLoaded", init);
