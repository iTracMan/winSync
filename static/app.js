"use strict";
// winSync control page: receives radio state from the server and applies it to the Kiwi
// running (same-origin, via the reverse proxy) in the iframe through window.winSync (bridge.js).
// With Control set to "kiwi" or "both", tuning done in the Kiwi is sent to the server, which
// moves the radio if that is allowed (receiving, UK amateur band) or says why it didn't.

const $ = (id) => document.getElementById(id);
const frame = $("kiwi");
const APPLY_INTERVAL_MS = 100;            // max ~10 tune calls/s while the VFO is spun
const SETTLE_MS = 800;                     // after winSync tunes the Kiwi, its readback is not the user's
const LOAD_SETTLE_MS = 3000;               // nor while a freshly loaded Kiwi settles
const RADIO_HOLD_MS = 1000;                // after a Kiwi -> radio change, radio echoes don't retune the Kiwi

let radio = { cat_connected: false, freq_hz: null, kiwi_mode: null, band: null };
let syncOn = true;
let muteOnTx = false;
let zoomOnBand = true;
let lastApplied = null;                    // {freq_hz, kiwi_mode, band} last pushed to Kiwi
let force = false;                         // set to re-push even if unchanged (Resync / Kiwi reload)
let serverDown = false;                    // the page has lost the winSync server
const OMNIRIG_READ_ONLY = "OmniRig is read-only: tuning the Kiwi doesn't move the radio";
let control = "radio";                     // "radio", "kiwi" or "both" (from the server)
let radioHoldUntil = 0;
let tuneInFlight = false;
const watch = KiwiWatch({});

// --diag (testing): what the page saw and did, sent to the server log once a second.
let diagOn = false;
let diagEvents = [];
let diagLastRb = "";
function diag(text) {
  if (diagOn) diagEvents.push(new Date().toISOString().slice(11, 23) + " " + text);
}
function diagReadback(b) {
  if (!diagOn || !b || !b.ready()) return;
  const rb = b.readback();
  const s = rb ? rb.freq_hz + " " + rb.mode : "none";
  if (s !== diagLastRb) { diag("Kiwi readback " + s); diagLastRb = s; }
}
function diagFlush() {
  if (!diagEvents.length) return;
  const events = diagEvents; diagEvents = [];
  fetch("/_winsync/api/diag", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ events }),
  }).catch(() => { /* testing aid only */ });
}

function bridge() {
  try { return frame.contentWindow.winSync || null; } catch (e) { return null; }
}

function setMsg(text, err) {
  $("msg").textContent = text || "";
  $("msg").className = err ? "err" : "";
}

function chip(id, text, cls) {
  const el = $(id);
  el.textContent = text;
  el.className = "chip " + (cls || "");
}

function fmtFreq(hz) {
  if (hz == null) return "—";
  const s = String(hz).padStart(4, "0");
  return s.slice(0, -6) + "." + s.slice(-6, -3) + "." + s.slice(-3);
}

function render() {
  const omni = radio.source === "omnirig";   // read-only OmniRig source: no RX2, no Kiwi -> radio
  if (serverDown) chip("cat-chip", "winSync server: reconnecting…", "bad");   // radio state is stale
  else chip("cat-chip", (omni ? "OmniRig" : "Thetis") + (radio.cat_connected ? ": connected" : ": disconnected"),
            radio.cat_connected ? "ok" : "bad");
  $("freq").textContent = fmtFreq(radio.freq_hz);
  $("mode").textContent = radio.mode_label || "—";
  $("band").textContent = radio.band || "—";
  $("txbadge").hidden = !radio.tx;
  const rx2 = radio.rx === 1;
  $("rxvfo").textContent = omni ? (rx2 ? "VFO B" : "VFO A") : (rx2 ? "RX2 · VFO B" : "RX1 · VFO A");
  $("rxvfo").className = rx2 ? "rx2" : "";
  const sel = $("follow");
  if (radio.follow && document.activeElement !== sel) sel.value = radio.follow;
  const ctl = $("control");
  if (radio.control && document.activeElement !== ctl) ctl.value = radio.control;
  ctl.disabled = omni;
  ctl.title = omni ? "OmniRig is read-only in winSync: the radio controls the Kiwi" : "";
  // OmniRig: always say it is read-only, after any OmniRig problem (not installed, not responding...)
  const note = omni ? [radio.control_note, OMNIRIG_READ_ONLY].filter(Boolean).join(" · ")
                    : radio.control_note || "";
  $("control-note").textContent = note;
  $("control-note").hidden = !note || (radio.control === "radio" && !omni);
  const b = bridge();
  const kind = (b && b.kind && b.kind()) || "Receiver";   // "Kiwi" or "WebSDR", from the bridge
  if (!frame.getAttribute("src")) chip("kiwi-chip", "Receiver: not connected", "");
  else chip("kiwi-chip", kind + (b && b.ready() ? ": ready" : ": loading…"),
            b && b.ready() ? "ok" : "warn");
}

