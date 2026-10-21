"""Read-only OmniRig source: the frequency, mode and TX state of a conventional radio, through
OmniRig (Afreet / VE3NEA, https://www.dxatlas.com/OmniRig/), Rig 1 or Rig 2. An alternative to
Thetis's TCP/IP CAT (cat.py) for radios that OmniRig supports.

Read-only: winSync only ever READS the properties in READ_PROPS of OmniRig's Rig object, through
ReadOnlyRig. It never sets a property (Freq, Mode, Tx, ...) or calls a method (SendCustomCommand,
SetSimplexMode, ...), so it never asks OmniRig to change the radio, and two-way sync is not
available with OmniRig. OmniRig itself talks to the radio with the commands in its rig file,
including any start-up commands that file contains; that is OmniRig's traffic, not winSync's.

COM runs in a thread of its own (initialised there) that polls the rig about 10 times a second;
OmniRig answers from its own copy of the rig's state, which it keeps up to date by polling the
radio. Results are handed to the asyncio loop.

Values checked against the type library in OmniRig.exe 1.20 (RigParamX, RigStatusX, IRigX):
ProgID OmniRig.OmniRigX with Rig1/Rig2; mode flags PM_SSB_L 0x04000000, PM_SSB_U 0x02000000,
PM_CW_U 0x00800000, PM_CW_L 0x01000000, PM_DIG_L 0x10000000, PM_DIG_U 0x08000000, PM_AM 0x20000000,
PM_FM 0x40000000; PM_RX 0x00200000, PM_TX 0x00400000; PM_VFOAA 0x80, PM_VFOAB 0x100, PM_VFOBA 0x200,
PM_VFOBB 0x400, PM_VFOA 0x800, PM_VFOB 0x1000; ST_ONLINE = 4.
"""
import asyncio
import logging
import threading
import weakref

log = logging.getLogger("winSync.omnirig")

PROG_ID = "OmniRig.OmniRigX"
# The only Rig properties winSync reads. Nothing is ever written.
READ_PROPS = frozenset({"Status", "StatusStr", "RigType", "ReadableParams",
                        "Freq", "FreqA", "FreqB", "Vfo", "Mode", "Tx"})
ST_ONLINE = 4
PM_RX, PM_TX = 0x00200000, 0x00400000
# OmniRig mode flag -> Thetis mode number (winSync's internal numbering; see modes.py)
OMNIRIG_MODES = {
    0x04000000: 0,   # SSB_L -> LSB
    0x02000000: 1,   # SSB_U -> USB
    0x01000000: 3,   # CW_L  -> CWL
    0x00800000: 4,   # CW_U  -> CWU
    0x40000000: 5,   # FM
    0x20000000: 6,   # AM
    0x08000000: 7,   # DIG_U -> DIGU
    0x10000000: 9,   # DIG_L -> DIGL
}
# Vfo flags whose receive VFO is B (OmniRig names them RX VFO first): VFOBA, VFOBB and VFOB.
# Everything else (VFOAA, VFOAB, VFOA, unknown) counts as VFO A.
VFO_B_FLAGS = frozenset({0x200, 0x400, 0x1000})
CLASS_NOT_REGISTERED = (-2147221005, -2147221164)     # CO_E_CLASSSTRING, REGDB_E_CLASSNOTREG
READ_ONLY = "winSync only reads from OmniRig, so the Kiwi can't change the radio"
NOT_INSTALLED = "OmniRig is not installed (https://www.dxatlas.com/OmniRig/)"


_running = weakref.WeakSet()      # started OmniRigSources, for release_all()


def release_all(timeout=2.0):
    """Stop every OmniRig polling thread and let go of OmniRig, from any thread. For exits that skip
    the normal shutdown (the server's os._exit when the winSync window goes): a client that dies still
    holding OmniRig leaves OmniRig running and polling the radio."""
    sources = list(_running)
    for src in sources:
        src._stop.set()
    for src in sources:
        if src._thread and src._thread is not threading.current_thread():
            src._thread.join(timeout)


class UnsafeAccess(AssertionError):
    """Raised instead of touching anything on the Rig object other than reading READ_PROPS."""


class ReadOnlyRig:
    """The only way winSync touches an OmniRig Rig object: reading the properties in READ_PROPS."""
    __slots__ = ("_rig",)

    def __init__(self, rig):
        object.__setattr__(self, "_rig", rig)

    def get(self, name):
        if name not in READ_PROPS:
            raise UnsafeAccess(f"winSync does not read OmniRig's {name}")
        return getattr(self._rig, name)

    def __setattr__(self, name, value):
        raise UnsafeAccess(f"winSync never sets OmniRig's {name}")

    def __getattr__(self, name):
        raise UnsafeAccess(f"use get(): winSync only reads {sorted(READ_PROPS)}")


class OmniRigUnavailable(Exception):
    """OmniRig could not be reached (not installed, or failed to start)."""


def com_dispatch():
    """Connect to OmniRig over COM (pywin32). Must run in the polling thread."""
    try:
        import pywintypes
        import win32com.client
    except ImportError:
        raise OmniRigUnavailable("pywin32 is missing: run winSync.cmd --setup") from None
    try:
        return win32com.client.Dispatch(PROG_ID)
    except pywintypes.com_error as e:
        if e.hresult in CLASS_NOT_REGISTERED:
            raise OmniRigUnavailable(NOT_INSTALLED) from None
        raise OmniRigUnavailable(f"OmniRig could not be started: {e.strerror}") from None


