/* MeshCore Channel Cracker — front-end logic.
   Single page, no build step.

   Model:
     - A CHANNEL is one entity with a STATE (named | public | unknown | exhausted),
       plus a transient "cracking" overlay while a job targets it.
     - A channel HASH is a single cleartext byte (0–255) and a *colliding* identifier:
       many channels can share one byte. Unknown/Exhausted rows are shown by hash.
     - PACKETS are the encrypted frames seen on a hash; MESSAGES are the ones we've
       decoded. They are distinct and shown as two columns everywhere.

   Three views switch in the main pane: Channels (unified list), Channel (detail with
   virtualized message stream), Config. */

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
function hex2(n) { return "0x" + (n || 0).toString(16).padStart(2, "0").toUpperCase(); }
function fmtInt(n) { return (n || 0).toLocaleString(); }
function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function fmtAgo(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}
function fmtBig(n) {
  if (n >= 1e18) return (n / 1e18).toFixed(1) + "E";
  if (n >= 1e15) return (n / 1e15).toFixed(1) + "P";
  if (n >= 1e12) return (n / 1e12).toFixed(1) + "T";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(Math.round(n));
}
function fmtEta(s) {
  if (!isFinite(s)) return "∞";
  if (s < 1) return "<1s";
  if (s < 90) return s.toFixed(0) + "s";
  if (s < 5400) return (s / 60).toFixed(0) + " min";
  if (s < 172800) return (s / 3600).toFixed(1) + " h";
  if (s < 3.15e7) return (s / 86400).toFixed(0) + " days";
  return (s / 3.15e7).toFixed(1) + " yr";
}

let toastTimer = null;
function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), 3200);
}

/* ---------------- Method badges ---------------- */

// Map a backend "method" to a short human label + css modifier.
function methodBadge(method) {
  switch (method) {
    case "dictionary": return { label: "catalog", cls: "dict" };
    case "rules":      return { label: "rules", cls: "rules" };
    case "bruteforce": return { label: "GPU brute-force", cls: "brute" };
    case "multiword":  return { label: "word-combo", cls: "multiword" };
    case "public":     return { label: "public PSK", cls: "public" };
    default:           return { label: "recovered", cls: "unk" };
  }
}

/* ---------------- App state ---------------- */

const state = {
  view: "channels",            // channels | channel | config
  active: null,                // active detail entry (unified row object)
  config: null,
  channels: [],                // /api/channels
  pending: [],                 // /api/pending
  exhausted: [],               // /api/exhausted
  crackStatus: null,           // /api/crack/status
  entries: [],                 // unified, computed
  filter: "all",               // all | named | unknown | cracking | exhausted | public
  sort: "packets",             // packets | recent
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
  const b = $("engine-badge");
  b.textContent = cfg.engine.toUpperCase();
  b.className = "badge " + cfg.engine;
  b.title = cfg.gpu.name ? cfg.gpu.name : (cfg.engine === "cpu" ? "CPU fallback" : "");
  const m = $("mode-badge");
  m.innerHTML = '<span class="dot"></span>' + (cfg.live ? "LIVE" : "OFFLINE");
  m.className = "badge " + (cfg.live ? "live" : "offline");
  $("db-path").textContent = cfg.db_path;
  $("db-path").title = cfg.db_path;
  return cfg;
}

/* ---------------- Device & link health ---------------- */

function fmtDur(secs) {
  if (secs == null) return "—";
  secs = Math.max(0, Math.round(secs));
  if (secs < 60) return secs + "s";
  if (secs < 3600) return Math.floor(secs / 60) + "m " + (secs % 60) + "s";
  if (secs < 86400) return Math.floor(secs / 3600) + "h " + Math.floor((secs % 3600) / 60) + "m";
  return Math.floor(secs / 86400) + "d " + Math.floor((secs % 86400) / 3600) + "h";
}

// A reboot cause that isn't a clean power-on / external reset is worth flagging.
const BOOT_BAD = new Set(["panic/crash", "interrupt watchdog", "task watchdog", "watchdog", "brownout"]);

async function refreshHealth() {
  let h;
  try { h = await getJSON("/api/health"); } catch (e) { h = null; }
  state.health = h;
  updateFreshBadge(h);
  if (state.view === "health") renderHealth();
  return h;
}

// Topbar indicator: how stale the capture is. This is the at-a-glance answer to
// "why are the messages hours old" — a dropped device shows up here immediately.
function updateFreshBadge(h) {
  const b = $("fresh-badge");
  if (!h || !h.live) { b.style.display = "none"; return; }
  b.style.display = "";
  const last = h.last_packet_at;
  const age = last ? (Date.now() / 1000 - last) : null;
  let cls, label;
  if (!h.connected) { cls = "offline"; label = "LINK DOWN"; }
  else if (age == null) { cls = "warn"; label = "no packets yet"; }
  else if (age > 300) { cls = "warn"; label = "stale " + fmtAgo(last).replace(" ago", ""); }
  else { cls = "live"; label = "fresh"; }
  b.className = "badge " + cls;
  b.innerHTML = '<span class="dot"></span>' + label;
  b.title = last ? ("last packet " + fmtTime(last) + " (" + fmtAgo(last) + ")") : "no packets captured yet";
}