// Mute the Kiwi while the radio transmits (if enabled). Idempotent; bridge.txMute only ever
// unmutes what winSync itself muted. Runs every tick so a Kiwi reload mid-TX is muted again.
function applyMute() {
  const b = bridge();
  if (b && b.txMute) b.txMute(muteOnTx && !!radio.tx);
}

function apply() {
  applyMute();
  watchKiwi();
  if (!syncOn || radio.freq_hz == null) return;
  // Kiwi leads: in "kiwi" mode, and while a Kiwi -> radio change is in flight or just made (the
  // radio's echo of it may arrive before the server's answer, and must not retune the Kiwi)
  if (!force && (control === "kiwi" || tuneInFlight || Date.now() < radioHoldUntil)) return;
  if (!radio.kiwi_mode) {
    if (radio.mode_label) setMsg("Not applied: no receiver mode for " + radio.mode_label, true);
    return;
  }
  const b = bridge();
  if (!b || !b.ready()) { render(); return; }
  const same = lastApplied && lastApplied.freq_hz === radio.freq_hz &&
               lastApplied.kiwi_mode === radio.kiwi_mode;
  if (same && !force) return;
  const bandChanged = !lastApplied || lastApplied.band !== radio.band;
  watch.quiet(Date.now(), SETTLE_MS);      // this tune is ours, not the user's
  diag("winSync tunes Kiwi " + radio.freq_hz + " " + radio.kiwi_mode + (force ? " (forced)" : ""));
  const res = b.tune(radio.freq_hz, radio.kiwi_mode,
                     { zoomToBand: zoomOnBand && bandChanged && radio.band !== "GEN" });
  force = false;
  if (res.ok) {
    lastApplied = { freq_hz: radio.freq_hz, kiwi_mode: radio.kiwi_mode, band: radio.band };
    setMsg("Applied " + fmtFreq(radio.freq_hz) + " " + radio.kiwi_mode);
  } else {
    // Remember it so we don't retry every 100 ms; a new radio change will try again.
    lastApplied = { freq_hz: radio.freq_hz, kiwi_mode: radio.kiwi_mode, band: lastApplied ? lastApplied.band : null };
    setMsg("Not applied: " + res.err, true);
  }
}

// Kiwi -> radio: report tuning the user did in the Kiwi.
function watchKiwi() {
  if (!syncOn || control === "radio" || tuneInFlight) return;
  const b = bridge();
  if (!b || !b.ready()) return;
  diagReadback(b);
  const change = watch.poll(Date.now(), b.readback());
  if (change) { diag("change seen as the user's: " + change.freq_hz + " " + change.kiwi_mode); sendTune(change); }
}

async function sendTune(change) {
  tuneInFlight = true;
  try {
    const r = await fetch("/_winsync/api/tune", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(change),
    });
    const j = await r.json();
    if (!r.ok) { setMsg(j.error || "Could not tune the radio", true); return; }
    diag("server: " + (j.moved ? "moved radio to " + j.freq_hz : j.message));
    if (j.moved) {
      // The radio will now report this: don't push it back to the Kiwi (it may be in a narrow
      // variant of the mode, e.g. usn, that the radio reports as plain usb).
      lastApplied = { freq_hz: j.freq_hz, kiwi_mode: j.kiwi_mode, band: j.band };
      radioHoldUntil = Date.now() + RADIO_HOLD_MS;
      setMsg("Radio set to " + fmtFreq(j.freq_hz) + (j.kiwi_mode ? " " + j.kiwi_mode : ""));
    } else {
      setMsg(j.message + (j.out_of_band ? " (Kiwi only)" : ""), true);
    }
  } catch (e) {
    setMsg("Could not reach the winSync server to tune the radio", true);
  } finally {
    tuneInFlight = false;
  }
}

