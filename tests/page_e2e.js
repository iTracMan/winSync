// End-to-end: real static/app.js + kiwiwatch.js (stubbed DOM, stubbed Kiwi bridge) against a real
// winSync server talking to the fake Thetis (tests/fake_thetis.py). Never touches the real radio.
// Run from the winSync folder: node tests/page_e2e.js   (uses ports 13114 and 8091)
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const ROOT = path.join(__dirname, "..");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "winsync-e2e-"));
const PY = path.join(ROOT, ".venv", "Scripts", "python.exe");
const CAT_PORT = 13114, WEB_PORT = 8091;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

fs.writeFileSync(path.join(TMP, "e2e.toml"), `
[server]
host = "127.0.0.1"
port = ${WEB_PORT}
urls_file = "${path.join(TMP, "urls.json").replace(/\\/g, "/")}"
[cat]
host = "127.0.0.1"
port = ${CAT_PORT}
[kiwi]
url = "http://127.0.0.1:9/"
[sync]
control = "both"
`);

const fake = spawn(PY, ["-m", "tests.fake_thetis", String(CAT_PORT)], { cwd: ROOT });
let serverLog = "";
const server = spawn(PY, ["-m", "winSync", "--config", path.join(TMP, "e2e.toml")], { cwd: ROOT });
server.stdout.on("data", (d) => (serverLog += d));
server.stderr.on("data", (d) => (serverLog += d));
const thetis = (line) => fake.stdin.write(line + "\n");
const radioSets = () => (serverLog.match(/Kiwi -> radio: VFO/g) || []).length;

// ---- stub DOM -------------------------------------------------------------------------------
function el(id) {
  const e = {
    id, textContent: "", className: "", hidden: false, value: "", checked: false, attrs: {},
    listeners: {}, classList: { toggle() {} }, dataset: {}, style: {},
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k] || null; },
    querySelectorAll() { return []; }, appendChild() {}, append() {}, blur() {}, focus() {},
    scrollIntoView() {},
  };
  return e;
}
const els = {};
const kiwi = { tunes: [], cur: { freq_hz: 10000000, mode: "am" }, muted: false };
const bridge = {
  kind: () => "Kiwi", ready: () => true, readback: () => ({ ...kiwi.cur }),
  tune(f, m) { kiwi.tunes.push([f, m]); kiwi.cur = { freq_hz: f, mode: m }; return { ok: true }; },
  txMute(on) { kiwi.muted = on; return true; },
};
const frame = el("kiwi");
frame.contentWindow = { winSync: bridge };
Object.defineProperty(frame, "src", {
  set(v) { this.attrs.src = v; setTimeout(() => (this.listeners.load || []).forEach((f) => f()), 50); },
  get() { return this.attrs.src; },
});
els.kiwi = frame;
const document = {
  activeElement: null,
  getElementById: (id) => (els[id] = els[id] || el(id)),
  createElement: () => el("x"),
};
const store = {};
const base = `http://127.0.0.1:${WEB_PORT}`;
const ctx = {
  console, document, setTimeout, setInterval, clearTimeout, JSON, Date, Math, String, Promise,
  location: { protocol: "http:", host: `127.0.0.1:${WEB_PORT}` },
  localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => (store[k] = v) },
  fetch: (u, o) => fetch(u.startsWith("/") ? base + u : u, o),
  WebSocket,
};
ctx.window = ctx;
vm.createContext(ctx);

const msg = () => els.msg.textContent;
async function until(pred, what, ms = 5000) {
  for (let t = 0; t < ms; t += 50) { if (pred()) return; await sleep(50); }
  throw new Error("timeout: " + what + " | msg=" + (els.msg && msg()) + "\n" + serverLog.slice(-1500));
}
async function userTunesKiwi(f, m) { kiwi.cur = { freq_hz: f, mode: m }; await sleep(700); }
async function setControl(v) {
  els.control.value = v;
  for (const f of els.control.listeners.change) await f({ target: els.control });
  await until(() => serverLog.includes("control: "), "control change");
}

