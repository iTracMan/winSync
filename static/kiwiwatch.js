// winSync: spots tuning done inside the receiver page, for Kiwi -> radio (two-way sync).
// No DOM here, so tests/test_kiwiwatch.js can drive it with a fake clock.
//
// Changes winSync makes itself are never reported: before winSync tunes the receiver (or while it
// loads) the watch is told to be quiet for a while, and afterwards it takes the receiver's position
// as its new starting point. Only a move away from that, held steady for debounceMs (so dragging the
// waterfall reports only where it stops), is reported, and at most once every minIntervalMs.
(function (root) {
  "use strict";

  // WebSDR readback modes -> Kiwi mode names (Kiwi readback already uses these).
  var WEBSDR_MODES = { LSB: "lsb", USB: "usb", CW: "cw", AM: "am", FM: "nbfm" };

  function KiwiWatch(opts) {
    opts = opts || {};
    var debounceMs = opts.debounceMs || 250;
    var minIntervalMs = opts.minIntervalMs || 200;
    var seen = null;            // receiver position winSync knows about
    var quietUntil = 0;
    var pending = null, pendingSince = 0;
    var lastReport = -Infinity;

    function norm(rb) {
      if (!rb || typeof rb.freq_hz !== "number" || !isFinite(rb.freq_hz)) return null;
      var mode = typeof rb.mode === "string" ? (WEBSDR_MODES[rb.mode] || rb.mode) : null;
      return { freq_hz: Math.round(rb.freq_hz), kiwi_mode: mode };
    }
    function same(a, b) { return Math.abs(a.freq_hz - b.freq_hz) <= 1 && a.kiwi_mode === b.kiwi_mode; }

    return {
      // winSync is about to tune the receiver itself, or it is (re)loading: ignore it for ms.
      quiet: function (now, ms) {
        quietUntil = Math.max(quietUntil, now + ms);
        seen = null; pending = null;
      },
      // Call every tick with the receiver's readback. Returns {freq_hz, kiwi_mode} when the user
      // has tuned the receiver, otherwise null.
      poll: function (now, rb) {
        var cur = norm(rb);
        if (!cur) return null;
        if (now < quietUntil || !seen) { seen = cur; pending = null; return null; }
        if (same(cur, seen)) { pending = null; return null; }
        if (!pending || !same(cur, pending)) { pending = cur; pendingSince = now; return null; }
        if (now - pendingSince < debounceMs || now - lastReport < minIntervalMs) return null;
        seen = cur; pending = null; lastReport = now;
        return cur;
      }
    };
  }

  root.KiwiWatch = KiwiWatch;
})(typeof window !== "undefined" ? window : this);
