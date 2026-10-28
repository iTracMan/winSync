"""Async CAT client for Thetis's TCP/IP CAT server (Setup > Serial/Network/Midi CAT > Network,
here 127.0.0.1:13013). Checked against Thetis v2.10.3.15 (MI0BOT Hermes Lite 2 build).

Reads: only the exact strings in SAFE_COMMANDS are ever written. Each one was checked in
Thetis's CATCommands.cs to be a pure read when sent without a parameter. Not every Thetis command
that looks like a read is one (ZZXU; raises XIT, ZZZZ; closes the CAT port), so never add a
command without checking its source. NEVER send TX;, RX;, ZZTX<digit>;, ZZTU<digit>; or anything
with a parameter: they key the transmitter or change the radio.

Sets (two-way sync only, control "kiwi" or "both"): the ONLY other commands that can be written
are those matching SET_RE, set frequency and set mode of the followed VFO (ZZFA/ZZMD for VFO A,
ZZFB/ZZME for VFO B), and only while the radio is receiving, inside the UK amateur bands, from a
fresh reading. _send() re-checks every one of these conditions itself. Valid ZZ sets get no reply;
a refused one gets "?;". MI0BOT's "CAT to VFO B" option sends ZZFA/ZZMD to VFO B, so with control
on ZZFA; is also polled and VFO A is only set once ZZFA; is seen to report VFO A (as ZZIF; does).

Thetis only pushes changes if Setup allows auto-information (AI;/ZZAI; answer "?;" otherwise), and
switching it on is a SET, so the state is polled as one batch, answered in order:
  ZZIF;  VFO A frequency, MOX and RX1 mode (Thetis numbering). Unlike FA;/IF;/ZZFA;/ZZMD;,
         MI0BOT's "CAT to VFO B" option never redirects it to VFO B.
  ZZFB;  VFO B frequency
  ZZME;  RX2 mode
  ZZRS;  RX2 on/off
  ZZXV;  combined status: bit 6 = MOX or CAT PTT, bit 7 = TUNE
  ZZFA;  (control on only) VFO A frequency as the CAT redirect sees it
On connect Thetis sends a "#Thetis TCP/IP Cat ...#;" greeting, which is skipped.

Thetis ties RX1 to VFO A and RX2 to VFO B but does not report a selected receiver (FR; always
answers 0). So with follow="auto" the Kiwi follows VFO A while RX2 is off, and with RX2 on whichever
VFO was tuned (frequency or mode) last. follow="A" or "B" fixes it.
"""
import asyncio
import collections
import logging
import re
import time

from .bands import uk_band_for

log = logging.getLogger("winSync.cat")

# Every read winSync may send, exactly. Enforced by a test and by _send().
SAFE_COMMANDS = frozenset({b"ZZIF;", b"ZZFB;", b"ZZME;", b"ZZRS;", b"ZZXV;", b"ZZFA;"})
POLL_BATCH = b"ZZIF;ZZFB;ZZME;ZZRS;ZZXV;"
CONTROL_BATCH = POLL_BATCH + b"ZZFA;"
FOLLOW_MODES = ("auto", "A", "B")

# Two-way sync. "radio": radio -> Kiwi only (no set is ever sent); "kiwi": Kiwi -> radio;
# "both": whichever was tuned last.
CONTROL_MODES = ("radio", "kiwi", "both")
# The only set commands that can ever be sent (full match). Modes: LSB USB CWL CWU FM AM SAM, the
# ones Kiwi modes map to; never DSB, SPEC, DRM or the DIG modes.
SET_RE = re.compile(rb"(?:ZZF[AB]\d{11}|ZZM[DE](?:00|01|03|04|05|06|10));")
SETTABLE_MODES = frozenset({0, 1, 3, 4, 5, 6, 10})
SET_FREQ = (b"ZZFA", b"ZZFB")      # by VFO: 0 = A, 1 = B
SET_MODE = (b"ZZMD", b"ZZME")
TX_HOLDOFF = 1.0                   # s after TX ends before the radio may be changed
FRESH = 0.5                        # s: a set needs a full reading at most this old
VERIFY_SAMPLES = 3                 # readings within which a set must show up on its VFO
RUNAWAY_SETS, RUNAWAY_WINDOW = 5, 6.0   # more changes than this in this many s: control stops


