"""Async CAT client for Thetis's TCP/IP CAT server (Setup > Serial/Network/Midi CAT > Network,
here 127.0.0.1:13013). Checked against Thetis v2.10.3.15 (MI0BOT Hermes Lite 2 build).

Read-only: only the exact strings in SAFE_COMMANDS are ever written. Each one was checked in
Thetis's CATCommands.cs to be a pure read when sent without a parameter. Not every Thetis command
that looks like a read is one (ZZXU; raises XIT, ZZZZ; closes the CAT port), so never add a
command without checking its source. NEVER send TX;, RX;, ZZTX<digit>;, ZZTU<digit>; or anything
with a parameter: they key the transmitter or change the radio.

Thetis only pushes changes if Setup allows auto-information (AI;/ZZAI; answer "?;" otherwise), and
switching it on is a SET, so the state is polled as one batch, answered in order:
  ZZIF;  VFO A frequency, MOX and RX1 mode (Thetis numbering). Unlike FA;/IF;/ZZFA;/ZZMD;,
         MI0BOT's "CAT to VFO B" option never redirects it to VFO B.
  ZZFB;  VFO B frequency
  ZZME;  RX2 mode
  ZZRS;  RX2 on/off
  ZZXV;  combined status: bit 6 = MOX or CAT PTT, bit 7 = TUNE
On connect Thetis sends a "#Thetis TCP/IP Cat ...#;" greeting, which is skipped.

Thetis ties RX1 to VFO A and RX2 to VFO B but does not report a selected receiver (FR; always
answers 0). So with follow="auto" the Kiwi follows VFO A while RX2 is off, and with RX2 on whichever
VFO was tuned (frequency or mode) last. follow="A" or "B" fixes it.
"""
import asyncio
import logging
import re
import time

log = logging.getLogger("winSync.cat")

# Every command winSync may send, exactly. Enforced by a test and by _send().
SAFE_COMMANDS = frozenset({b"ZZIF;", b"ZZFB;", b"ZZME;", b"ZZRS;", b"ZZXV;"})
POLL_BATCH = b"ZZIF;ZZFB;ZZME;ZZRS;ZZXV;"
BATCH_SIZE = 5
FOLLOW_MODES = ("auto", "A", "B")

# ZZIF + freq(11) step(4) rit/xit(+/-5) ritOn xitOn mem(3) TX mode(2) ... (Thetis ZZIF())
_ZZIF_RE = re.compile(r"^ZZIF(\d{11})\d{4}[+-]\d{5}\d\d\d{3}(\d)(\d{2})")
_ZZFB_RE = re.compile(r"^ZZFB(\d{11})$")
_ZZME_RE = re.compile(r"^ZZME(\d{2})$")
_ZZRS_RE = re.compile(r"^ZZRS([01])$")
_ZZXV_RE = re.compile(r"^ZZXV(\d{4})$")
XV_MOX, XV_TUN = 1 << 6, 1 << 7


def split_frames(buf):
    """Split a receive buffer into complete ';'-terminated frames plus the unfinished remainder."""
    *frames, rest = buf.split(";")
    return [f.strip() for f in frames if f.strip()], rest


def parse_frame(frame):
    """Parse a frame we use. Returns (kind, value) or None:
    ('if', (hz, tx, mode)) VFO A; ('freq_b', hz); ('mode_b', mode); ('rx2', bool); ('xv', int)."""
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
    return None


class CatClient:
    """Maintains a connection to Thetis and reports the state of the VFO being followed.

    on_state(freq_hz, mode, tx, vfo, rx2_on) is called whenever any of them changes. vfo is 0 for
    VFO A / RX1 and 1 for VFO B / RX2; mode is the Thetis mode number. on_status(connected) is
    called on connect/disconnect; a drop resets tx.
    """

    def __init__(self, host, port, on_state, on_status, poll_interval=0.1, follow="auto",
                 stall_timeout=3.0):
        self.host, self.port = host, port
        self.on_state, self.on_status = on_state, on_status
        self.poll_interval = poll_interval
        self.stall_timeout = stall_timeout
        self.follow = follow if follow in FOLLOW_MODES else "auto"
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

    def _send(self, cmds):
        # A batch is checked command by command against the whitelist.
        for cmd in re.findall(rb"[^;]*;", cmds):
            assert cmd in SAFE_COMMANDS, f"refusing to send {cmd!r}"
        assert b"".join(re.findall(rb"[^;]*;", cmds)) == cmds, f"refusing to send {cmds!r}"
        self._writer.write(cmds)

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
        while True:
            if self._pending == 0:
                self._send(POLL_BATCH)
                self._pending = BATCH_SIZE
                self._sent_at = time.monotonic()
                await writer.drain()
            elif time.monotonic() - self._sent_at > self.stall_timeout:
                raise ConnectionResetError("Thetis stopped answering CAT requests")
            await asyncio.sleep(self.poll_interval)

    def _handle(self, frame):
        if frame.startswith("#"):        # greeting banner
            return
        parsed = parse_frame(frame)
        if parsed:
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
        elif frame != "?":
            return                       # not an answer to us (e.g. an auto-information push)
        # Each answer, including "?" for a refused command, completes one request of the batch.
        if self._pending > 0:
            self._pending -= 1
            if self._pending == 0:
                self._sample()

    def _sample(self):
        """A full batch has been answered: work out the last-tuned VFO and report."""
        self.tx = self._if_tx or self._xv_tx
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

    def _select(self):
        self.vfo = {"A": 0, "B": 1}.get(self.follow, self._auto_vfo)

    def _emit(self):
        if self.freq_hz is None or self.mode is None:
            return
        state = (self.freq_hz, self.mode, self.tx, self.vfo, self.rx2)
        if state != self._last:
            self._last = state
            self.on_state(*state)
