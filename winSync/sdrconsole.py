"""Read-only SDR Console source: the receive frequency, mode and TX state of SDR Console (v3.4, here an
Adalm Pluto Plus on QO-100), read over its CAT serial port (https://www.sdr-radio.com/SerialPort).

SDR Console opens one end of a com0com virtual serial pair (here COM22, ticked in Program Options >
Controllers > CAT (Serial Port) > Port Selection) and winSync opens the other (COM23), 57,600 baud 8N1.
It speaks a Kenwood TS-2000 subset with auto-information always off, so winSync polls.

Read-only: only the exact strings in SAFE_COMMANDS are ever written, through SafePort, which refuses
anything else. DANGER: in SDR Console "TX;" switches to TRANSMIT and "RX;" to receive; they are not
status reads. FA<digits>; and MD<digit>; change the radio. Never add a command without checking the
SDR Console documentation that it is a pure read. Two-way sync is not available with SDR Console.

  IF;  the main poll: frequency, TX and mode in one 38-character reply, e.g.
       IF104897300000000+0000000000020000000;   [2:13] frequency in Hz, [28] 0 RX / 1 TX, [29] mode
  NA;  the radio name (as on SDR Console's status bar), read once after the port opens
  FA;  MD;  allowed reads, not used

The port is read and written in a thread of its own, about 10 polls a second; results are handed to
the asyncio loop. The port is opened only while the source runs and closed when it stops.
"""
import asyncio
import logging
import re
import threading
import time
import weakref

log = logging.getLogger("winSync.sdrconsole")

# Every command winSync may send to SDR Console, exactly. Enforced by SafePort and by a test.
SAFE_COMMANDS = frozenset({b"FA;", b"IF;", b"MD;", b"NA;"})
POLL = b"IF;"
NAME = b"NA;"
# SDR Console mode digit (MD and IF[29]) -> Thetis mode number (winSync's internal numbering, see
# modes.py), which gives the label shown and the Kiwi / WebSDR mode. 6 is "not used" in SDR Console.
SDR_MODES = {
    0: 2,    # DSB             -> DSB (Kiwi am, WebSDR AM)
    1: 0,    # LSB             -> LSB
    2: 1,    # USB             -> USB
    3: 4,    # CW (upper)      -> CWU (Kiwi cw, WebSDR CW)
    4: 5,    # FM              -> FM (Kiwi nbfm, WebSDR FM)
    5: 10,   # SAM             -> SAM (Kiwi sam, WebSDR AM)
    7: 3,    # CW (lower)      -> CWL (Kiwi cw, WebSDR CW)
    8: 5,    # Wide FM         -> FM (Kiwi nbfm, WebSDR FM)
    9: 5,    # Broadcast FM    -> FM (Kiwi nbfm, WebSDR FM)
}
READ_ONLY = "winSync only reads from SDR Console, so the receiver can't change the radio"
STALL_TIMEOUT = 3.0          # s without an IF answer: SDR Console is not answering
RESEND = 1.0                 # s: an unanswered IF; is sent again after this

_IF_RE = re.compile(r"^IF(\d{11}).{4}[+-].{10}([01])(\d).{7}$")
_NA_RE = re.compile(r"^NA(.+)$")

_running = weakref.WeakSet()      # started SdrConsoleSources, for release_all()


def release_all(timeout=2.0):
    """Stop every SDR Console polling thread and close its port, from any thread. For exits that skip
    the normal shutdown (the server's os._exit when the winSync window goes)."""
    sources = list(_running)
    for src in sources:
        src._stop.set()
    for src in sources:
        if src._thread and src._thread is not threading.current_thread():
            src._thread.join(timeout)


class UnsafeCommand(AssertionError):
    """Raised instead of writing anything that is not in SAFE_COMMANDS (not an assert: -O can't strip it)."""


def check_command(cmd):
    """Raise UnsafeCommand unless cmd is exactly one of SAFE_COMMANDS."""
    if cmd not in SAFE_COMMANDS:
        raise UnsafeCommand(f"refusing to send {cmd!r} to SDR Console")
    return cmd


class SafePort:
    """The only way winSync touches the serial port: writing a SAFE_COMMANDS read, reading, closing."""
    __slots__ = ("_port",)

    def __init__(self, port):
        object.__setattr__(self, "_port", port)

    def write(self, cmd):
        self._port.write(check_command(cmd))

    def read_available(self):
        """Whatever has arrived, waiting at most the port's read timeout for the first byte."""
        return self._port.read(max(1, self._port.in_waiting))

    def close(self):
        self._port.close()

    def __setattr__(self, name, value):
        raise UnsafeCommand(f"winSync never changes the port's {name}")


def split_frames(buf):
    """Split a receive buffer into complete ';'-terminated frames plus the unfinished remainder."""
    *frames, rest = buf.split(";")
    return [f.strip() for f in frames if f.strip()], rest


def parse_frame(frame):
    """('if', (hz, tx, sdr_mode_digit)), ('name', text), or None for anything else (e.g. "?")."""
    m = _IF_RE.match(frame)
    if m:
        return "if", (int(m.group(1)), m.group(2) == "1", int(m.group(3)))
    m = _NA_RE.match(frame)
    if m:
        return "name", m.group(1).strip()
    return None


def describe_open_error(port, err):
    """A plain message for a port that would not open."""
    text = str(err)
    if "FileNotFoundError" in text or "cannot find" in text.lower():
        return (f"{port} does not exist: check the com0com pair for SDR Console (e.g. COM22<->COM23) "
                "and [sdrconsole] port in config.toml")
    if "PermissionError" in text or "access is denied" in text.lower():
        return (f"{port} is in use by another program: is winSync already running, or is SDR Console "
                f"set to {port} instead of the other end of the pair?")
    return f"could not open {port}: {text}"