function renderHealth() {
  const h = state.health;
  const sum = $("health-summary");
  if (!h) { sum.textContent = "Unavailable."; return; }
  if (!h.live) {
    sum.textContent = "Offline mode — serving a stored database, no live device link.";
  } else {
    const last = h.last_packet_at;
    sum.innerHTML = (h.connected
        ? '<b class="ok">Connected</b> to the device.'
        : '<b class="bad">Link down</b> — not currently receiving.')
      + " Last packet: " + (last ? esc(fmtTime(last)) + " (" + esc(fmtAgo(last)) + ")" : "none yet") + ".";
  }

  const boots = (h.device_boots || []);
  $("boot-list").innerHTML = boots.length ? boots.map(b => {
    const bad = BOOT_BAD.has(b.reset_name);
    const prev = b.prev_alive
      ? `ran ${fmtDur(b.prev_uptime_secs)} · heap min ${fmtInt(b.prev_heap_min)}B` +
        (b.prev_rssi ? ` · RSSI ${b.prev_rssi}dBm` : "")
      : "cold boot (no prior stats)";
    return `<div class="hrow">
      <span class="state-pill ${bad ? "bad" : ""}">${esc(b.reset_name || "?")}</span>
      <span class="hmain">boot #${esc(b.boot_count)} · <span class="faint">${esc(prev)}</span></span>
      <span class="faint" title="${esc(fmtTime(b.timestamp))}">${esc(fmtAgo(b.timestamp))}</span>
    </div>`;
  }).join("") : '<div class="faint">No reboots recorded yet.</div>';

  const events = (h.connection_events || []);
  $("conn-list").innerHTML = events.length ? events.map(e => {
    const up = e.event === "connected";
    const gap = e.gap_secs != null
      ? (up ? `after ${fmtDur(e.gap_secs)} down` : `up for ${fmtDur(e.gap_secs)}`)
      : "";
    return `<div class="hrow">
      <span class="state-pill ${up ? "ok" : "bad"}">${up ? "up" : "down"}</span>
      <span class="hmain">${esc(e.detail || "")} <span class="faint">${esc(gap)}</span></span>
      <span class="faint" title="${esc(fmtTime(e.timestamp))}">${esc(fmtAgo(e.timestamp))}</span>
    </div>`;
  }).join("") : '<div class="faint">No connection events recorded yet.</div>';
}

function showHealth() { setView("health"); renderHealth(); refreshHealth(); }

/* ---------------- Unified channel model ---------------- */

// Build state.entries from channels + pending + exhausted + crack status.
function buildEntries() {
  // Which hashes are being actively worked right now?
  const crackingHashes = new Set();
  let sweeping = false;
  const st = state.crackStatus;
  if (st) {
    const jobs = [];
    if (st.active) jobs.push(st.active);
    for (const j of (st.queued || [])) jobs.push(j);
    for (const j of jobs) {
      if (j.kind === "sweep") sweeping = true;
      else if (j.kind === "multiword") {
        if (j.target_hash != null) crackingHashes.add(j.target_hash);
        else sweeping = true;
      } else if (j.target_hash != null) crackingHashes.add(j.target_hash);
    }
    if (st.running && st.target_hash != null) crackingHashes.add(st.target_hash);
  }

  const exhMap = new Map();
  for (const e of state.exhausted) exhMap.set(e.hash, e);

  const entries = [];

  // Named + public come from /api/channels.
  for (const c of state.channels) {
    const kind = c.state === "public" ? "public" : "named";
    entries.push({
      key: "name:" + c.channel_name,
      kind,
      name: c.channel_name,
      hash: c.channel_hash,
      method: c.method,
      packets: c.packets,
      messages: c.messages,
      decoded_packets: c.decoded_packets != null ? c.decoded_packets : c.packets,
      // Undecoded traffic belongs to the hash byte (a different channel), not to
      // this channel. shares_hash flags that the byte also carries unknown
      // traffic; the Unknown row for the byte owns the crack.
      shares_hash: !!c.shares_hash,
      undecoded: c.undecoded || 0,
      undecoded_distinct: c.undecoded_distinct || 0,
      unique_senders: c.unique_senders || 0,
      last_activity: c.last_activity,
      cracking: crackingHashes.has(c.channel_hash),
    });
  }

  // Unknown / exhausted come from /api/pending.
  for (const p of state.pending) {
    const isExh = !!p.exhausted;
    // A running sweep only grinds bytes it's actually eligible to crack: not
    // already exhausted (subsumed by a prior/equal sweep) and with >=2 distinct
    // undecoded packets to corroborate a hit. Painting *every* pending byte as
    // "cracking" during a sweep was misleading — it made already-swept bytes
    // (incl. a named channel's collision sibling, e.g. Public's) look like they
    // were being re-cracked.
    const sweepEligible = sweeping && !isExh && (p.undecoded_distinct || 0) >= 2;
    const isCracking = crackingHashes.has(p.hash) || sweepEligible;
    entries.push({
      key: "hash:" + p.hash,
      kind: isExh ? "exhausted" : "unknown",
      name: null,
      hash: p.hash,
      method: null,
      packets: p.packet_count,
      messages: 0,
      undecoded: p.undecoded_count || 0,
      undecoded_distinct: p.undecoded_distinct || 0,
      // Known channel(s) on the same byte, if any — this unknown traffic is a
      // *different* channel colliding with them on the 1-byte hash.
      collides_with: p.collides_with || [],
      unique_senders: 0,
      last_activity: null,
      cracking: isCracking,
      attempt: exhMap.get(p.hash) || null,
    });
  }

  state.entries = entries;
  state.sweeping = sweeping;
  return entries;
}