class UnsafeCommand(AssertionError):
    """Raised instead of writing anything that is not allowed (not an assert: -O can't strip it)."""


CAT_TO_B_NOTE = ("Thetis's CAT to VFO B option is on, so ZZFA/ZZMD would change VFO B: "
                 "VFO A is not changed from the receiver")


def out_of_band(freq_hz):
    return f"{freq_hz / 1e6:.6f} MHz is outside the UK amateur bands"

# ZZIF + freq(11) step(4) rit/xit(+/-5) ritOn xitOn mem(3) TX mode(2) ... (Thetis ZZIF())
_ZZIF_RE = re.compile(r"^ZZIF(\d{11})\d{4}[+-]\d{5}\d\d\d{3}(\d)(\d{2})")
_ZZFB_RE = re.compile(r"^ZZFB(\d{11})$")
_ZZME_RE = re.compile(r"^ZZME(\d{2})$")
_ZZRS_RE = re.compile(r"^ZZRS([01])$")
_ZZXV_RE = re.compile(r"^ZZXV(\d{4})$")
_ZZFA_RE = re.compile(r"^ZZFA(\d{11})$")
XV_MOX, XV_TUN = 1 << 6, 1 << 7


def split_frames(buf):
    """Split a receive buffer into complete ';'-terminated frames plus the unfinished remainder."""
    *frames, rest = buf.split(";")
    return [f.strip() for f in frames if f.strip()], rest


def parse_frame(frame):
    """Parse a frame we use. Returns (kind, value) or None:
    ('if', (hz, tx, mode)) VFO A; ('freq_b', hz); ('mode_b', mode); ('rx2', bool); ('xv', int);
    ('zzfa', hz) VFO A through the CAT redirect."""
    m = _ZZIF_RE.match(frame)
    if m:
        return "if", (int(m.group(1)), m.group(2) == "1", int(m.group(3)))
    m = _ZZFB_RE.match(frame)
    if m:
        return "freq_b", int(m.group(1))
    m = _ZZME_RE.match(frame)
    if m:
        return "mode_b", int(m.group(1))
    m = _ZZRS_RE.match(frame)
    if m:
        return "rx2", m.group(1) == "1"
    m = _ZZXV_RE.match(frame)
    if m:
        return "xv", int(m.group(1))
    m = _ZZFA_RE.match(frame)
    if m:
        return "zzfa", int(m.group(1))
    return None