def no_answer(port):
    return (f"SDR Console does not answer on {port}: is SDR Console running, with the other end of the "
            "com0com pair ticked in Program Options > Controllers > CAT (Serial Port)?")


def serial_open(port, baud):
    """Open the port (a COM name, or a pyserial URL such as socket://host:port for tests)."""
    import serial
    return serial.serial_for_url(port, baudrate=baud, bytesize=8, parity="N", stopbits=1,
                                 timeout=0.05, write_timeout=1.0)


class SdrConsoleSource:
    """Same interface the server uses from cat.CatClient: on_state(freq, mode, tx, vfo, rx2_on),
    on_status(connected), on_note(text or None); start()/stop(), set_follow(); request_tune() and
    _refusal() always refuse (read-only). mode is the Thetis mode number (None for a mode winSync
    doesn't know); vfo is always 0 and rx2_on False."""

    control = "radio"
    cat_to_b = False
    vfo = 0

    def __init__(self, port, baud, on_state, on_status, poll_interval=0.1, on_note=None,
                 opener=serial_open, retry=5.0, stall_timeout=STALL_TIMEOUT):
        self.port, self.baud = port, baud
        self.on_state, self.on_status = on_state, on_status
        self.on_note = on_note or (lambda note: None)
        self.poll_interval, self.retry, self.stall_timeout = poll_interval, retry, stall_timeout
        self._opener = opener
        self.radio_name = None
        self.connected = False
        self.tx = False
        self.note = None
        self._last = None
        self._stop = threading.Event()
        self._thread = None
        self._loop = None

    @property
    def freq_hz(self):
        return self._last[0] if self._last else None

    def start(self):
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sdrconsole", daemon=True)
        self._thread.start()
        _running.add(self)

    async def stop(self):
        self._stop.set()
        if self._thread:
            await asyncio.to_thread(self._thread.join, 3)

    def set_follow(self, follow):
        if follow not in ("auto", "A", "B"):
            raise ValueError("follow must be one of ('auto', 'A', 'B')")
        # SDR Console reports one receiver; there is no VFO to choose.

    def set_control(self, control):
        if control != "radio":
            raise ValueError(READ_ONLY)

    def request_tune(self, freq_hz, mode=None):
        return READ_ONLY

    def _refusal(self, vfo, freq_hz=None, mode=None):
        return READ_ONLY

    # --- polling thread ---------------------------------------------------------------------
    def _post(self, fn, *args):
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:         # loop closed during shutdown
            pass

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    port = SafePort(self._opener(self.port, self.baud))
                except Exception as e:           # serial.SerialException, OSError, bad URL
                    self._post(self._offline, describe_open_error(self.port, e))
                    self._stop.wait(self.retry)
                    continue
                log.info("opened %s for SDR Console", self.port)
                try:
                    self._poll(port)
                except UnsafeCommand:
                    raise
                except Exception as e:           # port vanished (com0com pair removed, USB gone)
                    log.info("SDR Console port %s lost: %s", self.port, e)
                    self._post(self._offline, f"lost {self.port}")
                    self._stop.wait(min(2.0, self.retry))
                finally:
                    try:
                        port.close()
                    except Exception:
                        pass
                    log.info("closed %s", self.port)
        finally:
            self._post(self._offline, None)

    def _write(self, port, cmd):
        try:
            port.write(cmd)
        except UnsafeCommand:
            raise
        except Exception as e:
            # A write timeout (nothing reading the other end of the pair) counts as no answer.
            if type(e).__name__ != "SerialTimeoutException":
                raise

    def _poll(self, port):
        buf = ""
        self._write(port, NAME)
        answered_at = time.monotonic()
        next_send = 0.0
        silent = False
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_send:
                self._write(port, POLL)
                next_send = now + RESEND          # sent again if it goes unanswered
            buf += port.read_available().decode("ascii", errors="replace")
            frames, buf = split_frames(buf)
            buf = buf[-200:]                      # never grow on garbage without a ';'
            for frame in frames:
                parsed = parse_frame(frame)
                if parsed is None:
                    continue                      # "?" and anything else
                kind, value = parsed
                if kind == "name":
                    self._post(self._named, value)
                else:
                    answered_at = time.monotonic()
                    next_send = answered_at + self.poll_interval
                    silent = False
                    self._post(self._reading, value)
            if not silent and time.monotonic() - answered_at > self.stall_timeout:
                silent = True
                self._post(self._offline, no_answer(self.port))

    # --- asyncio loop side ------------------------------------------------------------------
    def _named(self, name):
        if name != self.radio_name:
            self.radio_name = name
            log.info("SDR Console radio: %s", name)

    def _offline(self, why):
        if self.connected:
            self.connected = False
            self.on_status(False)
        if self.tx:                  # state unknown once SDR Console stops answering: don't stay muted
            self.tx = False
            self._emit_last()
        self._set_note(why)

    def _reading(self, value):
        freq, tx, digit = value
        if not self.connected:
            self.connected = True
            log.info("SDR Console answering on %s", self.port)
            self.on_status(True)
        mode = SDR_MODES.get(digit)
        self._set_note(None if mode is not None else
                       f"SDR Console mode {digit} is not one winSync knows, so the receiver is not tuned")
        self.tx = tx
        self._emit((freq, mode, tx, 0, False))

    def _emit_last(self):
        if self._last:
            self._emit(self._last[:2] + (self.tx,) + self._last[3:])

    def _emit(self, state):
        if state != self._last:
            self._last = state
            self.on_state(*state)

    def _set_note(self, note):
        if note != self.note:
            self.note = note
            if note:
                log.warning("%s", note)
            self.on_note(note)