// Which filter buckets does an entry belong to?
function entryBuckets(e) {
  const b = ["all"];
  b.push(e.kind);              // named | public | unknown | exhausted
  if (e.cracking) b.push("cracking");
  return b;
}

function filteredSorted() {
  const f = state.filter;
  let list = state.entries.filter((e) => entryBuckets(e).includes(f));
  const by = state.sort;
  list = list.slice().sort((a, b) => {
    if (by === "recent") return (b.last_activity || 0) - (a.last_activity || 0);
    return (b.packets || 0) - (a.packets || 0);
  });
  return list;
}

function bucketCounts() {
  const c = { all: 0, named: 0, unknown: 0, cracking: 0, exhausted: 0, public: 0 };
  for (const e of state.entries) {
    for (const b of entryBuckets(e)) if (b in c) c[b]++;
  }
  return c;
}

/* ---------------- Sidebar ---------------- */

function renderSidebar() {
  $("nav-channels").classList.toggle("active", state.view === "channels" || state.view === "channel");
  $("nav-config").classList.toggle("active", state.view === "config");
  $("nav-health").classList.toggle("active", state.view === "health");
  const c = bucketCounts();
  $("nav-channels-count").textContent = c.all;
  $("g-named").textContent = fmtInt(c.named + c.public);
  $("g-unknown").textContent = fmtInt(c.unknown);
  $("g-cracking").textContent = fmtInt(c.cracking);
  $("g-exhausted").textContent = fmtInt(c.exhausted);
}

/* ---------------- View switching ---------------- */

function setView(v) {
  state.view = v;
  for (const id of ["view-channels", "view-channel", "view-config", "view-health"]) {
    $(id).style.display = ("view-" + v === id) ? "" : "none";
  }
  renderSidebar();
}

function showChannels() { setView("channels"); renderChannels(); }
function showConfig() { setView("config"); renderConfig(); }

/* ---------------- Channels (unified list) view ---------------- */

const CHIPS = [
  { id: "all", label: "All" },
  { id: "named", label: "Named" },
  { id: "unknown", label: "Unknown" },
  { id: "cracking", label: "Cracking" },
  { id: "exhausted", label: "Exhausted" },
  { id: "public", label: "Public" },
];

function renderChips() {
  const counts = bucketCounts();
  const el = $("chips");
  el.innerHTML = "";
  for (const c of CHIPS) {
    const btn = document.createElement("button");
    btn.className = "chip" + (state.filter === c.id ? " active" : "")
      + (c.id === "cracking" && counts.cracking ? " live" : "");
    btn.innerHTML = `${esc(c.label)}<span class="chip-count">${counts[c.id] || 0}</span>`;
    btn.onclick = () => { state.filter = c.id; renderChannels(); };
    el.appendChild(btn);
  }
}

function renderChannels() {
  buildEntries();
  renderChips();
  renderSidebar();
  const el = $("channel-list");
  const list = filteredSorted();
  if (!list.length) {
    el.innerHTML = '<div class="empty">' + emptyCopy() + "</div>";
    return;
  }
  el.innerHTML = "";
  for (const e of list) el.appendChild(channelRow(e));
}

function emptyCopy() {
  switch (state.filter) {
    case "named": return "No named channels recovered yet.";
    case "unknown": return "No unknown channels — everything on the mesh decodes.";
    case "cracking": return "Nothing is being cracked right now.";
    case "exhausted": return "No exhausted hashes.";
    case "public": return "No public channels seen.";
    default: return "No channels seen yet.";
  }
}

