// Run: node tests/test_bridge.js
// Exercises static/bridge.js against a stubbed Kiwi page (no browser needed).
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function makeKiwi(muted) {
  const calls = [];
  const ctx = {
    console,
    kiwi: { muted: muted ? 1 : 0, freq_offset_kHz: 0 },
    toggle_or_set_mute(v) { calls.push(["mute", v]); ctx.kiwi.muted = v; },
    freqmode_set_dsp_kHz(k, m, o) { calls.push(["tune", k, m]); },
    demodulators: [{}], bandwidth: 30e6,
    zoom_step() { calls.push(["zoom"]); }, ext_zoom: { TO_BAND: 1 },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "static", "bridge.js"), "utf8"), ctx);
  return { ps: ctx.winSync, ctx, calls };
}

// TX mutes an unmuted Kiwi, RX restores it; repeated calls are idempotent.
{
  const { ps, ctx, calls } = makeKiwi(false);
  ps.txMute(true); ps.txMute(true);
  assert.strictEqual(ctx.kiwi.muted, 1);
  ps.txMute(false); ps.txMute(false);
  assert.strictEqual(ctx.kiwi.muted, 0);
  assert.deepStrictEqual(calls, [["mute", 1], ["mute", 0]]);
}

// A mute the user set themselves is never released by winSync.
{
  const { ps, ctx, calls } = makeKiwi(true);
  ps.txMute(true); ps.txMute(false);
  assert.strictEqual(ctx.kiwi.muted, 1);
  assert.deepStrictEqual(calls, []);
}

// If the user unmutes during TX, RX must not toggle it back on or off again.
{
  const { ps, ctx, calls } = makeKiwi(false);
  ps.txMute(true);
  ctx.kiwi.muted = 0;               // user un-mutes manually mid-TX
  ps.txMute(false);
  assert.strictEqual(ctx.kiwi.muted, 0);
  assert.deepStrictEqual(calls, [["mute", 1]]);
}

// Tuning: valid, invalid mode, out of range, and offset handling.
{
  const { ps, calls } = makeKiwi(false);
  assert.strictEqual(ps.tune(14200000, "usb").ok, true);
  assert.strictEqual(calls[0].join(","), "tune,14200,usb");
  assert.strictEqual(ps.tune(14200000, "bogus").ok, false);
  assert.strictEqual(ps.tune(50313000, "usb").ok, false);   // beyond 30 MHz Kiwi
  assert.strictEqual(calls.length, 1);
}

// ---- PA3FWM WebSDR page (stubbed from websdr-base.js) ----------------------------------------
function makeWebSdr() {
  const calls = [];
  const bands = [[1900, 384], [3650, 768], [5350, 384], [7050, 384], [14200, 768], [18100, 384]]
    .map(([c, sr]) => ({ centerfreq: c, samplerate: sr }));
  const ctx = {
    console, calls,
    bi: bands, nvbands: bands.length, band: 4, mode: "USB", lo: 0.3, hi: 2.7, freq: 14200,
    allloadeddone: true, waitingforwaterfalls: 0, soundapplet: {},
    document: { getElementById: (id) => (id === "mutecheckbox" ? ctx.cb : null) },
    cb: { checked: false },
    iscw() { return ctx.hi - ctx.lo < 1.0; },
    nominalfreq() { return ctx.iscw() ? ctx.freq + (ctx.hi + ctx.lo) / 2 : ctx.freq; },
    setfreq(f) { calls.push(["setfreq", f]); ctx.freq = f; },
    setfreqb(f) {
      calls.push(["setfreqb", f]);
      for (let i = 0; i < ctx.nvbands; i++) {
        const e = ctx.bi[i], w = e.samplerate / 2 + 4;
        if (f > e.centerfreq - w && f < e.centerfreq + w) { ctx.band = i; ctx.freq = f; return; }
      }
    },
    set_mode(m) {
      calls.push(["set_mode", m]);
      const t = { USB: [0.3, 2.7], LSB: [-2.7, -0.3], AM: [-4, 4], CW: [-0.95, -0.55], FM: [-6, 6] }[m];
      ctx.mode = m; ctx.lo = t[0]; ctx.hi = t[1];
    },
    wfset(c) { calls.push(["wfset", c]); },
    setmute(v) { calls.push(["mute", v]); },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "static", "bridge.js"), "utf8"), ctx);
  return { ps: ctx.winSync, ctx, calls };
}

// Detected as a WebSDR; ready only after sound and waterfalls have started.
{
  const { ps, ctx } = makeWebSdr();
  assert.strictEqual(ps.kind(), "WebSDR");
  assert.strictEqual(ps.ready(), true);
  ctx.waitingforwaterfalls = 1;
  assert.strictEqual(ps.ready(), false);
  ctx.waitingforwaterfalls = 0; ctx.allloadeddone = false;
  assert.strictEqual(ps.ready(), false);
  assert.strictEqual(makeKiwi(false).ps.kind(), "Kiwi");
}