def com_thread_init():
    try:
        import pythoncom
    except ImportError:
        return None
    pythoncom.CoInitialize()
    return pythoncom


def to_state(values, follow):
    """(freq_hz, mode, tx, vfo) from one reading, or None if there is no frequency yet.
    values: dict of the READ_PROPS read; follow: 'auto', 'A' or 'B'."""
    vfo_flag = values.get("Vfo") or 0
    active = 1 if vfo_flag in VFO_B_FLAGS else 0
    freq = values.get("Freq") or 0
    fa, fb = values.get("FreqA") or 0, values.get("FreqB") or 0
    if follow == "A":
        vfo, freq = 0, fa or (freq if active == 0 else 0)
    elif follow == "B":
        vfo, freq = 1, fb or (freq if active == 1 else 0)
    else:
        vfo = active
        freq = freq or (fb if active else fa)
    if not freq:
        return None
    mode = OMNIRIG_MODES.get(values.get("Mode") or 0)
    tx = values.get("Tx") == PM_TX
    return int(freq), mode, tx, vfo


class OmniRigSource:
    """Same interface the server uses from cat.CatClient: on_state(freq, mode, tx, vfo, rx2_on),
    on_status(connected), on_note(text or None); start()/stop(), set_follow(); request_tune() and
    _refusal() always refuse (read-only). rx2_on is always False."""

    control = "radio"
    cat_to_b = False

    def __init__(self, rig_no, on_state, on_status, poll_interval=0.1, follow="auto", on_note=None,
                 dispatch=com_dispatch, thread_init=com_thread_init, retry=5.0):
        self.rig_no = rig_no if rig_no in (1, 2) else 1
        self.on_state, self.on_status = on_state, on_status
        self.on_note = on_note or (lambda note: None)
        self.poll_interval, self.retry = poll_interval, retry
        self.follow = follow if follow in ("auto", "A", "B") else "auto"
        self._dispatch, self._thread_init = dispatch, thread_init
        self.values = None           # latest reading, None while offline
        self.connected = False
        self.tx = False
        self.vfo = 0
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
        self._thread = threading.Thread(target=self._run, name="omnirig", daemon=True)
        self._thread.start()
        _running.add(self)

    async def stop(self):
        self._stop.set()
        if self._thread:
            await asyncio.to_thread(self._thread.join, 3)

    def set_follow(self, follow):
        if follow not in ("auto", "A", "B"):
            raise ValueError("follow must be one of ('auto', 'A', 'B')")
        self.follow = follow
        self._update()

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
        com = self._thread_init()
        omni = rig = None
        try:
            while not self._stop.is_set():
                try:
                    # Keep the OmniRigX object referenced while polling: once the last client
                    # reference to it goes, OmniRig disconnects its Rig objects (RPC_E_DISCONNECTED).
                    omni = self._dispatch()
                    rig = ReadOnlyRig(getattr(omni, f"Rig{self.rig_no}"))
                except OmniRigUnavailable as e:
                    self._post(self._offline, str(e))
                    self._stop.wait(self.retry)
                    continue
                log.info("connected to OmniRig, Rig %d", self.rig_no)
                try:
                    self._poll(rig, com)
                except UnsafeAccess:
                    raise
                except Exception as e:           # COM error: OmniRig closed or crashed
                    log.info("OmniRig connection lost: %s", e)
                    self._post(self._offline, "lost the connection to OmniRig")
                    omni = rig = None
                    self._stop.wait(min(2.0, self.retry))
        finally:
            omni = rig = None            # release OmniRig before leaving COM, so it can close
            if com is not None:
                com.CoUninitialize()
            log.info("released OmniRig")

    def _poll(self, rig, com):
        while not self._stop.is_set():
            values = {name: rig.get(name) for name in ("Status", "StatusStr")}
            if values["Status"] == ST_ONLINE:
                for name in ("RigType", "ReadableParams", "Freq", "FreqA", "FreqB", "Vfo",
                             "Mode", "Tx"):
                    values[name] = rig.get(name)
                self._post(self._reading, values)
            else:
                self._post(self._offline, f"OmniRig Rig {self.rig_no}: {values['StatusStr']}")
            if com is not None:
                com.PumpWaitingMessages()
            self._stop.wait(self.poll_interval)

    # --- asyncio loop side ------------------------------------------------------------------
    def _offline(self, why):
        self.values = None
        if self.connected:
            self.connected = False
            self.on_status(False)
        if self.tx:                  # state unknown: don't stay muted
            self.tx = False
            self._emit_last()
        self._set_note(why)

    def _reading(self, values):
        self.values = values
        if not self.connected:
            self.connected = True
            log.info("OmniRig Rig %d online: %s", self.rig_no, values.get("RigType"))
            self.on_status(True)
        readable = values.get("ReadableParams") or 0
        if not readable & (PM_TX | PM_RX):
            self._set_note(f"OmniRig's file for the {values.get('RigType') or 'radio'} does not "
                           "report TX, so Mute on TX can't work")
        elif values.get("Mode") is not None and values.get("Mode") not in OMNIRIG_MODES:
            self._set_note("OmniRig does not report a mode winSync knows for this radio, so the "
                           "Kiwi is not tuned")
        else:
            self._set_note(None)
        self._update()

    def _update(self):
        if self.values is None:
            return
        state = to_state(self.values, self.follow)
        if state is None:
            return
        freq, mode, tx, vfo = state
        self.tx, self.vfo = tx, vfo
        if mode is None:
            return
        self._emit((freq, mode, tx, vfo, False))

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
