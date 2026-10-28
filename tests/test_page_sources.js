// The page for each radio source: status chip, VFO label, Control locked and the read-only note.
// Runs the real app.js with a stub DOM, stub fetch and a WebSocket that sends one state message.
// Run from the winSync folder: node tests/test_page_sources.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const ROOT = path.join(__dirname, "..");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function el(id) {
  return {
    id, textContent: "", className: "", hidden: false, value: "", checked: false, attrs: {},
    listeners: {}, classList: { toggle() {} }, dataset: {}, style: {}, disabled: false, title: "",
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k] || null; },
    querySelectorAll() { return []; }, appendChild() {}, append() {}, blur() {}, focus() {},
    scrollIntoView() {},
  };
}

// Load app.js in a fresh context, deliver `state` over the WebSocket, return the page's elements.
async function page(state) {
  const els = {};
  const document = {
    activeElement: null,
    getElementById: (id) => (els[id] = els[id] || el(id)),
    createElement: () => el("x"),
  };
  class FakeWebSocket {
    constructor() {
      setTimeout(() => {
        this.onopen && this.onopen();
        this.onmessage && this.onmessage({ data: JSON.stringify(Object.assign({
          type: "state", cat_connected: true, control: "radio", tx: false, rx: 0, follow: "auto",
          freq_hz: 7150000, mode_label: "LSB", band: "40m", control_note: null, radio_name: null,
        }, state)) });
      }, 10);
    }
    close() {}
  }
  const responses = { "/_winsync/api/config": { target: null, sync: { enabled: true }, source: state.source },
                      "/_winsync/api/urls": { urls: [] } };
  const timers = [];
  const ctx = {
    console, document, setTimeout, clearTimeout, JSON, Date, Math, String, Promise,
    setInterval: (f, ms) => { const t = setInterval(f, ms); timers.push(t); return t; },
    location: { protocol: "http:", host: "127.0.0.1:8090" },
    localStorage: { getItem: () => null, setItem() {} },
    fetch: async (u) => ({ ok: true, json: async () => responses[u] || {} }),
    WebSocket: FakeWebSocket,
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  for (const f of ["kiwiwatch.js", "app.js"]) {
    vm.runInContext(fs.readFileSync(path.join(ROOT, "static", f), "utf8"), ctx);
  }
  await sleep(250);
  timers.forEach(clearInterval);
  return els;
}

(async () => {
  // Thetis: as before
  let e = await page({ source: "thetis" });
  assert.strictEqual(e["cat-chip"].textContent, "Thetis: connected");
  assert.strictEqual(e["rxvfo"].textContent, "RX1 · VFO A");
  assert.strictEqual(e["control"].disabled, false);
  assert.strictEqual(e["follow"].disabled, false);
  assert.strictEqual(e["control-note"].hidden, true);

  // OmniRig: as in v1.2
  e = await page({ source: "omnirig" });
  assert.strictEqual(e["cat-chip"].textContent, "OmniRig: connected");
  assert.strictEqual(e["rxvfo"].textContent, "VFO A");
  assert.strictEqual(e["control"].disabled, true);
  assert.strictEqual(e["control-note"].textContent, "OmniRig is read-only: tuning the Kiwi doesn't move the radio");
  assert.strictEqual(e["control-note"].hidden, false);

  // SDR Console: name from NA;, one receiver, read-only note after any problem
  e = await page({ source: "sdrconsole", radio_name: "ADALM-PLUTO", freq_hz: 10489730000,
                   mode_label: "USB", band: "GEN", tx: true });
  assert.strictEqual(e["cat-chip"].textContent, "SDR Console: connected (ADALM-PLUTO)");
  assert.strictEqual(e["rxvfo"].textContent, "RX");
  assert.strictEqual(e["freq"].textContent, "10489.730.000");
  assert.strictEqual(e["band"].textContent, "GEN");
  assert.strictEqual(e["txbadge"].hidden, false);
  assert.strictEqual(e["control"].disabled, true);
  assert.strictEqual(e["follow"].disabled, true);
  assert.strictEqual(e["control-note"].textContent,
                     "SDR Console is read-only: tuning the receiver doesn't move the radio");
  e = await page({ source: "sdrconsole", cat_connected: false, radio_name: "ADALM-PLUTO",
                   control_note: "SDR Console does not answer on COM23" });
  assert.strictEqual(e["cat-chip"].textContent, "SDR Console: disconnected");
  assert.ok(e["control-note"].textContent.startsWith("SDR Console does not answer on COM23 · SDR Console is read-only"));

  console.log("page source tests passed");
  process.exit(0);
})().catch((err) => { console.error(err); process.exit(1); });
