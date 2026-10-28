// winSync bridge: injected by the winSync reverse proxy into the KiwiSDR or WebSDR page.
// Runs inside the receiver page's global scope and wraps its own tuning functions (the same idea as
// CatSync's script injection). All receiver-specific knowledge lives here. The receiver type is
// detected on each call: a page defining setfreqb() is a PA3FWM WebSDR, anything else is a Kiwi.
//
// Verified against KiwiSDR 1.902 client source (kiwisdr.min.js):
//   freqmode_set_dsp_kHz(kHz, mode, opt)  sets mode if it changed, otherwise just the frequency
//   ext_get_freq() / ext_get_mode()       readback
//   zoom_step(ext_zoom.TO_BAND)           zoom waterfall to the band
//   toggle_or_set_mute(1|0), kiwi.muted   audio mute
//
// Verified against WebSDR 20140718 (websdr-base.js, e.g. http://hackgreensdr.org:8901/):
//   setfreqb(kHz)            picks the band that covers the frequency, then tunes (carrier freq, kHz)
//   set_mode("USB"|"LSB"|"AM"|"CW"|"FM")   sets mode and default filter
//   iscw(), lo, hi           CW carrier is offset by the passband centre
//   bi[], allloadeddone, waitingforwaterfalls   band table / start-up state
//   setmute(0|1), #mutecheckbox   audio mute; wfset(4) zooms the waterfall fully out
// and, through winSync on 2026-10-28, the Goonhilly QO-100 narrowband WebSDR
// (https://eshail.batc.org.uk/nb/): page at /nb/, sockets "wss://" + location.host + "/~~stream"
// and "/~~waterstream<band>" (see the WebSocket wrapper below), one band 10,489,750 kHz +/- 256 kHz.
(function () {
  "use strict";

  // Some WebSDRs hard-code "wss://" + location.host for their sockets (e.g. Goonhilly's QO-100
  // WebSDR: /~~stream for audio, /~~waterstream for the waterfall). Through winSync the page is on
  // plain http, so those must be ws:// to winSync, which then connects to the receiver with wss.
  // Only sockets to winSync's own address are changed, and only on an http page. This script is
  // injected at the top of <head>, before the receiver's own scripts run.
  (function () {
    if (typeof location === "undefined" || location.protocol !== "http:" ||
        typeof window.WebSocket !== "function") return;
    var Native = window.WebSocket, own = "wss://" + location.host;
    function WebSocket(url, protocols) {
      if (typeof url === "string" && url.indexOf(own) === 0 && /^($|[\/?#])/.test(url.slice(own.length))) {
        url = "ws://" + url.slice("wss://".length);
      }
      return protocols === undefined ? new Native(url) : new Native(url, protocols);
    }
    WebSocket.prototype = Native.prototype;
    ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach(function (k) { WebSocket[k] = Native[k]; });
    window.WebSocket = WebSocket;
  })();

  // Kiwi silently turns unknown modes into "am", so validate ourselves.
  var MODES = ["am", "amn", "amw", "sam", "sal", "sau", "sas", "qam", "drm",
               "lsb", "lsn", "usb", "usn", "cw", "cwn", "nbfm", "nnfm", "iq"];

  // ---- PA3FWM WebSDR (websdr-base.js) ----------------------------------------------------
  var WEBSDR_MODES = { am: "AM", amn: "AM", amw: "AM", sam: "AM", sal: "AM", sau: "AM", sas: "AM",
                       qam: "AM", drm: "AM", lsb: "LSB", lsn: "LSB", usb: "USB", usn: "USB",
                       cw: "CW", cwn: "CW", nbfm: "FM", nnfm: "FM" };

  function isWebSdr() { return typeof setfreqb === "function" && typeof bi !== "undefined"; }

  // Band index covering freq_kHz, using the same +/-4 kHz margin as WebSDR's own setfreqb().
  function webSdrBand(f) {
    for (var i = 0; i < nvbands; i++) {
      var e = bi[i], w = e.samplerate / 2 + 4;
      if (f > e.centerfreq - w && f < e.centerfreq + w) return i;
    }
    return -1;
  }

  var webSdr = {
    label: "WebSDR",
    ready: function () {
      return typeof allloadeddone !== "undefined" && allloadeddone === true &&
             typeof waitingforwaterfalls !== "undefined" && waitingforwaterfalls === 0 &&
             bi.length > 0 && typeof soundapplet !== "undefined" && soundapplet !== null;
    },
    tune: function (freq_hz, mode, opts) {
      var m = WEBSDR_MODES[mode];
      if (!m) return { ok: false, err: "mode '" + mode + "' not supported by this WebSDR" };
      var f = freq_hz / 1000;
      var b = webSdrBand(f);
      if (b < 0) return { ok: false, err: (freq_hz / 1e6).toFixed(6) + " MHz is outside this WebSDR's bands" };
      var bandChanged = b !== band;
      setfreqb(f);                       // switches band if needed (this may swap USB/LSB)
      set_mode(m);                       // so set the wanted mode afterwards...
      if (iscw()) f -= (hi + lo) / 2;    // ...and put a CW carrier off the dial by the pitch
      setfreq(f);
      if (opts.zoomToBand && bandChanged && typeof wfset === "function") {
        try { wfset(4); } catch (e) { /* waterfall not up yet: not essential */ }
      }
      if (bandChanged) this.syncMute(true);   // a band change can leave the applet's mute out of step
      return { ok: true };
    },
    // Some WebSDR pages (e.g. sdr.websdrmaasbree.nl) toggle a global `Mute` and
    // the #Mute button via MakeCheckbox('Mute'), and their setmute() never touches #mutecheckbox. So
    // the checkbox can't be trusted there; use the button's own state and keep its colour in step.
    mute: function () {
      var btn = document.getElementById("Mute");
      if (btn && typeof MakeCheckbox === "function" && typeof Mute !== "undefined" &&
          typeof soundapplet !== "undefined" && soundapplet) {
        return {
          get: function () { return Mute == 1; },
          set: function (v) {
            if ((Mute == 1) !== !!v) MakeCheckbox("Mute");   // toggles Mute, button colour, setmute()
            webSdr.lastMute = v ? 1 : 0;
          }
        };
      }
      var cb = document.getElementById("mutecheckbox");
      if (!cb || typeof setmute !== "function") return null;
      return {
        get: function () { return !!cb.checked; },
        set: function (v) { cb.checked = !!v; setmute(v ? 1 : 0); webSdr.lastMute = v ? 1 : 0; }
      };
    },
    // The page's mute state and the sound applet's are separate; make the applet follow the page.
    syncMute: function (force) {
      if (typeof setmute !== "function" || typeof soundapplet === "undefined" || !soundapplet) return;
      var m = this.mute();
      if (!m) return;
      var v = m.get() ? 1 : 0;
      if (force || v !== this.lastMute) { setmute(v); this.lastMute = v; }
    },
    readback: function () {
      var m = mode === "AM" || mode === "FM" ? mode : (iscw() ? "CW" : (hi + lo > 0 ? "USB" : "LSB"));
      return { freq_hz: Math.round(nominalfreq() * 1000), mode: m };
    }
  };

  // ---- KiwiSDR ---------------------------------------------------------------------------
  var kiwiSdr = {
    label: "Kiwi",
    ready: function () {
      return typeof freqmode_set_dsp_kHz === "function" &&
             typeof demodulators !== "undefined" && demodulators.length > 0 &&
             typeof bandwidth !== "undefined" && bandwidth > 0;
    },
    tune: function (freq_hz, mode, opts) {
      if (MODES.indexOf(mode) < 0) return { ok: false, err: "unsupported Kiwi mode '" + mode + "'" };

      // Kiwi covers 0..bandwidth Hz of baseband, shifted by any frequency offset (transverter).
      var offset_hz = (typeof kiwi !== "undefined" && kiwi.freq_offset_kHz) ? kiwi.freq_offset_kHz * 1000 : 0;
      var bb_hz = freq_hz - offset_hz;
      if (bb_hz <= 0 || bb_hz > bandwidth) {
        return { ok: false, err: (freq_hz / 1e6).toFixed(6) + " MHz is outside this Kiwi's range" };
      }

      freqmode_set_dsp_kHz(freq_hz / 1000, mode, {});

      if (opts.zoomToBand && typeof zoom_step === "function" && typeof ext_zoom !== "undefined") {
        zoom_step(ext_zoom.TO_BAND);
      }
      return { ok: true };
    },
    mute: function () {
      if (typeof toggle_or_set_mute !== "function" || typeof kiwi === "undefined") return null;
      return {
        get: function () { return !!kiwi.muted; },
        set: function (v) { toggle_or_set_mute(v ? 1 : 0); }
      };
    },
    readback: function () { return { freq_hz: ext_get_freq(), mode: ext_get_mode() }; }
  };

  function driver() { return isWebSdr() ? webSdr : kiwiSdr; }

  var lastTx = false;
  var ownMute = false;   // true only while winSync itself is holding the receiver muted for TX

  window.winSync = {
    version: 3,

    // "Kiwi" or "WebSDR": lets the control page name the receiver correctly.
    kind: function () { return driver().label; },

    // Mute the receiver audio while transmitting (on=true) and restore it afterwards (on=false).
    // Idempotent. Never overrides a mute the user set: we only unmute what we muted ourselves.
    txMute: function (on) {
      try {
        var d = driver(), m = d.mute();
        if (!m) return false;
        var set = false;
        if (on) {
          if (!m.get()) { m.set(true); ownMute = true; set = true; }
        } else if (ownMute) {
          if (m.get()) { m.set(false); set = true; }
          ownMute = false;
        }
        if (d.syncMute) d.syncMute(lastTx && !on && !set);   // forced on the TX -> RX edge
        lastTx = !!on;
        return true;
      } catch (e) { return false; }
    },

    // True once the receiver has finished starting up and can be tuned.
    ready: function () {
      try { return driver().ready(); } catch (e) { return false; }
    },

    // Tune to freq_hz (the radio's dial frequency) in Kiwi-style mode `mode`.
    // Returns {ok:true} or {ok:false, err:"reason"}.
    tune: function (freq_hz, mode, opts) {
      opts = opts || {};
      var d = driver();
      try {
        if (!d.ready()) return { ok: false, err: d.label + " not ready" };
        return d.tune(freq_hz, mode, opts);
      } catch (e) {
        return { ok: false, err: d.label + " call failed: " + e.message };
      }
    },

    // What the receiver is currently tuned to (for status / verification).
    readback: function () {
      try { return driver().readback(); } catch (e) { return null; }
    }
  };
})();