function channelRow(e) {
  const row = document.createElement("div");
  row.className = "chrow " + e.kind + (e.cracking ? " cracking" : "");

  // Identifier / identity column.
  let ident;
  if (e.kind === "unknown" || e.kind === "exhausted") {
    ident = `<span class="ident hash-ident">hash <code>${hex2(e.hash)}</code>`
      + `<span class="faint dec">(${e.hash})</span></span>`;
  } else {
    ident = `<span class="ident name-ident">${esc(e.name)}</span>`;
  }

  // State / method tag.
  let tag = "";
  if (e.kind === "named" || e.kind === "public") {
    const mb = methodBadge(e.method);
    tag = `<span class="method-badge ${mb.cls}">${esc(mb.label)}</span>`;
  } else if (e.kind === "exhausted") {
    tag = `<span class="state-pill exhausted">exhausted</span>`;
  } else {
    tag = `<span class="state-pill unknown">unknown</span>`;
  }
  if (e.cracking) tag += `<span class="state-pill cracking"><span class="spin">◐</span> cracking</span>`;

  const named = e.kind === "named" || e.kind === "public";

  // Packets + messages columns. For a named channel these count only its OWN
  // traffic (the key's decoded packets); for an unknown byte, the still-
  // encrypted packet count and a distinct-message estimate (relay floods
  // collapsed) of how much unknown traffic sits on the byte.
  const pktNum = named ? e.decoded_packets : e.undecoded;
  const pktCol = `<span class="num">${fmtInt(pktNum)}</span>`
    + `<span class="col-k">${named ? "packets" : "encrypted"}</span>`;
  const msgCol = named
    ? `<span class="num">${fmtInt(e.messages)}</span><span class="col-k">messages</span>`
    : `<span class="num">~${fmtInt(e.undecoded_distinct)}</span><span class="col-k">unknown msgs</span>`;

  // Collision note. A named channel never has "undecodable" messages of its
  // own; if its 1-byte hash also carries unknown traffic, that is a *different*
  // channel on the same byte. Surface it as a crackable cross-reference, not as
  // this channel's failure. On an unknown byte, name the known channel(s) it
  // collides with.
  let collision = "";
  if (named && e.shares_hash) {
    collision = `<div class="collision-note">Hash byte <code>${hex2(e.hash)}</code> also carries `
      + `<b>~${fmtInt(e.undecoded_distinct)}</b> message(s) from another, un-cracked channel · `
      + `<a href="#" class="go-unknown">crack that channel &#8594;</a></div>`;
  } else if (!named && e.collides_with && e.collides_with.length) {
    collision = `<div class="collision-note faint">Shares hash byte with `
      + `${e.collides_with.map((n) => `<b>${esc(n)}</b>`).join(", ")} `
      + `<span class="faint">(different channel, same 1-byte hash)</span></div>`;
  }

  // Per-row action.
  let action = "";
  if (e.kind === "unknown") {
    action = `<button class="sm row-crack" ${e.cracking ? "disabled" : ""}>Crack</button>`;
  } else if (e.kind === "exhausted") {
    action = `<button class="sm row-retry" ${e.cracking ? "disabled" : ""}>Retry</button>`;
  } else {
    action = `<span class="row-go faint">View &#8594;</span>`;
  }

  row.innerHTML = `
    <div class="chrow-top">
      <div class="chrow-id">${ident} ${tag}</div>
      <div class="chrow-cols">
        <div class="col">${pktCol}</div>
        <div class="col">${msgCol}</div>
        <div class="col meta-col">
          <span class="num small">${e.unique_senders ? fmtInt(e.unique_senders) : "—"}</span><span class="col-k">senders</span>
        </div>
        <div class="col meta-col when"><span class="when-v">${e.last_activity ? fmtAgo(e.last_activity) : "—"}</span><span class="col-k">activity</span></div>
        <div class="col action-col">${action}</div>
      </div>
    </div>
    ${collision}`;

  // Clicks: open detail (except the action button).
  row.onclick = (ev) => {
    if (ev.target.closest("button")) return;
    openEntry(e);
  };
  const cb = row.querySelector(".row-crack");
  if (cb) cb.onclick = (ev) => { ev.stopPropagation(); startCrack(e.hash); };
  const rb = row.querySelector(".row-retry");
  if (rb) rb.onclick = (ev) => { ev.stopPropagation(); retryCrack(e.hash); };
  // "crack that channel" jumps to the Unknown row for this byte.
  const gu = row.querySelector(".go-unknown");
  if (gu) gu.onclick = (ev) => { ev.preventDefault(); ev.stopPropagation(); openHash(e.hash); };
  return row;
}

/* ---------------- Crack actions + progress + queue ---------------- */

async function startCrack(hash) {
  const res = await postJSON("/api/crack", { hash });
  if (res.queued === false || res.started === false) { toast(res.error || "could not queue", true); return; }
  toast(res.position > 1 ? `Queued (#${res.position})` : "Cracking…");
  pollStatus();
}

async function retryCrack(hash) {
  const res = await postJSON("/api/crack/retry", { hash });
  if (res.queued === false || res.started === false || res.ok === false) { toast(res.error || "could not retry", true); return; }
  toast("Retrying hash " + hex2(hash));
  pollStatus();
}

async function startSweep() {
  const res = await postJSON("/api/crack/sweep", {});
  if (res.queued === false || res.started === false) { toast(res.error || "could not start sweep", true); return; }
  toast("Sweeping all pending channels…");
  pollStatus();
}

/* ---------------- Word-combo (multiword) crack ---------------- */

const SEARCH_WARN = 5e11;   // flag runs beyond ~this many candidates as a long grind
let _mwInited = false;
let _mwEstTimer = null;

function mwParams() {
  return {
    n: parseInt($("mw-words").value, 10),
    tier: parseInt($("mw-tier").value, 10),
    hyphen: $("mw-hyphen").checked,
    concat: $("mw-concat").checked,
  };
}

function initMultiword() {
  if (_mwInited) return;
  _mwInited = true;
  const cfg = (state.config && state.config.settings) || {};
  if (cfg.multiword_words) $("mw-words").value = cfg.multiword_words;
  if (cfg.multiword_tier) $("mw-tier").value = String(cfg.multiword_tier);
  if (cfg.multiword_hyphen != null) $("mw-hyphen").checked = !!cfg.multiword_hyphen;
  if (cfg.multiword_concat != null) $("mw-concat").checked = !!cfg.multiword_concat;
  $("mw-words-val").textContent = $("mw-words").value;

  const onChange = (persist) => {
    $("mw-words-val").textContent = $("mw-words").value;
    refreshMwEstimate();
    if (persist) {
      const p = mwParams();
      postJSON("/api/config", {
        multiword_words: p.n, multiword_tier: p.tier,
        multiword_hyphen: p.hyphen, multiword_concat: p.concat,
      });
    }
  };
  $("mw-words").oninput = () => onChange(false);
  $("mw-words").onchange = () => onChange(true);
  $("mw-tier").onchange = () => onChange(true);
  $("mw-hyphen").onchange = () => onChange(true);
  $("mw-concat").onchange = () => onChange(true);
  $("mw-go").onclick = startMultiword;
  refreshMwEstimate();
}