class CatClient:
    """Maintains a connection to Thetis and reports the state of the VFO being followed.

    on_state(freq_hz, mode, tx, vfo, rx2_on) is called whenever any of them changes. vfo is 0 for
    VFO A / RX1 and 1 for VFO B / RX2; mode is the Thetis mode number. on_status(connected) is
    called on connect/disconnect; a drop resets tx. on_note(text or None) reports a problem that
    stops the radio being changed (two-way sync), or that it has cleared.
    """

    def __init__(self, host, port, on_state, on_status, poll_interval=0.1, follow="auto",
                 stall_timeout=3.0, control="radio", on_note=None):
        self.host, self.port = host, port
        self.on_state, self.on_status = on_state, on_status
        self.on_note = on_note or (lambda note: None)
        self.poll_interval = poll_interval
        self.stall_timeout = stall_timeout
        self.follow = follow if follow in FOLLOW_MODES else "auto"
        self.control = control if control in CONTROL_MODES else "radio"
        self._fault = None           # a set that did not show up as expected: control stops
        self._last_note = None
        self.cat_to_b = None         # ZZFA/ZZMD redirected to VFO B? None = not known yet
        self._zzfa = None
        self._want = None            # (vfo, freq, mode) waiting for the next poll
        self._sent = None            # (vfo, freq, mode, readings left) being verified
        self._sets_in_flight = 0
        self._sampled_at = float("-inf")
        self._tx_ended = float("-inf")
        self._recent_sets = collections.deque()   # times of recent Kiwi -> radio changes
        self.freq_a = self.mode_a = None
        self.freq_b = self.mode_b = None
        self.rx2 = False
        self.tx = False
        self.vfo = 0                 # VFO being followed
        self._auto_vfo = 0           # last-tuned VFO, for follow="auto"
        self._prev = None            # (vfo A, vfo B) values of the previous complete sample
        self._if_tx = self._xv_tx = False
        self._pending = 0
        self._sent_at = 0.0
        self._writer = None
        self._task = None
        self._last = None

    # Followed VFO's values, for tests and status
    @property
    def freq_hz(self):
        return self.freq_b if self.vfo == 1 else self.freq_a

    @property
    def mode(self):
        return self.mode_b if self.vfo == 1 else self.mode_a

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def set_follow(self, follow):
        """'auto', 'A' or 'B'. Takes effect at once."""
        if follow not in FOLLOW_MODES:
            raise ValueError(f"follow must be one of {FOLLOW_MODES}")
        self.follow = follow
        self._select()
        self._emit()
        self._update_note()

    def set_control(self, control):
        """'radio', 'kiwi' or 'both'. Switching to 'radio' drops anything not yet sent."""
        if control not in CONTROL_MODES:
            raise ValueError(f"control must be one of {CONTROL_MODES}")
        self.control = control
        self._fault = None           # choosing a control mode again clears a stopped one
        self._recent_sets.clear()
        if control == "radio":
            self._want = None
        self._update_note()

    def request_tune(self, freq_hz, mode=None):
        """Kiwi -> radio: set the followed VFO to freq_hz and (unless None) Thetis mode `mode`.
        Returns None if it will go with the next poll, otherwise why the radio won't be changed.
        Everything is checked again, against a fresh reading, just before it is sent."""
        reason = self._refusal(self.vfo, freq_hz, mode)
        if reason is None:
            self._want = (self.vfo, freq_hz, mode)
        return reason

    def _refusal(self, vfo, freq_hz=None, mode=None):
        """Why the radio must not be changed now, or None if it may."""
        if self.control == "radio":
            return "Control is set to Radio controls receiver"
        if self._writer is None or self.freq_hz is None:
            return "Thetis is not connected"
        if vfo != self.vfo:
            return "that is not the VFO being followed"
        now = time.monotonic()
        if self.tx or now - self._tx_ended < TX_HOLDOFF:
            return "the radio is transmitting"
        if now - self._sampled_at > FRESH:
            return "no fresh reading from Thetis"
        if self._fault:
            return self._fault
        if vfo == 0 and self.cat_to_b:
            return CAT_TO_B_NOTE
        if vfo == 0 and self.cat_to_b is None:
            return ("still checking Thetis's CAT to VFO B setting (this needs VFO A and VFO B on "
                    "different frequencies)")
        if freq_hz is not None and (not isinstance(freq_hz, int) or uk_band_for(freq_hz) is None):
            return out_of_band(freq_hz)
        if mode is not None and mode not in SETTABLE_MODES:
            return f"Thetis mode {mode} is not one winSync sets"
        return None

    def _send(self, cmds, sets=False):
        # Checked command by command: a read on the whitelist, or (only when sets=True) a set
        # matching SET_RE whose VFO, value and timing pass _refusal() right now.
        parts = re.findall(rb"[^;]*;", cmds)
        if b"".join(parts) != cmds:
            raise UnsafeCommand(f"refusing to send {cmds!r}")
        for cmd in parts:
            if cmd in SAFE_COMMANDS:
                continue
            if not (sets and SET_RE.fullmatch(cmd)):
                raise UnsafeCommand(f"refusing to send {cmd!r}")
            vfo = 0 if cmd[:4] in (b"ZZFA", b"ZZMD") else 1
            value = int(cmd[4:-1])
            is_freq = cmd[:4] in SET_FREQ
            reason = self._refusal(vfo, value if is_freq else None, None if is_freq else value)
            if reason:
                raise UnsafeCommand(f"refusing to send {cmd!r}: {reason}")
        self._writer.write(cmds)

    def _take_sets(self):
        """The set commands for a waiting Kiwi -> radio change, or b"" (dropped if no longer allowed:
        it is never kept for later, e.g. until a transmission ends)."""
        want, self._want = self._want, None
        if want is None:
            return b""
        vfo, freq, mode = want
        reason = self._refusal(vfo, freq, mode)
        if reason:
            log.info("receiver -> radio change dropped: %s", reason)
            return b""
        cur_freq, cur_mode = (self.freq_a, self.mode_a) if vfo == 0 else (self.freq_b, self.mode_b)
        cmds = b""
        if freq != cur_freq:
            cmds += SET_FREQ[vfo] + b"%011d;" % freq
        if mode is not None and mode != cur_mode:
            cmds += SET_MODE[vfo] + b"%02d;" % mode
        if cmds and self._runaway():
            return b""
        if cmds:
            log.info("receiver -> radio: VFO %s %s Hz%s", "AB"[vfo], freq,
                     "" if mode is None else f" mode {mode}")
            self._sent = (vfo, freq, mode, VERIFY_SAMPLES)
        return cmds

    async def _run(self):
        delay = 1.0
        while True:
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
            except OSError as e:
                log.info("CAT connect to %s:%s failed: %s", self.host, self.port, e)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15.0)
                continue
            delay = 1.0
            log.info("CAT connected to Thetis at %s:%s", self.host, self.port)
            self._writer = writer
            # Values from before the drop may be stale; nothing is reported until re-read.
            self.freq_a = self.mode_a = self.freq_b = self.mode_b = None
            self._prev = None
            self._pending = 0
            self.cat_to_b = self._zzfa = self._want = self._sent = self._fault = None
            self._sets_in_flight = 0
            self._sampled_at = float("-inf")
            self.on_status(True)
            try:
                await self._session(reader, writer)
            except (OSError, asyncio.IncompleteReadError) as e:
                log.info("CAT connection lost: %s", e)
            finally:
                writer.close()
                self._writer = None
                if self.tx:  # state unknown once the link is gone; don't stay muted forever
                    self.tx = self._if_tx = self._xv_tx = False
                    self._tx_ended = time.monotonic()
                    self._emit()
                self.on_status(False)
            await asyncio.sleep(1.0)

    async def _session(self, reader, writer):
        poller = asyncio.create_task(self._poll(writer))
        buf = ""
        try:
            while True:
                if poller.done():
                    poller.result()          # re-raise a stall as a lost connection
                try:
                    data = await asyncio.wait_for(reader.read(1024), self.stall_timeout)
                except asyncio.TimeoutError:
                    continue                 # the poller decides whether the link is stalled
                if not data:
                    raise ConnectionResetError("closed by peer")
                buf += data.decode("ascii", errors="replace")
                frames, buf = split_frames(buf)
                for frame in frames:
                    self._handle(frame)
        finally:
            poller.cancel()

    async def _poll(self, writer):
        # One batch at a time: Thetis answers a batch in ~55 ms, so waiting for the previous
        # answers keeps requests from piling up if it is busy.
        # A Kiwi -> radio change goes in front of the batch sent right after a reading completes,
        # so it is checked against the newest reading. The batch then reads the result back.
        while True:
            if self._pending == 0:
                batch = POLL_BATCH if self.control == "radio" else CONTROL_BATCH
                sets = self._take_sets()
                self._send(sets + batch, sets=bool(sets))
                self._sets_in_flight = sets.count(b";")
                self._zzfa = None
                self._pending = batch.count(b";")
                self._sent_at = time.monotonic()
                await writer.drain()
            elif time.monotonic() - self._sent_at > self.stall_timeout:
                raise ConnectionResetError("Thetis stopped answering CAT requests")
            await asyncio.sleep(self.poll_interval)

    def _handle(self, frame):
        if frame.startswith("#"):        # greeting banner
            return
        parsed = parse_frame(frame)
        if frame == "?" and self._sets_in_flight:
            # Thetis answers in order and a set it accepts gets no reply, so a "?" before the
            # batch's first answer is a refused set. It is not one of the batch's answers.
            self._sets_in_flight -= 1
            log.warning("Thetis refused a receiver -> radio set command")
            return
        if parsed:
            self._sets_in_flight = 0
            kind, value = parsed
            if kind == "if":
                self.freq_a, self._if_tx, self.mode_a = value
            elif kind == "freq_b":
                self.freq_b = value
            elif kind == "mode_b":
                self.mode_b = value
            elif kind == "rx2":
                self.rx2 = value
            elif kind == "xv":
                self._xv_tx = bool(value & (XV_MOX | XV_TUN))
            elif kind == "zzfa":
                self._zzfa = value
        elif frame != "?":
            return                       # not an answer to us (e.g. an auto-information push)
        # Each answer, including "?" for a refused command, completes one request of the batch.
        if self._pending > 0:
            self._pending -= 1
            if self._pending == 0:
                self._sample()

    def _sample(self):
        """A full batch has been answered: work out the last-tuned VFO and report."""
        was_tx, self.tx = self.tx, self._if_tx or self._xv_tx
        self._sampled_at = time.monotonic()
        if was_tx and not self.tx:
            self._tx_ended = self._sampled_at
        self._check_redirect()
        self._verify()
        a, b = (self.freq_a, self.mode_a), (self.freq_b, self.mode_b)
        if not self.rx2:
            self._auto_vfo = 0
        elif self._prev is not None:
            a_moved, b_moved = a != self._prev[0], b != self._prev[1]
            if b_moved and not a_moved:
                self._auto_vfo = 1
            elif a_moved and not b_moved:
                self._auto_vfo = 0
            # both moved at once (VFO sync, A>B copy): keep following the same one
        self._prev = (a, b)
        self._select()
        self._emit()
        self._update_note()

    def _runaway(self):
        """More than RUNAWAY_SETS changes within RUNAWAY_WINDOW s is not someone tuning the Kiwi by
        hand (a drag only sends where it stops): something is moving it, e.g. a stuck auto-repeat
        step button or a loop. Stop changing the radio until Control is chosen again."""
        now = time.monotonic()
        while self._recent_sets and now - self._recent_sets[0] > RUNAWAY_WINDOW:
            self._recent_sets.popleft()
        if len(self._recent_sets) >= RUNAWAY_SETS:
            self._fault = (f"the receiver asked to change the radio more than {RUNAWAY_SETS} times in "
                           f"{RUNAWAY_WINDOW:g} s, so winSync has stopped changing the radio "
                           "(choose Control again to retry)")
            log.error("receiver -> radio stopped: %s", self._fault)
            return True
        self._recent_sets.append(now)
        return False

    def _check_redirect(self):
        """Is MI0BOT's "CAT to VFO B" on? Then ZZFA; answers with VFO B while ZZIF; still reports
        VFO A. Only decided while A and B differ."""
        if self._zzfa is None or self.freq_a is None:
            return
        if self._zzfa != self.freq_a:
            redirected = True
        elif self.freq_a != self.freq_b:
            redirected = False
        else:
            return
        if redirected != self.cat_to_b:
            self.cat_to_b = redirected
            log.info("Thetis CAT to VFO B is %s", "on: VFO A won't be set" if redirected else "off")

    def _verify(self):
        """After a set: the followed VFO must show it within a few readings. If the other VFO
        moved there instead, the commands are going to the wrong VFO and control stops."""
        if self._sent is None:
            return
        vfo, freq, mode, left = self._sent
        mine = (self.freq_a, self.mode_a) if vfo == 0 else (self.freq_b, self.mode_b)
        other = self.freq_b if vfo == 0 else self.freq_a
        if mine[0] == freq and (mode is None or mine[1] == mode):
            self._sent = None
        elif other == freq and mine[0] != freq:
            self._sent = None
            self._fault = (f"a change meant for VFO {'AB'[vfo]} moved VFO {'BA'[vfo]}, so winSync "
                           "has stopped changing the radio (choose Control again to retry)")
            log.error("receiver -> radio stopped: %s", self._fault)
        elif left <= 1:
            self._sent = None
            log.warning("Thetis did not take the change to VFO %s %s Hz (VFO locked?)", "AB"[vfo], freq)
        else:
            self._sent = (vfo, freq, mode, left - 1)

    @property
    def note(self):
        """A lasting reason the radio can't be changed (shown on the page), or None."""
        if self.control == "radio":
            return None
        if self._fault:
            return self._fault
        if self.vfo == 0 and self.cat_to_b:
            return CAT_TO_B_NOTE
        return None

    def _update_note(self):
        note = self.note
        if note != self._last_note:
            self._last_note = note
            self.on_note(note)

    def _select(self):
        self.vfo = {"A": 0, "B": 1}.get(self.follow, self._auto_vfo)

    def _emit(self):
        if self.freq_hz is None or self.mode is None:
            return
        state = (self.freq_hz, self.mode, self.tx, self.vfo, self.rx2)
        if state != self._last:
            self._last = state
            self.on_state(*state)
