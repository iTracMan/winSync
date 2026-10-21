// Run: node tests/test_kiwiwatch.js
// Exercises static/kiwiwatch.js (Kiwi -> radio change detection) with a fake clock.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function load() {
  const ctx = { window: {} };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "static", "kiwiwatch.js"), "utf8"), ctx);
  return ctx.window.KiwiWatch;
}
const KiwiWatch = load();
const rb = (f, m) => ({ freq_hz: f, mode: m || "usb" });

// The first reading is only a starting point; nothing is reported until the user moves away.
{
  const w = KiwiWatch({});
  assert.strictEqual(w.poll(0, rb(7074000)), null);
  assert.strictEqual(w.poll(100, rb(7074000)), null);
  assert.strictEqual(w.poll(5000, rb(7074000)), null);
}

// A move is reported once it has held still for the debounce time, and only once.
{
  const w = KiwiWatch({ debounceMs: 250 });
  w.poll(0, rb(7074000));
  assert.strictEqual(w.poll(100, rb(7080000)), null);          // just moved
  assert.strictEqual(w.poll(300, rb(7080000)), null);          // 200 ms steady
  assert.deepStrictEqual({ ...w.poll(400, rb(7080000)) }, { freq_hz: 7080000, kiwi_mode: "usb" });
  assert.strictEqual(w.poll(500, rb(7080000)), null);
  assert.strictEqual(w.poll(5000, rb(7080000)), null);
}

// Dragging the waterfall: only where it stops is reported.
{
  const w = KiwiWatch({ debounceMs: 250 });
  w.poll(0, rb(7000000));
  const out = [];
  for (let t = 100, f = 7001000; t <= 1000; t += 100, f += 1000) {
    const r = w.poll(t, rb(f));
    if (r) out.push(r.freq_hz);
  }
  for (let t = 1100; t <= 1600; t += 100) {
    const r = w.poll(t, rb(7010000));
    if (r) out.push(r.freq_hz);
  }
  assert.deepStrictEqual(out, [7010000]);
}

// winSync's own tuning (quiet) and a reload are not reported, and become the new starting point.
{
  const w = KiwiWatch({ debounceMs: 250 });
  w.poll(0, rb(7074000));
  w.quiet(100, 800);                                            // winSync tunes the Kiwi to 14.074
  for (let t = 100; t <= 900; t += 100) assert.strictEqual(w.poll(t, rb(14074000)), null);
  for (let t = 1000; t <= 3000; t += 100) assert.strictEqual(w.poll(t, rb(14074000)), null);
  w.poll(3100, rb(14080000));
  assert.ok(w.poll(3400, rb(14080000)));                        // a real move afterwards is seen
}

// A Kiwi that settles somewhere slightly different from what winSync asked (e.g. a CW offset) is
// not mistaken for the user, because the position is re-read after the quiet time.
{
  const w = KiwiWatch({ debounceMs: 250 });
  w.quiet(0, 800);
  w.poll(100, rb(7030000, "cw"));
  w.poll(500, rb(7029500, "cw"));                               // settles 500 Hz off
  for (let t = 900; t <= 3000; t += 100) assert.strictEqual(w.poll(t, rb(7029500, "cw")), null);
}

// A mode change alone is a change; 1 Hz of rounding jitter is not.
{
  const w = KiwiWatch({ debounceMs: 250 });
  w.poll(0, rb(7074000.4, "usb"));
  assert.strictEqual(w.poll(100, rb(7074001, "usb")), null);
  assert.strictEqual(w.poll(500, rb(7074001, "usb")), null);
  w.poll(600, rb(7074000, "lsb"));
  assert.deepStrictEqual({ ...w.poll(900, rb(7074000, "lsb")) }, { freq_hz: 7074000, kiwi_mode: "lsb" });
}

// WebSDR readback modes are turned into Kiwi names.
{
  const w = KiwiWatch({ debounceMs: 250 });
  w.poll(0, { freq_hz: 3700000, mode: "LSB" });
  w.poll(100, { freq_hz: 3700000, mode: "FM" });
  assert.deepStrictEqual({ ...w.poll(400, { freq_hz: 3700000, mode: "FM" }) },
                         { freq_hz: 3700000, kiwi_mode: "nbfm" });
}

// Rate limit: two moves in quick succession are at least minIntervalMs apart.
{
  const w = KiwiWatch({ debounceMs: 50, minIntervalMs: 500 });
  w.poll(0, rb(7000000));
  w.poll(10, rb(7001000));
  assert.ok(w.poll(70, rb(7001000)));
  w.poll(80, rb(7002000));
  assert.strictEqual(w.poll(200, rb(7002000)), null);            // debounced, but too soon
  assert.ok(w.poll(600, rb(7002000)));
}

// Junk readbacks are ignored.
{
  const w = KiwiWatch({});
  for (const bad of [null, {}, { freq_hz: NaN }, { freq_hz: "7074000" }]) {
    assert.strictEqual(w.poll(0, bad), null);
  }
}

console.log("kiwiwatch tests passed");
