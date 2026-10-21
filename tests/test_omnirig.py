"""OmniRig source: read-only, mapping, follow, TX, status. Uses a fake OmniRig COM object."""
import asyncio
import threading

import pytest

from winSync import omnirig
from winSync.omnirig import (OMNIRIG_MODES, PM_RX, PM_TX, READ_ONLY, READ_PROPS, OmniRigSource,
                             OmniRigUnavailable, ReadOnlyRig, UnsafeAccess, to_state)
from tests.test_cat import until

SSB_U, SSB_L, CW_U, CW_L, DIG_U, AM, FM = (0x02000000, 0x04000000, 0x00800000, 0x01000000,
                                           0x08000000, 0x20000000, 0x40000000)


class FakeRig:
    """Stands in for OmniRig's Rig object. Records every property read, every property write and
    every method call (OmniRig's methods: SendCustomCommand, SetSimplexMode, SetSplitMode, ...)."""

    def __init__(self, **values):
        d = {"Status": 4, "StatusStr": "On-line", "RigType": "IC-7300",
             "ReadableParams": PM_RX | PM_TX | 0xFFFFF, "Freq": 14_074_000, "FreqA": 14_074_000,
             "FreqB": 7_074_000, "Vfo": 0x80, "Mode": SSB_U, "Tx": PM_RX}
        d.update(values)
        object.__setattr__(self, "values", d)
        object.__setattr__(self, "reads", [])
        object.__setattr__(self, "writes", [])
        object.__setattr__(self, "calls", [])
        object.__setattr__(self, "lock", threading.Lock())

    def __getattr__(self, name):
        with self.lock:
            if name in self.values:
                self.reads.append(name)
                return self.values[name]

        def method(*args):
            self.calls.append((name, args))
        return method

    def __setattr__(self, name, value):
        self.writes.append((name, value))

    def set(self, **values):
        with self.lock:
            self.values.update(values)


class FakeOmniRig:
    def __init__(self, rig1=None, rig2=None):
        self.Rig1, self.Rig2 = rig1 or FakeRig(), rig2 or FakeRig(RigType="FT-991")


async def started(fake=None, rig_no=1, dispatch=None, **kw):
    fake = fake or FakeOmniRig()
    states, status, notes = [], [], []
    src = OmniRigSource(rig_no, lambda *s: states.append(s), status.append, poll_interval=0.02,
                        on_note=notes.append, dispatch=dispatch or (lambda: fake),
                        thread_init=lambda: None, retry=0.05, **kw)
    src.start()
    return fake, src, states, status, notes


def test_read_only_wrapper():
    rig = FakeRig()
    ro = ReadOnlyRig(rig)
    assert ro.get("Freq") == 14_074_000
    for name in ("SendCustomCommand", "SetSimplexMode", "SetSplitMode", "FrequencyOfTone",
                 "ClearRit", "Pitch", "RitOffset", "Split", "Rit", "Xit", "WriteableParams"):
        with pytest.raises(UnsafeAccess):
            ro.get(name)
    for name in ("Freq", "FreqA", "Mode", "Tx", "Vfo"):
        with pytest.raises(UnsafeAccess):
            setattr(ro, name, 1)
    with pytest.raises(UnsafeAccess):
        ro.Freq                                       # not even a plain attribute read
    assert rig.writes == [] and rig.calls == []
    assert READ_PROPS == {"Status", "StatusStr", "RigType", "ReadableParams", "Freq", "FreqA",
                          "FreqB", "Vfo", "Mode", "Tx"}


def test_mode_and_vfo_mapping():
    assert OMNIRIG_MODES == {SSB_L: 0, SSB_U: 1, CW_L: 3, CW_U: 4, FM: 5, AM: 6, DIG_U: 7,
                             0x10000000: 9}
    v = {"Freq": 7_030_000, "FreqA": 7_030_000, "FreqB": 3_700_000, "Vfo": 0x80, "Mode": CW_U,
         "Tx": PM_RX}
    assert to_state(v, "auto") == (7_030_000, 4, False, 0)
    assert to_state(v, "B") == (3_700_000, 4, False, 1)
    for b_flag in (0x200, 0x400, 0x1000):             # VFOBA / VFOBB (either order) / VFOB
        assert to_state(dict(v, Vfo=b_flag, Freq=3_700_000), "auto") == (3_700_000, 4, False, 1)
    assert to_state(dict(v, Vfo=0x100), "auto")[3] == 0          # VFOAB: receiving on A
    assert to_state(dict(v, Tx=PM_TX), "auto")[2] is True
    assert to_state(dict(v, Mode=0), "auto")[1] is None          # unknown mode
    assert to_state(dict(v, FreqA=0), "A") == (7_030_000, 4, False, 0)   # no FreqA: current freq
    assert to_state(dict(v, FreqB=0), "B") is None                       # B not readable, A active
    assert to_state(dict(v, Freq=0, FreqA=0, FreqB=0), "auto") is None


