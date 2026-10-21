# winSync

> **Based on piSync by M0AWS.** winSync is the Windows / Thetis version of **piSync v0.9**, M0AWS's
> Linux and Raspberry Pi tool that keeps a KiwiSDR or WebSDR tuned to piHPSDR. Its design (read-only CAT,
> the reverse proxy and bridge.js approach, mute on TX, following the active VFO), the web page, the
> control window and the logo artwork all come from piSync. All credit for the original goes to M0AWS.
>
> The idea for piSync came from **Steve M0XVT**, and the code was written by **Mike M0AWS**. winSync is
> shared with Mike's permission.

Keeps a [KiwiSDR](http://kiwisdr.com/) or [WebSDR](http://websdr.org/) web receiver tuned to the same
**frequency, mode and band** as **Thetis** (tested with v2.10.3.15, MI0BOT Hermes Lite 2 build) on
Windows 10/11.

## How it works

* A small Python server connects to Thetis's **TCP/IP CAT server** (default here **127.0.0.1:13013**)
  and polls it about 10 times a second. By default it only ever *reads* from the radio (see "CAT
  commands" below). Optional two-way sync lets tuning in the Kiwi set the radio's frequency and mode.
* The same server acts as a reverse proxy for the Kiwi you choose, so the Kiwi page is served from the
  same origin as the winSync control page. That lets winSync call Kiwi's own tuning function
  (`freqmode_set_dsp_kHz`) directly, with no fake keystrokes or clicks.
* The receiver type is detected from the page: KiwiSDR, or the classic PA3FWM **WebSDR**
  (e.g. `http://hackgreensdr.org:8901/`), where winSync calls WebSDR's own `setfreqb()` / `set_mode()`.
* The control page (`/_winsync/`) shows the radio state and applies it to the Kiwi in an iframe.

## Setup (one double-click)

1. In Thetis: **Setup > Serial/Network/Midi CAT > Network**, enable the **TCP/IP CAT server** on
   127.0.0.1 port 13013. (A different port: change `[cat] port` in `config.toml`.)
2. Double-click **`winSync.cmd`** in this folder.

The first time, a console window sets winSync up:

* it looks for Python 3.11 or newer with tkinter. If there is none it offers to install Python 3.12 with
  `winget`, for your user only (no administrator rights needed); answer **Y**;
* it creates a private Python environment (`.venv`) in this folder and installs `aiohttp` into it;
* it puts a **winSync** shortcut on the Desktop and in the Start menu.

Then the winSync window opens. Later starts go straight to the window, from either `winSync.cmd` or the
shortcuts. If you move the folder, run `winSync.cmd --setup` (from a Command Prompt in the folder) to fix the
shortcuts; `winSync.cmd --uninstall` removes the shortcuts and `.venv`.

If Windows SmartScreen asks about `winSync.cmd` the first time, choose *More info* > *Run anyway*.

## The winSync window

1. Start Thetis with the TCP/IP CAT server on, then tick
   **"Thetis is running and its TCP/IP CAT server is enabled"**. Start stays greyed out until you do.
2. Press **Start winSync**. Its output scrolls in the field below; **Stop winSync** stops it.
3. Press **Open winSync page** to open <http://127.0.0.1:8090/_winsync/> in your default browser
   (only enabled while winSync is running). **Open websdr map** opens
   <https://rx-tx.info/map-sdr-points#close>, handy for finding a receiver.

Closing the window stops winSync. If port 8090 is already in use (for example winSync is already running)
the window says so.

## Using the page

The page connects to `[kiwi] url` from `config.toml` (default `http://m0cke.proxy.kiwisdr.com:8073/`), or
enter another Kiwi or WebSDR URL and press **Connect**. Click once inside the Kiwi to start audio (browser
autoplay rules); on a WebSDR click its green audio start button. Receiver URLs you connect to are kept in the
drop-down list and saved in `%APPDATA%\winSync\urls.json`.

## Behaviour notes

* **Control** (selector on the page; default `[sync] control` in `config.toml`, or `--control`):
  * **Radio controls Kiwi** (`radio`, the default, as v1.0): tuning inside the Kiwi does not affect
    Thetis; the next radio change (or **Resync**) puts the Kiwi back on the radio's frequency.
  * **Kiwi controls radio** (`kiwi`): tuning the Kiwi sets the followed VFO's frequency and mode in
    Thetis. Radio changes no longer move the Kiwi (Resync still does).
  * **Both** (`both`): whichever was tuned last wins.

  The radio is only changed when **all** of these hold, otherwise the page says "Radio not moved: …"
  and the Kiwi stays where you put it:
  * the radio is receiving: not MOX, CAT PTT or TUNE, and not within 1 s of the end of a
    transmission. A change that arrives during TX is dropped, not saved for later;
  * the frequency is inside the **UK amateur bands**, exactly as Thetis defines them for its UK region
    (low edge included, high edge excluded): 160m 1.810–2.000, 80m 3.500–3.800, 60m 5.250–5.410,
    40m 7.000–7.200, 30m 10.100–10.150, 20m 14.000–14.350, 17m 18.068–18.168, 15m 21.000–21.450,
    12m 24.890–24.990, 10m 28.000–29.700, 6m 50.030–52.000, 2m 144–148 MHz. (Thetis's "Extended"
    option allows nearly any frequency, so its table is deliberately not used.) The dial frequency
    is checked, so an SSB signal tuned right at a band edge can still spill a few kHz over it;
  * only the **followed VFO** is changed (Follow), using `ZZFA`/`ZZMD` for VFO A or `ZZFB`/`ZZME`
    for VFO B, from a reading of Thetis at most 0.5 s old;
  * for VFO A: Thetis's MI0BOT "CAT to VFO B" option is seen to be off. With it on, `ZZFA`/`ZZMD`
    change VFO B, so VFO A is never set from the Kiwi and the page says why. winSync can only tell
    while VFO A and VFO B are on different frequencies. If a change ever shows up on the other VFO,
    winSync stops changing the radio until you choose Control again.

  Kiwi modes map back as LSB, USB, CW (CWU, or CWL if the radio is already in CWL), AM, SAM and FM.
  The radio's mode is left alone when it already matches: Kiwi `usb` doesn't turn DIGU into USB, and
  narrow variants such as `usn` or `cwn` stay on the Kiwi. Kiwi modes with no radio equivalent
  (`iq`, `drm`) change only the frequency. A drag across the waterfall sends only where it stops
  (it must hold still for 0.25 s), and at most about five changes a second are sent.
* **Follows the VFO in use.** Thetis ties RX1 to VFO A and RX2 to VFO B, but does not report which
  receiver you are using (its `FR;` always answers 0). So with **Follow: Auto (last tuned)** the Kiwi
  follows VFO A while RX2 is off, and with RX2 on whichever VFO you tuned (frequency or mode) last.
  When both move at once (VFO sync, A>B copy) it stays on the current one. **Follow: VFO A** or
  **VFO B** fixes it. The default is `[sync] follow` in `config.toml`. The page shows
  "RX1 · VFO A" or "RX2 · VFO B" for the VFO being followed.
* **Mute on TX** (checkbox, remembered by the browser; default `mute_on_tx` in `config.toml`): the
  receiver's audio is muted while Thetis transmits (MOX, CAT PTT or TUNE) and restored on return to
  receive. If you had muted the Kiwi yourself, winSync leaves it muted. TX state is polled, so the mute
  follows the start of a transmission within about 0.1–0.15 s. If the CAT link drops during TX, winSync
  treats the radio as receiving and unmutes.
  Tested live with Thetis v2.10.3.15 (MI0BOT HL2) on 2026-10-19: CW in CWU sent from Thetis's CWX form
  shows TX in the winSync log while keyed and RX afterwards, and the Kiwi was silent throughout; TUNE in
  LSB mutes too. The Kiwi itself delays its audio by 2–3 seconds, so its sound stops and resumes that much
  later than the TX/RX change in the log. That is the Kiwi's own audio buffering, not winSync.
* Thetis modes map to Kiwi modes as: LSB→lsb, USB→usb, CWL/CWU→cw, FM→nbfm, AM→am, SAM→sam,
  DIGU→usb, DIGL→lsb, DSB→am, DRM→am. SPEC has no equivalent and is not applied. Override with a
  `[modes]` table in `config.toml` (keys are Thetis mode numbers, listed there). On a WebSDR the modes are
  reduced to its five: LSB, USB, CW, AM and FM.
* Frequencies outside the receiver's coverage are reported and skipped (a WebSDR only covers its own bands).
* Bind to `127.0.0.1` (default) unless you need LAN access; the server proxies to whatever Kiwi you
  enter, so don't expose it to untrusted networks.
* Please respect each Kiwi operator's time limits and rules.

## CAT commands

With Control on **Radio controls Kiwi** (the default) winSync sends exactly five commands, all reads,
as one batch per poll: `ZZIF;ZZFB;ZZME;ZZRS;ZZXV;`. `winSync/cat.py` refuses to send anything else,
and a test checks this. With two-way sync on, the batch also reads `ZZFA;` (a pure read that returns
VFO A, or VFO B when "CAT to VFO B" is on), and the only other commands that can ever be written are:

| Command | Sets |
|---|---|
| `ZZFA` + 11 digits | VFO A frequency in Hz (only while following VFO A) |
| `ZZFB` + 11 digits | VFO B frequency in Hz (only while following VFO B) |
| `ZZMD` + `00` `01` `03` `04` `05` `06` or `10` | RX1 mode: LSB, USB, CWL, CWU, FM, AM or SAM |
| `ZZME` + the same | RX2 mode |

Each one is checked again just before it is written: syntax, VFO, UK band, receive state and
freshness. Thetis's other 400-odd CAT commands are refused, whether bare or with parameters (a test
tries every one of them), so DSB, SPEC, DRM and DIG modes are never set either. The rest of this
section describes the reads. Each command was checked in
Thetis's source (`CATCommands.cs`) to be a pure read when sent with no parameter. Not every Thetis command
that looks like a read is one: `ZZXU;` raises XIT and `ZZZZ;` closes the CAT port, for example. winSync
never sends `TX;`, `ZZTX1;`, `ZZTU1;` or any command that changes the radio, and it does not switch
on Thetis's auto-information either.

| Command | Used for |
|---|---|
| `ZZIF;` | VFO A frequency, RX1 mode, MOX. Unlike `FA;`/`IF;`/`ZZFA;`/`ZZMD;`, the MI0BOT build's "CAT to VFO B" option never redirects it to VFO B. |
| `ZZFB;` | VFO B frequency |
| `ZZME;` | RX2 mode |
| `ZZRS;` | RX2 on/off |
| `ZZXV;` | combined status: bit 6 = MOX or CAT PTT, bit 7 = TUNE |

These were tested against Thetis v2.10.3.15 (MI0BOT HL2) on 2026-10-19:

| Command | RX2 off | RX2 on (VFO A 3.750 LSB, VFO B 3.7494 USB) |
|---|---|---|
| connect | `#Thetis TCP/IP Cat - Thetis v2.10.3.15 x64 (09/04/26) HL2 (MI0BOT)#;` | same |
| `ZZIF;` | `ZZIF000036840000011+00000000000000000000;` | `ZZIF000037500000011+00000000000000000000;` |
| `ZZFB;` | `ZZFB00007100000;` | `ZZFB00003749400;` |
| `ZZME;` | `ZZME00;` | `ZZME01;` |
| `ZZRS;` | `ZZRS0;` | `ZZRS1;` |
| `ZZXV;` | `ZZXV0016;` (CTUN on) | `ZZXV0000;` |
| `FR;` / `ZZYR;` | `FR0;` / `ZZYR0;` | `FR0;` / `ZZYR0;` (no selected-receiver info) |
| `AI;` / `ZZAI;` | `?;` | `?;` (auto-information off, so winSync polls) |

A batch of all five takes about 56 ms to answer, against 51 ms for a single command.

## Running from a terminal instead

```bat
.venv\Scripts\python.exe -m winSync            rem or: --config path\to\config.toml, --follow A|B|auto, -v
```

`--control radio|kiwi|both` overrides `[sync] control`. For testing two-way sync, `--dry-run` makes it
decide as usual but never send anything to the radio (the log says what it would have done), and
`--diag` makes the page log every Kiwi readback change, every time winSync tunes the Kiwi and every
change it treats as yours.

## Tests

```bat
.venv\Scripts\python.exe -m pip install pytest
.venv\Scripts\python.exe -m pytest tests
node tests\test_bridge.js      rem bridge.js logic against stubbed Kiwi and WebSDR pages (needs Node.js)
node tests\test_kiwiwatch.js   rem Kiwi -> radio change detection
node tests\page_e2e.js         rem the real page against a real server and the fake Thetis (ports 13114, 8091)
```

Node.js LTS: `winget install OpenJS.NodeJS.LTS`.

The logo and icons are made from piSync's artwork by `tools\make_logo.py`
(`.venv\Scripts\python.exe -m pip install pillow` first; it reads `..\piSync-Logo.png` or a path you give it).

`.venv\Scripts\python.exe -m tests.fake_thetis` runs a fake Thetis CAT server on port 13013 for trying
winSync without a radio (type `a 14074000 1`, `b 7030000 4`, `rx2 1`, `tx 1`).