(async () => {
  try {
    for (let i = 0; i < 100; i++) {           // wait for the server
      try { await fetch(base + "/_winsync/api/config"); break; } catch (e) { await sleep(100); }
    }
    for (const f of ["kiwiwatch.js", "app.js"]) {
      vm.runInContext(fs.readFileSync(path.join(ROOT, "static", f), "utf8"), ctx);
    }
    const html = await (await fetch(base + "/_winsync/")).text();
    assert.ok(html.includes('id="control"') && html.includes("kiwiwatch.js"), "page has Control");

    // 1. Load: radio (fake: VFO A 14.200 USB) is pushed to the Kiwi once
    await until(() => kiwi.tunes.length >= 1, "initial radio -> Kiwi");
    assert.deepStrictEqual(kiwi.tunes[0], [14200000, "usb"]);
    assert.strictEqual(els.control.value, "both");
    await until(() => serverLog.includes("CAT to VFO B is off"), "redirect check");
    await sleep(3200);                            // the Kiwi's load settle time
    console.log("1 ok: initial sync, Control=both, CAT to VFO B off");

    // 2. User tunes the Kiwi in band: the radio follows, and nothing echoes back to the Kiwi
    const tunesBefore = kiwi.tunes.length;
    await userTunesKiwi(14074000, "usn");
    await until(() => radioSets() === 1, "radio set");
    await until(() => msg().startsWith("Radio set to 14.074.000"), "msg radio set");
    await sleep(2000);
    assert.strictEqual(kiwi.tunes.length, tunesBefore, "no echo back to the Kiwi");
    assert.strictEqual(radioSets(), 1, "exactly one set");
    assert.deepStrictEqual(kiwi.cur, { freq_hz: 14074000, mode: "usn" });   // narrow mode kept
    console.log("2 ok: Kiwi -> radio, one set, no echo:", msg());

    // 3. Out of the UK bands: radio not moved, Kiwi stays, page says why
    await userTunesKiwi(14400000, "usb");
    await until(() => msg().includes("outside the UK amateur bands"), "out of band msg");
    assert.strictEqual(msg(), "Radio not moved: 14.400000 MHz is outside the UK amateur bands (Kiwi only)");
    await sleep(1000);
    assert.strictEqual(radioSets(), 1);
    assert.deepStrictEqual(kiwi.cur, { freq_hz: 14400000, mode: "usb" });
    console.log("3 ok: out of band ->", msg());

    // 4. Radio tuned (Both): Kiwi follows, and that tune is not sent back to the radio
    thetis("a 7074000 0");
    await until(() => kiwi.cur.freq_hz === 7074000 && kiwi.cur.mode === "lsb", "radio -> Kiwi");
    await sleep(2000);
    assert.strictEqual(radioSets(), 1, "no echo to radio");
    console.log("4 ok: radio -> Kiwi in Both, no echo");

    // 5. TX: Kiwi change refused
    thetis("tx 1");
    await until(() => serverLog.includes("TX"), "TX seen");
    await sleep(300);
    await userTunesKiwi(7080000, "lsb");
    await until(() => msg().includes("transmitting"), "tx msg");
    assert.strictEqual(radioSets(), 1);
    thetis("tx 0");
    console.log("5 ok: during TX ->", msg());

    // 6. Control = Kiwi controls radio: radio changes no longer move the Kiwi
    await setControl("kiwi");
    await sleep(1500);
    const n6 = kiwi.tunes.length;
    thetis("a 7100000 0");
    await sleep(1500);
    assert.strictEqual(kiwi.tunes.length, n6, "Kiwi not moved by radio in Kiwi mode");
    await userTunesKiwi(7090000, "cw");
    await until(() => radioSets() === 2, "second set");
    await until(() => msg().startsWith("Radio set to 7.090.000"), "msg cw");
    console.log("6 ok: Kiwi controls radio:", msg());

    // 7. Control = radio: Kiwi changes never reach the radio
    serverLog = serverLog.replace(/control: /g, "ctl ");
    await setControl("radio");
    await userTunesKiwi(7095000, "lsb");
    await sleep(1500);
    assert.strictEqual(radioSets(), 2, "nothing sent with Control=radio");
    console.log("7 ok: Radio controls Kiwi: no sets");

    const sets = serverLog.split("\n").filter((l) => l.includes("Kiwi -> radio: VFO"));
    console.log("radio sets made:\n  " + sets.join("\n  "));
    console.log("PAGE E2E PASSED");
    process.exitCode = 0;
  } catch (e) {
    console.error("PAGE E2E FAILED:", e.message);
    process.exitCode = 1;
  } finally {
    server.kill(); fake.kill();
    setTimeout(() => process.exit(), 300);
  }
})();