function connectWs() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/_winsync/ws");
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "state") {
      // Choosing Control must not itself move the radio to wherever the Kiwi was left: the Kiwi's
      // current position becomes the starting point and only tuning after this counts.
      if ((m.control || "radio") !== control) {
        watch.quiet(Date.now(), 0);
        diag("Control " + m.control);
        setMsg("");                         // an older "Radio set to ..." no longer applies
      }
      radio = m; control = m.control || "radio"; render();
    }
  };
  ws.onopen = () => { serverDown = false; };
  ws.onclose = () => {
    radio.cat_connected = false;
    serverDown = true;                     // render() keeps saying so until the server is back
    render();
    setTimeout(connectWs, 2000);
  };
}

// ---- Remembered receiver URLs (editable drop-down) ----
// Kept in this browser's localStorage, most recent first. A URL is added only once the server has
// accepted it, so typos that fail validation never end up in the list.
const URL_KEY = "winSync.urls";
const MAX_URLS = 30;
const urlInput = $("kiwi-url");
const urlList = $("url-list");
let activeIdx = -1;

// The server keeps the master copy (so the list survives restarts and works from any browser/origin);
// localStorage is only a cache and the source for migrating an older list.
let urls = [];
function readLocalUrls() {
  try {
    const a = JSON.parse(localStorage.getItem(URL_KEY));
    if (Array.isArray(a)) return a.filter((u) => typeof u === "string" && u);
  } catch (e) { /* storage blocked or corrupt */ }
  return [];
}
function loadUrls() { return urls; }
function saveUrls(list) {
  urls = list.slice(0, MAX_URLS);
  try { localStorage.setItem(URL_KEY, JSON.stringify(urls)); } catch (e) { /* ignore */ }
  fetch("/_winsync/api/urls", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ urls }),
  }).catch(() => { /* server unreachable; localStorage still has it */ });
}
async function loadUrlsFromServer() {
  const local = readLocalUrls();
  try {
    const j = await (await fetch("/_winsync/api/urls")).json();
    const server = Array.isArray(j.urls) ? j.urls : [];
    if (!server.length && local.length) saveUrls(local);   // migrate an older browser-only list
    else urls = server.slice(0, MAX_URLS);
  } catch (e) {
    urls = local;
  }
}
function rememberUrl(url) {
  saveUrls([url].concat(urls.filter((u) => u !== url)));
}

function listOpen() { return !urlList.hidden; }
function closeList() {
  urlList.hidden = true; activeIdx = -1;
  urlInput.setAttribute("aria-expanded", "false");
}
function setActive(i) {
  const items = urlList.querySelectorAll("li[data-url]");
  activeIdx = items.length ? (i + items.length) % items.length : -1;
  items.forEach((li, n) => li.classList.toggle("active", n === activeIdx));
  if (items[activeIdx]) items[activeIdx].scrollIntoView({ block: "nearest" });
}
function chooseUrl(url) {
  urlInput.value = url;
  closeList();
  connectKiwi(url);
}
function openList() {
  const urls = loadUrls();
  urlList.textContent = "";
  if (!urls.length) {
    const li = document.createElement("li");
    li.className = "empty"; li.textContent = "No receivers used yet";
    urlList.appendChild(li);
  }
  urls.forEach((u) => {
    const li = document.createElement("li");
    li.dataset.url = u; li.setAttribute("role", "option");
    const t = document.createElement("span");
    t.className = "url"; t.textContent = u; t.title = u;
    t.addEventListener("mousedown", (e) => { e.preventDefault(); chooseUrl(u); });
    const d = document.createElement("button");
    d.type = "button"; d.className = "del"; d.textContent = "×";
    d.title = "Remove from list"; d.setAttribute("aria-label", "Remove " + u + " from list");
    d.addEventListener("mousedown", (e) => {
      e.preventDefault();
      saveUrls(loadUrls().filter((x) => x !== u));
      openList();
    });
    li.append(t, d);
    urlList.appendChild(li);
  });
  urlList.hidden = false;
  urlInput.setAttribute("aria-expanded", "true");
  activeIdx = -1;
}

