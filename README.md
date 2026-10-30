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
Windows 10/11. Other radios can be followed, read-only, through OmniRig (see "OmniRig" below), and so
can SDR Console on QO-100, with the Goonhilly WebSDR as the receiver (see "SDR Console / QO-100" below).

> **Extract the zip first.** Right-click the winSync zip file, choose **Extract All**, then run
> `winSync.cmd` from the extracted folder. Double-clicking `winSync.cmd` inside the zip doesn't work:
> Windows copies only that one file to a Temp folder, so winSync shows a message saying to extract it.

## How it works

* A small Python server connects to Thetis's **TCP/IP CAT server** (default here **127.0.0.1:13013**)
  and polls it about 10 times a second. By default it only ever *reads* from the radio (see "CAT
  commands" below). Optional two-way sync lets tuning in the receiver set the radio's frequency and mode.
* The same server acts as a reverse proxy for the receiver you choose, so the receiver's page is served
  from the same origin as the winSync control page. That lets winSync call a Kiwi's own tuning function
  (`freqmode_set_dsp_kHz`) directly, with no fake keystrokes or clicks.
* The receiver type is detected from the page: KiwiSDR, or the classic PA3FWM **WebSDR**
  (e.g. `http://hackgreensdr.org:8901/`), where winSync calls WebSDR's own `setfreqb()` / `set_mode()`.
* The control page (`/_winsync/`) shows the radio state and applies it to the receiver in an iframe.

## Setup (one double-click)

1. In Thetis: **Setup > Serial/Network/Midi CAT > Network**, enable the **TCP/IP CAT server** on
   127.0.0.1 port 13013. (A different port: change `[cat] port` in `config.toml`.)
2. Double-click **`winSync.cmd`** in this folder.

The first time, a console window sets winSync up:

* it looks for Python 3.11 or newer with tkinter. If there is none it offers to install Python 3.12 with
  `winget`, for your user only (no administrator rights needed); answer **Y**;
* it creates a private Python environment (`.venv`) in this folder and installs `aiohttp`, `pywin32`
  (for OmniRig) and `pyserial` (for SDR Console) into it;
* it puts a **winSync** shortcut on the Desktop and in the Start menu.

Then the winSync window opens. Later starts go straight to the window, from either `winSync.cmd` or the
shortcuts. If you move the folder, run `winSync.cmd --setup` (from a Command Prompt in the folder) to fix the
shortcuts; `winSync.cmd --uninstall` removes the shortcuts and `.venv`.

**Upgrading from v1.2:** replace the files and double-click `winSync.cmd` as usual. It notices that
`pyserial` (new in v1.3) is missing, runs the setup once to add it to the existing `.venv`, and opens the
window. Nothing to do by hand.

If Windows SmartScreen asks about `winSync.cmd` the first time, choose *More info* > *Run anyway*.

## The winSync window

1. Start Thetis with the TCP/IP CAT server on, then tick
   **"Thetis is running and its TCP/IP CAT server is enabled"**. Start stays greyed out until you do.
   **Radio** chooses Thetis, OmniRig Rig 1 / Rig 2 or SDR Console; with OmniRig or SDR Console the box
   asks you to confirm that instead. The window remembers the Radio you last started with (in
   `%APPDATA%\winSync\last_radio.json`, beside `urls.json`) and opens on it next time; the first time,
   or if that file is missing or unreadable, it opens on `[cat] source` from `config.toml`.
2. Press **Start winSync**. Its output scrolls in the field below; **Stop winSync** stops it.
   **Control** (below the Start button, default `[sync] control` in `config.toml`) chooses two-way sync
   when winSync starts: Radio controls receiver, Receiver controls radio or Both. While winSync runs it is
   locked; change it with the Control selector on the page instead.
3. Press **Open winSync page** to open <http://127.0.0.1:8090/_winsync/> in your default browser
   (only enabled while winSync is running). **Open websdr map** opens
   <https://rx-tx.info/map-sdr-points#close>, handy for finding a receiver.

Closing the window stops winSync. If port 8090 is already in use (for example winSync is already running)
the window says so.

## Using the page

The page connects to `[kiwi] url` from `config.toml` (default `http://m0cke.proxy.kiwisdr.com:8073/`), or
enter another Kiwi or WebSDR URL and press **Connect**. Click once inside the Kiwi to start audio (browser
autoplay rules); on a WebSDR click its green audio start button. Receiver URLs you connect to are kept in the
drop-down list and saved in `%APPDATA%\winSync\urls.json`. With **Radio: SDR Console** the page connects to
`[sdrconsole] url` instead (default the Goonhilly QO-100 WebSDR). A receiver page below the root of its site,
such as `https://eshail.batc.org.uk/nb/`, keeps its path.

## Behaviour notes

* **Control** (selector on the page; default `[sync] control` in `config.toml`, or `--control`):
  * **Radio controls receiver** (`radio`, the default, as v1.0): tuning inside the receiver does not
    affect Thetis; the next radio change (or **Resync**) puts the receiver back on the radio's frequency.
  * **Receiver controls radio** (`kiwi`): tuning the receiver sets the followed VFO's frequency and mode
    in Thetis. Radio changes no longer move the receiver (Resync still does).

  (The `config.toml` values are still `radio`, `kiwi` and `both`; before v1.3 the first two were
  labelled "Radio controls Kiwi" and "Kiwi controls radio".)
  * **Both** (`both`): whichever was tuned last wins.

  The radio is only changed when **all** of these hold, otherwise the page says "Radio not moved: …"
  and the receiver stays where you put it:
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
    change VFO B, so VFO A is never set from the receiver and the page says why. winSync can only tell
    while VFO A and VFO B are on different frequencies. If a change ever shows up on the other VFO,
    winSync stops changing the radio until you choose Control again.

  Kiwi modes map back as LSB, USB, CW (CWU, or CWL if the radio is already in CWL), AM, SAM and FM.
  The radio's mode is left alone when it already matches: Kiwi `usb` doesn't turn DIGU into USB, and
  narrow variants such as `usn` or `cwn` stay on the Kiwi. Kiwi modes with no radio equivalent
  (`iq`, `drm`) change only the frequency. A drag across the waterfall sends only where it stops
  (it must hold still for 0.25 s), and at most about five changes a second are sent.
* **Follows the VFO in use.** Thetis ties RX1 to VFO A and RX2 to VFO B, but does not report which
  receiver you are using (its `FR;` always answers 0). So with **Follow: Auto (last tuned)** the receiver
  follows VFO A while RX2 is off, and with RX2 on whichever VFO you tuned (frequency or mode) last.
  When both move at once (VFO sync, A>B copy) it stays on the current one. **Follow: VFO A** or
  **VFO B** fixes it. The default is `[sync] follow` in `config.toml`. The page shows
  "RX1 · VFO A" or "RX2 · VFO B" for the VFO being followed.
* **Mute on TX** (checkbox, remembered by the browser; default `mute_on_tx` in `config.toml`): the
  receiver's audio is muted while Thetis transmits (MOX, CAT PTT or TUNE) and restored on return to
  receive. If you had muted the receiver yourself, winSync leaves it muted. TX state is polled, so the mute
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
* Bind to `127.0.0.1` (default) unless you need LAN access; the server proxies to whatever receiver you
  enter, so don't expose it to untrusted networks.
* Please respect each Kiwi operator's time limits and rules.

## CAT commands

With Control on **Radio controls receiver** (the default) winSync sends exactly five commands, all reads,
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

## OmniRig (other radios, read-only)

Instead of Thetis, winSync can follow any radio that [OmniRig](https://www.dxatlas.com/OmniRig/)
(VE3NEA's free CAT engine, v1.20) supports. Choose **Radio: OmniRig Rig 1** or **Rig 2** in the winSync
window, or set it in `config.toml`:

```toml
[cat]
source = "omnirig"

[omnirig]
rig = 1          # OmniRig's Rig 1 or Rig 2
```

(`--source omnirig --rig 2` on the command line.) Set the radio up in OmniRig itself (rig type, COM port,
baud rate). winSync starts OmniRig through COM when it needs it, using `pywin32` (installed by setup).

* **Read-only, always.** winSync only reads OmniRig's `Status`, `StatusStr`, `RigType`,
  `ReadableParams`, `Freq`, `FreqA`, `FreqB`, `Vfo`, `Mode` and `Tx`. It never sets a property or calls
  a method (`winSync/omnirig.py`, `ReadOnlyRig`; the tests check that against a fake OmniRig that records
  everything). So two-way sync is off with OmniRig: Control is fixed to *Radio controls receiver*, and the
  page's Control selector is greyed out.
* OmniRig itself sends its rig file's commands to the radio (to poll it, plus any `[INIT]` commands in
  that rig's `.ini` file). That is OmniRig's traffic, not winSync's.
* Modes: LSB, USB, CW-L, CW-U, FM, AM, DIG-U and DIG-L map to Kiwi modes as for Thetis. If the radio
  reports another mode, the receiver is not tuned and the page says why.
* VFO: Follow Auto follows the VFO OmniRig reports as receiving; Follow A or B uses `FreqA` / `FreqB`.
* TX: if the rig's OmniRig file doesn't report TX, the page says so, and Mute on TX can't work.
* If OmniRig isn't installed, the radio isn't responding or OmniRig closes, the page says so and winSync
  keeps retrying.

### Testing OmniRig with Thetis

Without another radio, Thetis can stand in for one: OmniRig talks to it as a Kenwood TS-2000 over a pair
of virtual COM ports.

1. Install **com0com** (the signed 2.2.2.0 build, from the com0com project on SourceForge) and create a
   pair, e.g. **COM20 <-> COM21** (leave *emulate baud rate* off). **OmniRig 1.20 can't see ports above
   COM20**, so OmniRig's end of the pair must be COM20 or lower.
2. In Thetis: **Setup > Serial/Network/Midi CAT > CAT Control**: CAT port **COM21**, 9600 8N1, **ID as
   TS-2000**, PTT by RTS/DTR off, *Allow Kenwood AI Command* off, and tick **Enable CAT**. (Thetis's
   TCP/IP CAT server for normal winSync stays as it is.)
3. Install OmniRig 1.20 and open **OmniRig Settings**: Rig 1, rig type **TS-2000**, port **COM20**, 9600,
   8 data bits, no parity, 1 stop bit, RTS and DTR Low, poll 500 ms, timeout 4000 ms. The status should
   change to *On-line*.
   *Before this step, look at the `[INIT]` section of OmniRig's `Rigs\TS-2000.ini`*: those commands go to
   Thetis as soon as OmniRig connects (in 1.20 it is only `AI0;`).
4. In the winSync window choose **Radio: OmniRig Rig 1**, tick the box and press **Start winSync**. The
   log should say `OmniRig Rig 1 online: TS-2000`; the page shows **OmniRig: connected** and follows
   Thetis's VFO A as you tune, change mode and press MOX or TUNE (TX).
5. Quit OmniRig or untick Enable CAT in Thetis: the page should say why it is disconnected, and recover
   when you undo it.

What to expect from Thetis as a TS-2000 (from Thetis's source): OmniRig polls `IF;`, `FA;` and `FB;`, all
reads. `IF;` reports VFO A (VFO B with MI0BOT's CAT to VFO B), always as "receiving on VFO A", and TX
from MOX only. Modes Thetis has no Kenwood code for (SAM, DSB, SPEC, DRM) are reported as USB, so the
Kiwi goes to USB. The `[INIT]` command `AI0;` (auto-information off) is refused with `?;` unless Thetis
allows Kenwood frequency broadcasts, and changes nothing else.

## SDR Console / QO-100 (read-only)

winSync can follow **SDR Console** (v3.4; tested with an Adalm Pluto Plus on QO-100) and keep the
**Goonhilly QO-100 narrowband WebSDR** (<https://eshail.batc.org.uk/nb/>, BATC / AMSAT-UK, PA3FWM WebSDR
software) on SDR Console's receive frequency and mode, with Mute on TX.

1. Create a com0com virtual serial pair for SDR Console, e.g. **COM22 <-> COM23** (com0com 2.2.2.0, *emulate
   baud rate* on). This is a separate pair from the COM20 <-> COM21 pair used for Thetis's CAT1 / OmniRig;
   leave that one alone.
2. In SDR Console: **Program Options > Controllers > CAT (Serial Port) > Port Selection**, tick **COM22**.
   (57,600 baud, SDR Console's default for a virtual port.)
3. In the winSync window choose **Radio: SDR Console**, tick the box and press **Start winSync**. winSync
   opens the other end, **COM23**, only while it runs, and closes it when you stop winSync or close the
   window.

```toml
[cat]
source = "sdrconsole"

[sdrconsole]
port = "COM23"                            # winSync's end of the pair
baud = 57600
url = "https://eshail.batc.org.uk/nb/"   # the receiver used with SDR Console
```

(`--source sdrconsole` on the command line.) Thetis and OmniRig keep using `[kiwi] url`.

* **Read-only, always.** winSync only ever sends `FA;`, `IF;`, `MD;` and `NA;` (in practice `NA;` once, for
  the radio name on the page, then `IF;` about 10 times a second). `winSync/sdrconsole.py` refuses
  anything else, and a test tries every other two-letter command, bare and with parameters. This
  matters: **in SDR Console `TX;` switches to transmit and `RX;` to receive**; they are not status
  reads, and `FA…;` / `MD…;` with a value change the radio. So two-way sync is off with SDR Console:
  Control is fixed to *Radio controls receiver*, the page's Control and Follow selectors are greyed out, and
  the page always says "SDR Console is read-only: tuning the receiver doesn't move the radio".
* SDR Console has auto-information always off, so winSync polls. One `IF;` reply carries all three
  values: e.g. `IF104897300000000+0000000000020000000;` is 10,489,730,000 Hz (characters 2–12, counting
  from 0), receiving (character 28: `0` RX, `1` TX), USB (character 29). The frequency is SDR Console's
  10 GHz receive frequency, so no offset is needed, and it stays there while transmitting.
* Modes (SDR Console's mode digit → shown on the page → Kiwi → WebSDR). Only LSB and USB have been seen
  live so far; the rest follow SDR Console's documentation:

  | SDR Console | Page | Kiwi | WebSDR |
  |---|---|---|---|
  | 1 LSB | LSB | lsb | LSB |
  | 2 USB | USB | usb | USB |
  | 3 CW (upper) | CWU | cw | CW |
  | 7 CW (lower) | CWL | cw | CW |
  | 4 FM | FM | nbfm | FM |
  | 8 Wide FM | FM | nbfm | FM |
  | 9 Broadcast FM | FM | nbfm | FM |
  | 0 DSB | DSB | am | AM |
  | 5 SAM | SAM | sam | AM |
  | 6 (not used) | — | not tuned, the page says why | — |

  A `[modes]` table in `config.toml` changes the Kiwi side as for Thetis (keys are the Thetis mode
  numbers listed there: DSB 2, SAM 10, FM 5, CWU 4, CWL 3).
* **Mute on TX** follows `IF;` character 28, polled about 10 times a second, as for Thetis. If SDR Console
  stops answering for 3 s, or COM23 goes away, during a transmission, winSync treats it as receiving and
  unmutes. (The 1 s "after TX" hold-off in Behaviour notes is for two-way sync, which SDR Console doesn't
  have.) Through the satellite and the WebSDR's own audio buffer you may still hear the very end of your
  own signal after you return to receive.
* The page shows **SDR Console: connected (name)**, with the name from `NA;` (winSync shows whatever follows
  `NA`; on mas-laptop it showed "Pluto_SDR_HOME"). When something is wrong it says what:
  * *COM23 does not exist*: the com0com pair is missing, or `[sdrconsole] port` is wrong;
  * *COM23 is in use by another program*: winSync is already running, or SDR Console was set to COM23
    instead of COM22;
  * *SDR Console does not answer on COM23*: SDR Console isn't running, or COM22 isn't ticked in its CAT
    settings. winSync keeps trying and recovers by itself.
* **Goonhilly through winSync.** Two things had to change for this WebSDR, checked against the real site
  on 2026-10-28. Its page is at `/nb/` (the site root is the project page) while its sockets and `/~~`
  requests are at the root, so winSync proxies the whole site and opens the page at its path. And its
  audio and waterfall scripts open `wss://` + the page's own address, which would fail through winSync's
  plain http page, so `bridge.js` points those (and only those) at `ws://` to winSync, which then
  connects to Goonhilly with `wss`. Kiwis and other WebSDRs are not affected.
* The Goonhilly WebSDR has one band, about 10489.494–10490.006 MHz (the narrowband transponder). A
  frequency outside it is reported ("… is outside this WebSDR's bands") and skipped. 10 GHz is not one of
  winSync's amateur bands, so the page shows the band as **GEN** and never re-zooms the waterfall.

To try it without the radio, `.venv\Scripts\python.exe -m tests.fake_sdrconsole 7300` runs a fake SDR
Console on TCP port 7300 (set `[sdrconsole] port = "socket://127.0.0.1:7300"`), or give it the free end of
a spare com0com pair instead of a port number. Type `f 10489600000`, `m 1` (mode digit) or `tx 1` / `tx 0`.

## Running from a terminal instead

```bat
.venv\Scripts\python.exe -m winSync            rem or: --config path\to\config.toml, --follow A|B|auto, -v
```

`--control radio|kiwi|both` overrides `[sync] control`; `--source thetis|omnirig|sdrconsole` and `--rig 1|2`
override `[cat] source` and `[omnirig] rig`. For testing two-way sync, `--dry-run` makes it
decide as usual but never send anything to the radio (the log says what it would have done), and
`--diag` makes the page log every receiver readback change, every time winSync tunes the receiver and every
change it treats as yours.

## Tests

```bat
.venv\Scripts\python.exe -m pip install pytest
.venv\Scripts\python.exe -m pytest tests
node tests\test_bridge.js      rem bridge.js logic against stubbed Kiwi and WebSDR pages (needs Node.js)
node tests\test_kiwiwatch.js   rem Kiwi -> radio change detection
node tests\page_e2e.js         rem the real page against a real server and the fake Thetis (ports 13114, 8091)
node tests\test_page_reconnect.js   rem the page while the winSync server is down
node tests\test_page_sources.js     rem the page's status and read-only note for each Radio source
```

Node.js LTS: `winget install OpenJS.NodeJS.LTS`.

The logo and icons are made from piSync's artwork by `tools\make_logo.py`
(`.venv\Scripts\python.exe -m pip install pillow` first; it reads `..\piSync-Logo.png` or a path you give it).

`.venv\Scripts\python.exe -m tests.fake_thetis` runs a fake Thetis CAT server on port 13013 for trying
winSync without a radio (type `a 14074000 1`, `b 7030000 4`, `rx2 1`, `tx 1`).