def test_follows_rig_and_never_writes_or_calls():
    async def scenario():
        fake, src, states, status, notes = await started()
        await until(lambda: states and states[-1] == (14_074_000, 1, False, 0, False), "first")
        assert status == [True]
        fake.Rig1.set(Freq=7_074_000, FreqA=7_074_000, Mode=SSB_L)
        await until(lambda: states[-1] == (7_074_000, 0, False, 0, False), "LSB")
        fake.Rig1.set(Tx=PM_TX)
        await until(lambda: states[-1][2] is True, "TX")
        fake.Rig1.set(Tx=PM_RX)
        await until(lambda: states[-1][2] is False, "RX")
        fake.Rig1.set(Vfo=0x1000, Freq=7_074_000)    # switched to VFO B
        await until(lambda: states[-1][3] == 1, "VFO B")
        src.set_follow("A")
        await until(lambda: states[-1] == (7_074_000, 0, False, 0, False), "follow A")
        await src.stop()
        rig = fake.Rig1
        assert rig.writes == [] and rig.calls == []
        assert set(rig.reads) <= READ_PROPS
        assert fake.Rig2.reads == []                  # Rig 2 untouched
        assert notes == [] or notes[-1] is None

    asyncio.run(scenario())


def test_rig_2_and_read_only_requests():
    async def scenario():
        fake, src, states, status, _ = await started(rig_no=2)
        await until(lambda: states, "first")
        assert fake.Rig1.reads == [] and fake.Rig2.reads
        assert src.request_tune(7_074_000, 1) == READ_ONLY
        assert src._refusal(0, 7_074_000) == READ_ONLY
        with pytest.raises(ValueError):
            src.set_control("both")
        src.set_control("radio")
        await src.stop()
        assert fake.Rig2.writes == [] and fake.Rig2.calls == []

    asyncio.run(scenario())


def test_offline_not_responding_and_back():
    async def scenario():
        fake, src, states, status, notes = await started()
        await until(lambda: states, "first")
        fake.Rig1.set(Tx=PM_TX)
        await until(lambda: states[-1][2] is True, "TX")
        fake.Rig1.set(Status=3, StatusStr="Rig is not responding")
        await until(lambda: status[-1] is False, "offline")
        assert states[-1][2] is False                 # TX cleared when the rig goes away
        assert notes[-1] == "OmniRig Rig 1: Rig is not responding"
        fake.Rig1.set(Status=4, Tx=PM_RX)
        await until(lambda: status[-1] is True and notes[-1] is None, "back")
        await src.stop()

    asyncio.run(scenario())


def test_not_installed_then_installed():
    async def scenario():
        fake = FakeOmniRig()
        installed = threading.Event()

        def dispatch():
            if not installed.is_set():
                raise OmniRigUnavailable(omnirig.NOT_INSTALLED)
            return fake
        _, src, states, status, notes = await started(dispatch=dispatch)
        await until(lambda: notes and notes[-1] == omnirig.NOT_INSTALLED, "not installed")
        assert status == [] and states == []
        installed.set()
        await until(lambda: states and status == [True], "connected")
        await src.stop()

    asyncio.run(scenario())


def test_com_error_drops_and_reconnects():
    async def scenario():
        class DyingRig(FakeRig):
            def __getattr__(self, name):
                if self.values.get("dead"):
                    raise OSError("The RPC server is unavailable")   # OmniRig closed
                return super().__getattr__(name)
        fake = FakeOmniRig(rig1=DyingRig())
        conns = []

        def dispatch():
            conns.append(1)
            return fake
        _, src, states, status, notes = await started(dispatch=dispatch)
        await until(lambda: states, "first")
        fake.Rig1.set(dead=True)
        await until(lambda: status[-1] is False, "offline")
        assert notes[-1] == "lost the connection to OmniRig"
        fake.Rig1.set(dead=False)
        await until(lambda: status[-1] is True and len(conns) >= 2, "reconnected")
        await src.stop()

    asyncio.run(scenario())


def test_keeps_omnirig_object_while_polling():
    """Real OmniRig disconnects Rig1/Rig2 once no client holds the OmniRigX object (found live)."""
    class DyingRig(FakeRig):
        def __getattr__(self, name):
            if self.values.get("dead"):
                raise OSError("The object invoked has disconnected from its clients.")
            return super().__getattr__(name)

    class OwningOmniRig(FakeOmniRig):
        def __del__(self):
            self.Rig1.set(dead=True)

    async def scenario():
        _, src, states, status, notes = await started(dispatch=lambda: OwningOmniRig(rig1=DyingRig()))
        await until(lambda: len(states) >= 1 and status == [True], "online")
        await asyncio.sleep(0.2)
        assert status == [True] and notes in ([], [None])      # never dropped
        await src.stop()

    asyncio.run(scenario())


def test_rig_without_tx_reporting_says_so():
    async def scenario():
        fake = FakeOmniRig(rig1=FakeRig(ReadableParams=0xFFFF, RigType="FT-817"))
        _, src, states, _, notes = await started(fake)
        await until(lambda: notes and notes[-1] and "does not report TX" in notes[-1], "note")
        assert "FT-817" in notes[-1]
        await until(lambda: states, "still tunes the Kiwi")
        await src.stop()

    asyncio.run(scenario())