// SSB tune: band switch first, then the mode, so WebSDR's own LSB/USB swap can't override it.
{
  const { ps, ctx, calls } = makeWebSdr();
  const r = ps.tune(7100000, "lsb", { zoomToBand: true });
  assert.strictEqual(r.ok, true);
  assert.strictEqual(ctx.band, 3);
  assert.strictEqual(ctx.mode, "LSB");
  assert.strictEqual(ctx.freq, 7100);
  assert.deepStrictEqual(calls.map((c) => c[0]), ["setfreqb", "set_mode", "setfreq", "wfset", "mute"]);
  assert.deepStrictEqual({ ...ps.readback() }, { freq_hz: 7100000, mode: "LSB" });
  // same band again: no re-zoom
  calls.length = 0;
  ps.tune(7110000, "lsb", { zoomToBand: true });
  assert.ok(!calls.some((c) => c[0] === "wfset"));
}

// CW: the carrier sits off the dial by the passband centre, and readback gives the dial back.
{
  const { ps, ctx } = makeWebSdr();
  assert.strictEqual(ps.tune(14030000, "cw", {}).ok, true);
  assert.strictEqual(ctx.mode, "CW");
  assert.ok(Math.abs(ctx.freq - (14030 + 0.75)) < 1e-9);       // carrier = dial - (hi+lo)/2
  assert.deepStrictEqual({ ...ps.readback() }, { freq_hz: 14030000, mode: "CW" });
}

// Kiwi mode names map onto WebSDR's five modes.
{
  const { ps, ctx } = makeWebSdr();
  for (const [k, w] of [["am", "AM"], ["sam", "AM"], ["nbfm", "FM"], ["usb", "USB"], ["cwn", "CW"]]) {
    assert.strictEqual(ps.tune(14200000, k, {}).ok, true);
    assert.strictEqual(ctx.mode, w);
  }
  assert.strictEqual(ps.tune(14200000, "iq", {}).ok, false);
}

// Out of coverage (WARC 30m, 6m) is reported, and nothing is changed.
{
  const { ps, calls } = makeWebSdr();
  const r = ps.tune(10120000, "usb", {});
  assert.strictEqual(r.ok, false);
  assert.match(r.err, /outside/);
  assert.strictEqual(calls.length, 0);
}

// Mute on TX behaves like the Kiwi: only releases a mute winSync applied.
{
  const { ps, ctx, calls } = makeWebSdr();
  ps.txMute(true); ps.txMute(true);
  assert.strictEqual(ctx.cb.checked, true);
  ps.txMute(false);
  assert.strictEqual(ctx.cb.checked, false);
  assert.deepStrictEqual(calls, [["mute", 1], ["mute", 0]]);
  calls.length = 0; ctx.cb.checked = true;            // user's own mute
  ps.txMute(true); ps.txMute(false);
  assert.strictEqual(ctx.cb.checked, true);
  assert.deepStrictEqual(calls, [["mute", 1], ["mute", 1]]);   // box untouched; applet re-synced to it
}

// The applet's mute is re-asserted from the checkbox after TX, even if the applet lost it.
{
  const { ps, ctx, calls } = makeWebSdr();
  ctx.cb.checked = true;                               // user's own mute
  ps.txMute(true);
  calls.length = 0;                                    // applet silently loses its mute during TX
  ps.txMute(false);
  assert.deepStrictEqual(calls, [["mute", 1]]);
  assert.strictEqual(ctx.cb.checked, true);
  calls.length = 0;
  ps.txMute(false);                                    // steady RX: no repeated calls
  assert.deepStrictEqual(calls, []);
}

// Button-style mute page (setmute() leaves #mutecheckbox alone; state is the global Mute + #Mute button).
{
  const { ps, ctx, calls } = makeWebSdr();
  const btn = { style: { color: "" } };
  ctx.document.getElementById = (id) => (id === "Mute" ? btn : id === "mutecheckbox" ? ctx.cb : null);
  ctx.Mute = 0;
  ctx.MakeCheckbox = () => { ctx.Mute = ctx.Mute ? 0 : 1; ctx.setmute(ctx.Mute); };
  // winSync mutes and restores an unmuted page
  ps.txMute(true); assert.strictEqual(ctx.Mute, 1);
  ps.txMute(false); assert.strictEqual(ctx.Mute, 0);
  // the user's own mute (invisible to #mutecheckbox) survives TX -> RX
  calls.length = 0;
  ctx.MakeCheckbox();
  ps.txMute(true); ps.txMute(false);
  assert.strictEqual(ctx.Mute, 1);
  const sent = calls.filter((c) => c[0] === "mute").map((c) => c[1]);
  assert.ok(sent.length >= 1 && sent.every((v) => v === 1));   // never unmuted
}