$("url-toggle").addEventListener("mousedown", (e) => {
  e.preventDefault();                       // keep focus in the text field
  if (listOpen()) closeList(); else { urlInput.focus(); openList(); }
});
urlInput.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (!listOpen()) openList();
    setActive(activeIdx + (e.key === "ArrowDown" ? 1 : -1));
  } else if (e.key === "Enter" && listOpen() && activeIdx >= 0) {
    e.preventDefault();
    chooseUrl(urlList.querySelectorAll("li[data-url]")[activeIdx].dataset.url);
  } else if (e.key === "Escape") {
    closeList();
  }
});
urlInput.addEventListener("blur", closeList);

async function connectKiwi(url) {
  setMsg("");
  const r = await fetch("/_winsync/api/target", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  const j = await r.json();
  if (!r.ok) { setMsg(j.error || "Bad Kiwi URL", true); return; }
  try { localStorage.setItem("winSync.kiwi", j.target); } catch (e) { /* storage may be blocked */ }
  rememberUrl(j.target);
  $("kiwi-url").value = j.target;
  lastApplied = null;
  force = true;
  // No query string: Kiwi appends the page's query to its WebSocket URLs. Assigning src again
  // re-navigates, giving a fresh Kiwi session; bridge.js is injected by the proxy.
  frame.src = "/";
}

$("kiwi-form").addEventListener("submit", (e) => {
  e.preventDefault();
  closeList();
  const url = $("kiwi-url").value.trim();
  if (url) connectKiwi(url);
});
$("sync-on").addEventListener("change", (e) => { syncOn = e.target.checked; force = syncOn; });
$("mute-tx").addEventListener("change", (e) => {
  muteOnTx = e.target.checked;
  try { localStorage.setItem("winSync.muteOnTx", muteOnTx ? "1" : "0"); } catch (err) { /* ignore */ }
  applyMute();
});
$("follow").addEventListener("change", async (e) => {
  const r = await fetch("/_winsync/api/follow", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ follow: e.target.value }),
  });
  if (!r.ok) setMsg((await r.json()).error || "Could not change the VFO followed", true);
  e.target.blur();                          // let render() show the server's value again
});
$("control").addEventListener("change", async (e) => {
  const r = await fetch("/_winsync/api/control", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ control: e.target.value }),
  });
  if (!r.ok) setMsg((await r.json()).error || "Could not change Control", true);
  e.target.blur();
});
$("resync").addEventListener("click", () => { force = true; apply(); });
frame.addEventListener("load", () => {
  diag("Kiwi page loaded");
  lastApplied = null; force = true;
  watch.quiet(Date.now(), LOAD_SETTLE_MS);
});

(async function init() {
  const cfg = await (await fetch("/_winsync/api/config")).json();
  syncOn = cfg.sync.enabled !== false;
  zoomOnBand = cfg.sync.zoom_on_band_change !== false;
  $("sync-on").checked = syncOn;
  muteOnTx = cfg.sync.mute_on_tx === true;
  diagOn = cfg.sync.diag === true;
  if (diagOn) setInterval(diagFlush, 1000);
  if (cfg.sync.dry_run) setMsg("DRY RUN: winSync will not change the radio");
  try {
    const saved = localStorage.getItem("winSync.muteOnTx");
    if (saved !== null) muteOnTx = saved === "1";
  } catch (e) { /* ignore */ }
  $("mute-tx").checked = muteOnTx;
  await loadUrlsFromServer();
  let saved = null;
  try { saved = localStorage.getItem("winSync.kiwi"); } catch (e) { /* ignore */ }
  const initial = cfg.target || saved;
  if (initial) { $("kiwi-url").value = initial; connectKiwi(initial); }
  connectWs();
  setInterval(() => { apply(); render(); }, APPLY_INTERVAL_MS);
})();