function refreshMwEstimate() {
  clearTimeout(_mwEstTimer);
  _mwEstTimer = setTimeout(async () => {
    const p = mwParams();
    const q = `?n=${p.n}&tier=${p.tier}&concat=${p.concat ? 1 : 0}&hyphen=${p.hyphen ? 1 : 0}`;
    let e;
    try { e = await getJSON("/api/multiword" + q); } catch (_) { return; }
    $("mw-estimate").textContent = fmtBig(e.under_cap) + " candidates";
    $("mw-eta").textContent = fmtEta(e.eta_seconds) + " on GPU";
    $("mw-raw").textContent = e.patterns
      ? ` (${fmtBig(e.raw)} before the 30-char cap)` : "";
    $("mw-warn").style.display = e.under_cap > SEARCH_WARN ? "" : "none";
    const noGpu = !e.gpu;
    $("mw-nogpu").style.display = noGpu ? "" : "none";
    $("mw-go").disabled = noGpu;
    $("mw-gpu-badge").classList.toggle("off", noGpu);
  }, 120);
}

async function startMultiword() {
  const p = mwParams();
  const res = await postJSON("/api/crack/multiword", {
    n: p.n, tier: p.tier, concat: p.concat, hyphen: p.hyphen,
  });
  if (res.queued === false) { toast(res.reason || res.error || "could not queue", true); return; }
  toast(`Word-combo crack queued (${p.n} words, top ${fmtBig(p.tier)})`);
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
  if (res.queued === false || res.started === false) { toast(res.error || "could not queue", true); return; }
  toast("Queued pasted packet");
  pollStatus();
}

async function cancelJob(id) { await postJSON("/api/crack/cancel", { job_id: id }); pollStatus(); }
async function cancelAllJobs() { await postJSON("/api/crack/cancel", { all: true }); pollStatus(); }

function renderQueue(st) {
  const el = $("queue");
  const active = st.active;
  const queued = st.queued || [];
  const recent = st.recent || [];
  const rows = [];
  if (active) rows.push(jobRow(active, true, st));
  for (const j of queued) rows.push(jobRow(j, true, st));
  for (const j of recent.slice(0, 4)) rows.push(jobRow(j, false, st));
  el.innerHTML = rows.length ? `<div class="queue-list">${rows.join("")}</div>` : "";
  $("q-cancel-all").style.display = (active || queued.length) ? "" : "none";
  for (const b of el.querySelectorAll("[data-cancel]")) {
    b.onclick = () => cancelJob(parseInt(b.getAttribute("data-cancel"), 10));
  }
}

function jobLabel(j, st) {
  if (j.kind === "sweep") {
    // Sweep of all pending channels.
    if (j.status === "running" && st && st.running) {
      const prog = `len ${st.length}/${st.max_length || "?"} · ${fmtInt(st.total || 0)} tried`;
      return `Sweeping pending channels <span class="muted">(${prog})</span>`;
    }
    return `Sweep all pending channels`;
  }
  if (j.kind === "multiword") {
    const scope = j.target_hash != null ? `hash ${hex2(j.target_hash)}` : "all pending";
    if (j.status === "running" && st && st.running) {
      return `Word-combo crack (${scope}) <span class="muted">(${fmtInt(st.total || 0)} tried · ${(st.elapsed || 0).toFixed(0)}s)</span>`;
    }
    return `Word-combo crack (${scope})`;
  }
  // Single hash.
  const h = j.target_hash;
  return `Crack hash ${h != null ? hex2(h) : ""}`;
}

function jobRow(j, cancelable, st) {
  const srcTag = j.source && j.source !== "manual" ? ` <span class="muted">(${esc(j.source)})</span>` : "";
  let right = `<span class="qstate ${j.status}">${esc(j.status)}</span>`;
  const r = j.result;
  if (j.status === "done" && r) {
    if ((j.kind === "sweep" && r.method === "sweep") ||
        (j.kind === "multiword" && r.method === "multiword")) {
      const names = (r.names || []).map(esc).join(", ");
      right = `<span class="qstate done">✅ ${fmtInt(r.found || 0)} found${names ? " · " + names : ""}</span>`;
    } else if (r.cracked) {
      right = `<span class="qstate done">✅ ${esc(r.channel_name)}</span>`;
    } else {
      right = `<span class="qstate failed">not found</span>`;
    }
  }
  if (cancelable && (j.status === "queued" || j.status === "running")) {
    right += ` <button class="qcancel" data-cancel="${j.id}" title="Cancel">✕</button>`;
  }
  return `<div class="qrow"><span>${jobLabel(j, st)}${srcTag}</span><span>${right}</span></div>`;
}