// Goonhilly QO-100 narrowband WebSDR (its tmp/bandinfo.js, 2026-10-28): one band, centre
// 10,489,750 kHz, 512 kHz wide. SDR Console's 10 GHz receive frequency tunes it directly, in kHz.
{
  const { ps, ctx, calls } = makeWebSdr();
  ctx.bi = [{ centerfreq: 10489750, samplerate: 512 }]; ctx.nvbands = 1; ctx.band = 0;
  assert.strictEqual(ps.kind(), "WebSDR");
  const r = ps.tune(10489730000, "usb", { zoomToBand: false });   // band "GEN": the page never zooms
  assert.strictEqual(r.ok, true);
  assert.deepStrictEqual(calls.slice(0, 3), [["setfreqb", 10489730], ["set_mode", "USB"], ["setfreq", 10489730]]);
  assert.ok(!calls.some((c) => c[0] === "wfset"));
  assert.deepStrictEqual({ ...ps.readback() }, { freq_hz: 10489730000, mode: "USB" });
  for (const [k, w] of [["lsb", "LSB"], ["cw", "CW"], ["nbfm", "FM"], ["am", "AM"], ["sam", "AM"]]) {
    assert.strictEqual(ps.tune(10489600000, k, {}).ok, true);       // SDR Console's modes -> WebSDR's five
    assert.strictEqual(ctx.mode, w);
  }
  calls.length = 0;
  const out = ps.tune(10490300000, "usb", {});                       // above the 512 kHz band
  assert.strictEqual(out.ok, false);
  assert.match(out.err, /10490\.300000 MHz is outside/);
  assert.strictEqual(calls.length, 0);
  ps.txMute(true);
  assert.strictEqual(ctx.cb.checked, true);                          // Mute on TX on its #mutecheckbox
  ps.txMute(false);
  assert.strictEqual(ctx.cb.checked, false);
}

// Sockets hard-coded as "wss://" + location.host (Goonhilly's audio and waterfall) go to winSync
// as ws:// while the page is on http. Nothing else is changed.
function loadWithSockets(protocol) {
  const made = [];
  class NativeWS {
    constructor(url, protocols) { made.push(protocols === undefined ? [url] : [url, protocols]); }
  }
  NativeWS.CONNECTING = 0; NativeWS.OPEN = 1; NativeWS.CLOSING = 2; NativeWS.CLOSED = 3;
  const ctx = { console, WebSocket: NativeWS, location: { protocol, host: "127.0.0.1:8090" } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "static", "bridge.js"), "utf8"), ctx);
  return { ctx, made, NativeWS };
}
{
  const { ctx, made, NativeWS } = loadWithSockets("http:");
  const run = (js) => vm.runInContext(js, ctx);
  const ws = run('new WebSocket("wss://" + window.location.host + "/~~stream?v=11")');
  run('new WebSocket("wss://" + window.location.host + "/~~waterstream0?format=9&width=1024")');
  run('new WebSocket("wss://127.0.0.1:8090")');
  run('new WebSocket("ws://127.0.0.1:8090/ws/kiwi/1/SND")');            // a Kiwi: already ws
  run('new WebSocket("wss://other.example.org/x")');                    // another host: left alone
  run('new WebSocket("wss://127.0.0.1:80901/x")');                      // not the same port
  run('new WebSocket("wss://127.0.0.1:8090/p", ["chat"])');
  assert.deepStrictEqual(made.map((m) => m[0]), [
    "ws://127.0.0.1:8090/~~stream?v=11", "ws://127.0.0.1:8090/~~waterstream0?format=9&width=1024",
    "ws://127.0.0.1:8090", "ws://127.0.0.1:8090/ws/kiwi/1/SND", "wss://other.example.org/x",
    "wss://127.0.0.1:80901/x", "ws://127.0.0.1:8090/p"]);
  assert.strictEqual(JSON.stringify(made[6][1]), '["chat"]');       // protocols passed on
  assert.ok(ws instanceof NativeWS);
  assert.strictEqual(run("!!WebSocket.CLOSING"), true);                 // websdr-base.js checks this
  assert.strictEqual(run("WebSocket.OPEN"), 1);
}
{
  const { ctx, made, NativeWS } = loadWithSockets("https:");             // page on https: untouched
  assert.strictEqual(ctx.WebSocket, NativeWS);
  vm.runInContext('new WebSocket("wss://127.0.0.1:8090/~~stream")', ctx);
  assert.deepStrictEqual(made, [["wss://127.0.0.1:8090/~~stream"]]);
}

console.log("bridge tests passed");
