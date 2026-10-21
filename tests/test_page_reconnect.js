// The page while the winSync server is down: the status chip must say so steadily, not flicker
// between "winSync server: reconnecting…" and the last radio state. Runs the real app.js with a stub
// DOM, stub fetch and a WebSocket that connects once, then the "server" goes away.
// Run from the winSync folder: node tests/test_page_reconnect.js
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
const els = {};
const document = {
  activeElement: null,
  getElementById: (id) => (els[id] = els[id] || el(id)),
  createElement: () => el("x"),
};

let serverUp = true;
const sockets = [];
class FakeWebSocket {
  constructor() {
    sockets.push(this);
    setTimeout(() => {
      if (!serverUp) { this.onclose && this.onclose(); return; }
      this.onopen && this.onopen();
      this.onmessage && this.onmessage({ data: JSON.stringify({
        type: "state", cat_connected: true, source: "omnirig", control: "radio", tx: false, rx: 0,
        follow: "auto", freq_hz: 7150000, mode_label: "LSB", band: "40m" }) });
    }, 10);
  }
  close() { this.onclose && this.onclose(); }
}
const responses = { "/_winsync/api/config": { target: null, sync: { enabled: true }, source: "omnirig" },
                    "/_winsync/api/urls": { urls: [] } };
const ctx = {
  console, document, setTimeout, setInterval, clearTimeout, JSON, Date, Math, String, Promise,
  location: { protocol: "http:", host: "127.0.0.1:8090" },
  localStorage: { getItem: () => null, setItem() {} },
  fetch: async (u) => ({ ok: true, json: async () => responses[u] || {} }),
  WebSocket: FakeWebSocket,
};
ctx.window = ctx;
vm.createContext(ctx);

(async () => {
  for (const f of ["kiwiwatch.js", "app.js"]) {
    vm.runInContext(fs.readFileSync(path.join(ROOT, "static", f), "utf8"), ctx);
  }
  await sleep(300);
  assert.strictEqual(els["cat-chip"].textContent, "OmniRig: connected");

  // The server stops: sample the chip every 20 ms across two reconnect attempts (2 s apart).
  serverUp = false;
  sockets[sockets.length - 1].close();
  const seen = new Set();
  for (let t = 0; t < 4500; t += 20) { seen.add(els["cat-chip"].textContent); await sleep(20); }
  assert.deepStrictEqual([...seen], ["winSync server: reconnecting…"],
                         "chip flickered while the server was down: " + [...seen].join(" | "));
  assert.ok(sockets.length >= 3, "kept retrying");

  // The server comes back: the radio state shows again.
  serverUp = true;
  await sleep(2300);
  assert.strictEqual(els["cat-chip"].textContent, "OmniRig: connected");
  console.log("page reconnect tests passed");
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