function renderProgress(st) {
  const el = $("progress");
  const activeKind = st.active && st.active.kind;
  if (st.running) {
    el.className = "progress running";
    const total = fmtInt(st.total || 0);
    if (activeKind === "multiword") {
      // No meaningful length/total ratio for a word sweep; show tried + elapsed.
      el.innerHTML = `<span class="spin">◐</span>`
        + `<div class="bar indet"><span></span></div>`
        + `<span class="nowrap">${(st.engine || "").toUpperCase()} · word-combo · ${total} tried · ${(st.elapsed || 0).toFixed(1)}s</span>`;
    } else {
      const who = st.target_hash != null ? "hash " + hex2(st.target_hash) : "sweep";
      el.innerHTML = `<span class="spin">◐</span>`
        + `<div class="bar"><span style="width:${st.max_length ? Math.min(100, (st.length / st.max_length) * 100) : 0}%"></span></div>`
        + `<span class="nowrap">${(st.engine || "").toUpperCase()} · ${who} · len ${st.length}/${st.max_length || "?"} · ${total} tried · ${(st.elapsed || 0).toFixed(1)}s</span>`;
    }
  } else if (st.result) {
    const r = st.result;
    if (r.method === "sweep" || r.method === "multiword") {
      const kindWord = r.method === "multiword" ? "Word-combo crack" : "Sweep";
      el.className = r.found ? "progress ok" : "progress";
      const names = (r.names || []).map(esc).join(", ");
      el.innerHTML = r.found
        ? `✅ ${kindWord} recovered <b>${fmtInt(r.found)}</b> channel${r.found === 1 ? "" : "s"}${names ? ` — ${names}` : ""}.`
        : (r.error ? `${kindWord} failed — ${esc(r.error)}` : `${kindWord} finished — no new channels recovered.`);
    } else if (r.cracked) {
      el.className = "progress ok";
      const mb = methodBadge(r.method);
      el.innerHTML = `✅ Cracked <b>${esc(r.channel_name)}</b> (${hex2(r.channel_hash)}) via ${esc(mb.label)}`
        + ` — ${fmtInt(r.decoded_count)} messages decoded.`;
    } else if (r.need_more_packets) {
      el.className = "progress";
      el.innerHTML = `Hash ${hex2(r.channel_hash)}: only one encrypted packet seen — `
        + `need at least 2 to brute-force safely. Tried the dictionary/rules; waiting for more traffic.`;
    } else {
      el.className = "progress err";
      el.innerHTML = `❌ Hash ${r.channel_hash != null ? hex2(r.channel_hash) : "?"} not cracked`
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
  state.crackStatus = st;
  renderProgress(st);
  renderQueue(st);
  const busy = st.running || (st.queued && st.queued.length) || st.active;
  state.cracking = !!busy;
  // Keep the unified list's cracking overlay fresh while jobs run.
  if (state.view === "channels") renderChannels();
  if (state.view === "channel" && state.active) refreshDetailState();
  if (busy) {
    if (!_pollScheduled) { _pollScheduled = true; setTimeout(() => { _pollScheduled = false; pollStatus(); }, 500); }
  } else {
    await refreshData();
    if (state.view === "channels") renderChannels();
    if (state.view === "channel" && state.active) {
      await refreshDetailState();
      if (state.active.kind === "named" || state.active.kind === "public") msgView.reload();
    }
  }
}

/* ---------------- Data refresh ---------------- */

async function refreshData() {
  try { state.channels = await getJSON("/api/channels"); } catch (e) { state.channels = []; }
  try { state.pending = await getJSON("/api/pending"); } catch (e) { state.pending = []; }
  try { state.exhausted = await getJSON("/api/exhausted"); } catch (e) { state.exhausted = []; }
  buildEntries();
  renderSidebar();
}

/* ---------------- Channel detail view ---------------- */

function openEntry(e) {
  state.active = e;
  setView("channel");
  renderDetail(e);
  if (e.kind === "named" || e.kind === "public") {
    $("d-msg-wrap").style.display = "";
    msgView.open(e.name);
  } else {
    $("d-msg-wrap").style.display = "none";
  }
}

// Open the Unknown/Exhausted entry for a hash byte (the crack target), e.g. from
// a named channel's "crack that channel" collision cross-reference.
function openHash(hash) {
  const target = state.entries.find(
    (x) => x.hash === hash && (x.kind === "unknown" || x.kind === "exhausted"));
  if (target) openEntry(target);
  else startCrack(hash);  // not yet in the list — just queue it
}

// Re-locate the active entry in fresh data (its state may have changed after a crack).
async function refreshDetailState() {
  if (!state.active) return;
  buildEntries();
  let next = null;
  if (state.active.name) next = state.entries.find((x) => x.name === state.active.name);
  if (!next && state.active.hash != null) next = state.entries.find((x) => x.hash === state.active.hash);
  if (next) {
    const wasEncrypted = state.active.kind === "unknown" || state.active.kind === "exhausted";
    state.active = next;
    renderDetail(next);
    // A hash we were viewing just got a name — load its messages.
    if (wasEncrypted && (next.kind === "named" || next.kind === "public")) {
      $("d-msg-wrap").style.display = "";
      msgView.open(next.name);
    }
  }
}

function renderDetail(e) {
  const encrypted = e.kind === "unknown" || e.kind === "exhausted";
  // Title.
  if (encrypted) {
    $("d-title").innerHTML = `hash <code>${hex2(e.hash)}</code> <span class="faint dec">(${e.hash})</span>`;
  } else {
    $("d-title").textContent = e.name;
  }
  // Method badge.
  const mb = $("d-method");
  if (e.kind === "named" || e.kind === "public") {
    const b = methodBadge(e.method);
    mb.textContent = b.label;
    mb.className = "method-badge " + b.cls;
    mb.style.display = "";
  } else {
    mb.style.display = "none";
  }
  // State pill.
  const sp = $("d-state");
  const stateLabel = { named: "named", public: "public", unknown: "unknown", exhausted: "exhausted" }[e.kind];
  sp.textContent = e.cracking ? "cracking…" : stateLabel;
  sp.className = "state-pill " + (e.cracking ? "cracking" : e.kind);

  // Collision cross-reference. A named channel decodes all of its own traffic;
  // leftover undecoded packets on its 1-byte hash are a *different*, un-cracked
  // channel. Offer to crack that one instead of implying this channel failed.
  // On an unknown byte, name the known channel(s) it collides with.
  const col = $("d-collision");
  if (!encrypted && e.shares_hash) {
    col.style.display = "";
    col.innerHTML = `Hash byte <code>${hex2(e.hash)}</code> also carries `
      + `<b>~${fmtInt(e.undecoded_distinct)}</b> message(s) from another, un-cracked channel. `
      + `<a href="#" id="d-go-unknown">Crack that channel &#8594;</a>`;
    const g = $("d-go-unknown");
    if (g) g.onclick = (ev) => { ev.preventDefault(); openHash(e.hash); };
  } else if (encrypted && e.collides_with && e.collides_with.length) {
    col.style.display = "";
    col.innerHTML = `Shares this hash byte with `
      + `${e.collides_with.map((n) => `<b>${esc(n)}</b>`).join(", ")} — `
      + `a different channel on the same 1-byte hash.`;
  } else {
    col.style.display = "none";
  }

  // Stats row. For a named channel the figures are its own decoded traffic; for
  // an unknown byte, the still-encrypted packets and a distinct-message estimate.
  $("d-stats").innerHTML = (encrypted ? [
    stat(fmtInt(e.undecoded), "packets (encrypted)"),
    stat("~" + fmtInt(e.undecoded_distinct), "unknown messages"),
    stat(e.last_activity ? fmtAgo(e.last_activity) : "—", "last activity"),
  ] : [
    stat(fmtInt(e.decoded_packets), "packets (decoded)"),
    stat(fmtInt(e.messages), "messages"),
    stat(e.unique_senders ? fmtInt(e.unique_senders) : "—", "unique senders"),
    stat(e.last_activity ? fmtAgo(e.last_activity) : "—", "last activity"),
  ]).filter(Boolean).join("");

  // Crack controls for encrypted channels.
  const panel = $("d-crack-panel");
  if (encrypted) {
    panel.style.display = "";
    $("d-crack-sub").textContent = e.kind === "exhausted"
      ? "Already swept at the current parameters without a hit. Retry clears the exhausted mark and tries again."
      : "Recover the name to decode this channel's traffic on the hash byte. Dictionary/catalog/rules first, then brute-force.";
    const attempt = $("d-attempt");
    if (e.kind === "exhausted" && e.attempt) {
      attempt.style.display = "";
      const a = e.attempt;
      attempt.innerHTML = `Last attempt: charset <code>${esc(a.charset || "?")}</code>, max length `
        + `<b>${esc(a.max_length)}</b>, ${fmtInt(a.packets_seen)} packets seen`
        + (a.attempted_at ? ` · ${fmtTime(a.attempted_at)}` : "");
    } else {
      attempt.style.display = "none";
    }
    $("d-crack-go").disabled = !!e.cracking;
    $("d-retry-go").style.display = e.kind === "exhausted" ? "" : "none";
    $("d-retry-go").disabled = !!e.cracking;
  } else {
    panel.style.display = "none";
  }
}

function stat(v, k) {
  return `<div class="stat"><span class="v">${esc(v)}</span><span class="k">${esc(k)}</span></div>`;
}

/* ---------------- Channel message view (virtualized) ---------------- */

const msgView = (function () {
  const ROW_EST = 34;
  const WINDOW = 60;
  const PAGE = 80;
  let channel = null;
  let search = "";
  let rows = [];
  let total = 0;
  let oldestId = null;
  let newestId = null;
  let hasMore = true;
  let loading = false;
  let avgRow = ROW_EST;
  let winStart = 0;
  let scrollEl, listEl;

  function mount() {
    scrollEl = $("msg-scroll");
    listEl = $("msg-list");
    scrollEl.onscroll = onScroll;
  }

  async function open(name) {
    channel = name;
    search = $("msg-search").value.trim();
    rows = []; total = 0; oldestId = null; newestId = null; hasMore = true; winStart = 0;
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
        rows = rows.concat(data.messages);
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

  async function loadNewer() {
    if (loading || !channel || newestId == null) return;
    try {
      const data = await getJSON(url({ limit: 200, after_id: newestId }));
      if (data.messages && data.messages.length) {
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
    const el = document.querySelector("#d-msg-wrap .msg-count");
    const txt = `${fmtInt(total)} message${total === 1 ? "" : "s"}${s}`;
    if (el) el.textContent = txt;
  }

  function onScroll() {
    const st = scrollEl.scrollTop;
    const newWin = Math.max(0, Math.floor(st / avgRow) - 8);
    if (Math.abs(newWin - winStart) >= 8) { winStart = newWin; render(); }
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
    measure();
  }

  function measure() {
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

/* ---------------- Config view ---------------- */

function renderConfig() {
  const cfg = state.config;
  if (!cfg) return;
  const s = cfg.settings;

  $("cfg-engine").value = s.engine;
  $("cfg-engine-gpu").textContent = cfg.gpu.available
    ? "GPU available" + (cfg.gpu.name ? ` (${cfg.gpu.name})` : "")
    : (cfg.gpu.forced_cpu ? "GPU disabled (--cpu)" : "No GPU detected");
  $("cfg-engine-gpu").className = "field-hint " + (cfg.gpu.available ? "" : "warn-note");
  $("cfg-engine-opt-gpu").disabled = !cfg.gpu.available;

  $("cfg-charset").value = s.charset;
  $("cfg-maxlen").value = s.max_length;
  updateSearchEstimate();

  const wl = cfg.wordlist;
  $("wl-total").textContent = fmtInt(wl.total);
  $("wl-builtin").textContent = fmtInt(wl.builtin);
  $("wl-catalog").textContent = fmtInt(wl.catalog);
  $("wl-rules").textContent = fmtInt(wl.rules_count || 0);
  $("cfg-catalog").checked = wl.catalog_enabled;
  $("cfg-rules").checked = !!s.use_rules;
  $("cfg-rules-hint").textContent = wl.rules_enabled
    ? (wl.rules_built ? `(${fmtInt(wl.rules_count)} mangles built)` : "(built on demand)")
    : "";
  const files = wl.custom_files || [];
  $("wl-custom").innerHTML = files.length
    ? files.map((f) => `<div class="faint mono" style="font-size:12px">+ ${esc(f)}</div>`).join("")
    : '<span class="faint">none</span>';

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

async function saveEngine() { await applyConfig({ engine: $("cfg-engine").value }); }
async function saveBrute() {
  await applyConfig({ charset: $("cfg-charset").value, max_length: parseInt($("cfg-maxlen").value, 10) });
  toast("Brute-force defaults saved");
}
async function toggleCatalog() { await applyConfig({ use_catalog: $("cfg-catalog").checked }); }
async function toggleRules() { await applyConfig({ use_rules: $("cfg-rules").checked }); }
async function toggleAuto() { await applyConfig({ auto_crack: $("cfg-auto").checked }); pollStatus(); }
async function toggleAutoDict() { await applyConfig({ auto_dict_only: $("cfg-auto-dict").checked }); }

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
  else if (e.key === "g") showChannels();
  else if (e.key === "c") showConfig();
  else if (e.key === "s") { if (state.view !== "config") startSweep(); }
  else if (e.key === "t") toggleTheme();
  else if (e.key === "Escape" && state.view === "channel") showChannels();
}

/* ---------------- Init ---------------- */

async function init() {
  initTheme();
  msgView.mount();

  // Message toolbar count element (kept out of the virtualized list).
  const tb = document.querySelector("#d-msg-wrap .msg-toolbar");
  if (tb && !tb.querySelector(".msg-count")) {
    const span = document.createElement("span");
    span.className = "msg-count msg-stats";
    tb.appendChild(span);
  }

  $("theme-btn").onclick = toggleTheme;
  $("nav-channels").onclick = showChannels;
  $("nav-config").onclick = showConfig;
  $("nav-health").onclick = showHealth;
  $("back-btn").onclick = showChannels;
  $("sweep-go").onclick = startSweep;
  $("m-go").onclick = startCrackHash;
  $("p-go").onclick = startCrackPacket;
  $("sort-by").onchange = () => { state.sort = $("sort-by").value; renderChannels(); };
  $("msg-search-btn").onclick = () => msgView.doSearch();
  $("d-crack-go").onclick = () => { if (state.active) startCrack(state.active.hash); };
  $("d-retry-go").onclick = () => { if (state.active) retryCrack(state.active.hash); };
  $("cfg-engine").onchange = saveEngine;
  $("cfg-charset").oninput = updateSearchEstimate;
  $("cfg-maxlen").oninput = updateSearchEstimate;
  $("cfg-charset").onchange = saveBrute;
  $("cfg-maxlen").onchange = saveBrute;
  $("cfg-save-brute").onclick = saveBrute;
  $("cfg-catalog").onchange = toggleCatalog;
  $("cfg-rules").onchange = toggleRules;
  $("cfg-auto").onchange = toggleAuto;
  $("cfg-auto-dict").onchange = toggleAutoDict;
  $("q-cancel-all").onclick = cancelAllJobs;
  $("wl-add").onclick = addWordlist;
  document.addEventListener("keydown", onKey);

  await refreshConfig();
  initMultiword();
  await refreshData();
  await refreshHealth();
  setView("channels");
  renderChannels();
  await pollStatus();

  setInterval(() => {
    refreshHealth();   // keep the freshness badge live even mid-crack
    if (state.cracking) return;   // poll loop handles refresh while busy
    refreshData().then(() => {
      if (state.view === "channels") renderChannels();
      if (state.view === "channel") msgView.loadNewer();
    });
  }, 5000);
}

document.addEventListener("DOMContentLoaded", init);
