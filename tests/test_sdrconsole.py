"""SDR Console source: read-only whitelist, IF parsing, mode mapping, TX (Mute on TX), errors, release.
Uses tests.fake_sdrconsole, never a real radio."""
import asyncio
import itertools
import string
import threading
import time

import pytest

from winSync import sdrconsole
from winSync.modes import THETIS_LABELS, thetis_to_kiwi
from winSync.sdrconsole import (READ_ONLY, SAFE_COMMANDS, SDR_MODES, SafePort, SdrConsoleSource,
                                UnsafeCommand, check_command, describe_open_error, no_answer,
                                parse_frame, split_frames)
from tests.fake_sdrconsole import FakeSdrConsole
from tests.test_cat import until

# Replies seen live from SDR Console v3.4 (Adalm Pluto Plus, QO-100) on 2026-10-27
LIVE_IF_RX = "IF104897300000000+0000000000020000000;"
LIVE_IF_TX = "IF104897300000000+0000000000120000000;"


class FakePort:
    """Stands in for an open pyserial port wired to a FakeSdrConsole. Records every write and close."""

    def __init__(self, fake):
        self.fake, self.out, self.buf = fake, b"", ""
        self.writes, self.closed = [], False
        self.lock = threading.Lock()

    @property
    def in_waiting(self):
        return len(self.out)

    def write(self, data):
        with self.lock:
            self.writes.append(bytes(data))
            reply, self.buf = self.fake.feed(data.decode(), self.buf)
            self.out += reply.encode()

    def read(self, n):
        with self.lock:
            data, self.out = self.out[:n], self.out[n:]
        if not data:
            time.sleep(0.01)
        return data

    def close(self):
        self.closed = True


async def started(fake=None, opener=None, **kw):
    fake = fake or FakeSdrConsole()
    ports = []

    def open_fake(port, baud):
        p = FakePort(fake)
        ports.append(p)
        return p
    states, status, notes = [], [], []
    src = SdrConsoleSource("COM23", 57600, lambda *s: states.append(s), status.append,
                           poll_interval=0.02, on_note=notes.append, opener=opener or open_fake,
                           retry=0.05, **kw)
    src.start()
    return fake, src, ports, states, status, notes


def test_whitelist_is_exactly_the_four_reads():
    assert SAFE_COMMANDS == {b"FA;", b"IF;", b"MD;", b"NA;"}
    for cmd in SAFE_COMMANDS:
        assert check_command(cmd) == cmd


@pytest.mark.parametrize("bad", [
    b"TX;", b"RX;", b"TX0;", b"TX1;", b"TX", b"RX", b"tx;", b"FA10489730000;", b"FA00014074000;",
    b"FB;", b"FB10489730000;", b"MD0;", b"MD1;", b"MD2;", b"MD9;", b"AI1;", b"AI;", b"IF", b"if;",
    b" IF;", b"IF; ", b"IF;IF;", b"IF;TX;", b"NA;TX;", b"IF;\r", b"\x00IF;", b"", b";", b"?;",
    b"ZZIF;", b"ZZTX1;", b"PS0;", b"SH;", b"FR1;", b"FT1;", b"RA;",
])
def test_anything_else_is_refused(bad):
    with pytest.raises(UnsafeCommand):
        check_command(bad)
    port = FakePort(FakeSdrConsole())
    with pytest.raises(UnsafeCommand):
        SafePort(port).write(bad)
    assert port.writes == []                       # nothing reached the port


def test_every_other_two_letter_command_is_refused():
    """Every Kenwood-style command name, bare and with a parameter, except the four reads."""
    letters = string.ascii_uppercase
    for a, b in itertools.product(letters, letters):
        name = (a + b).encode()
        for cmd in (name + b";", name + b"0;", name + b"1;", name + b"10489730000;"):
            if cmd in SAFE_COMMANDS:
                continue
            with pytest.raises(UnsafeCommand):
                check_command(cmd)


def test_safe_port_cannot_be_reconfigured():
    port = SafePort(FakePort(FakeSdrConsole()))
    for name in ("baudrate", "write", "_port"):
        with pytest.raises(UnsafeCommand):
            setattr(port, name, None)


def test_parse_live_if_replies():
    assert len(LIVE_IF_RX) == len(LIVE_IF_TX) == 38
    frames, rest = split_frames(LIVE_IF_RX + LIVE_IF_TX + "IF1048")
    assert rest == "IF1048"
    assert parse_frame(frames[0]) == ("if", (10_489_730_000, False, 2))
    assert parse_frame(frames[1]) == ("if", (10_489_730_000, True, 2))
    # The positions the brief gives, in the full string
    assert LIVE_IF_RX[2:13] == "10489730000" and LIVE_IF_RX[28] == "0" and LIVE_IF_RX[29] == "2"
    assert LIVE_IF_TX[28] == "1"
    assert parse_frame("NAADALM-PLUTO") == ("name", "ADALM-PLUTO")
    for other in ("?", "FA10489730000", "MD2", "IF1048973000", "IF10489730000000" + "0" * 30,
                  "AI0", ""):
        assert parse_frame(other) is None
    # The fake answers exactly like the live radio
    fake = FakeSdrConsole()
    assert fake.reply("IF") == LIVE_IF_RX
    fake.set_state(tx=True)
    assert fake.reply("IF") == LIVE_IF_TX
    assert fake.reply("FA") == "FA10489730000;" and fake.reply("MD") == "MD2;"
    assert fake.reply("TX") == "?;" and fake.reply("RX") == "?;"


def test_mode_mapping():
    # SDR Console digit -> (label shown, Kiwi mode); the WebSDR mode follows from the Kiwi mode
    expected = {0: ("DSB", "am"), 1: ("LSB", "lsb"), 2: ("USB", "usb"), 3: ("CWU", "cw"),
                4: ("FM", "nbfm"), 5: ("SAM", "sam"), 7: ("CWL", "cw"), 8: ("FM", "nbfm"),
                9: ("FM", "nbfm")}
    assert set(SDR_MODES) == set(expected) and 6 not in SDR_MODES
    for digit, (label, kiwi) in expected.items():
        assert THETIS_LABELS[SDR_MODES[digit]] == label
        assert thetis_to_kiwi(SDR_MODES[digit]) == kiwi


def test_follows_sdr_console_and_only_reads():
    async def scenario():
        fake, src, ports, states, status, notes = await started()
        await until(lambda: states and states[-1] == (10_489_730_000, 1, False, 0, False), "first")
        assert status == [True]
        await until(lambda: src.radio_name == "ADALM-PLUTO", "name")
        fake.set_state(freq=10_489_800_000, mode=1)
        await until(lambda: states[-1] == (10_489_800_000, 0, False, 0, False), "LSB")
        fake.set_state(mode=3)
        await until(lambda: states[-1][1] == 4, "CW")
        fake.set_state(tx=True)
        await until(lambda: states[-1][2] is True, "TX")
        assert src.freq_hz == 10_489_800_000           # receive frequency, also during TX
        fake.set_state(tx=False)
        await until(lambda: states[-1][2] is False, "RX")
        assert src.request_tune(10_489_750_000, 1) == READ_ONLY
        assert src._refusal(0, 10_489_750_000) == READ_ONLY
        with pytest.raises(ValueError):
            src.set_control("both")
        src.set_control("radio")
        src.set_follow("B")                            # accepted, nothing to choose
        await src.stop()
        sent = set(fake.received)
        assert sent == {"NA;", "IF;"} and fake.received[0] == "NA;"
        assert all(w in SAFE_COMMANDS for w in ports[0].writes)
        assert ports[0].closed
        assert not notes or notes[-1] is None

    asyncio.run(scenario())


def test_over_a_real_pyserial_port():
    """The real opener (pyserial serial_for_url), with the fake on a TCP socket instead of COM23."""
    async def scenario():
        fake = FakeSdrConsole()
        await fake.start()
        states, status = [], []
        src = SdrConsoleSource(fake.url, 57600, lambda *s: states.append(s), status.append,
                               poll_interval=0.02, retry=0.05)
        src.start()
        await until(lambda: states and states[-1] == (10_489_730_000, 1, False, 0, False), "first")
        fake.set_state(tx=True)
        await until(lambda: states[-1][2] is True, "TX")
        await src.stop()
        await until(lambda: not fake.writers, "port closed")
        assert set(fake.received) == {"NA;", "IF;"}
        await fake.stop()

    asyncio.run(scenario())


def test_not_answering_unmutes_and_says_why():
    async def scenario():
        fake, src, _, states, status, notes = await started(stall_timeout=0.3)
        fake.set_state(tx=True)
        await until(lambda: states and states[-1][2] is True, "TX")
        fake.answer = False                            # e.g. COM22 not ticked in SDR Console
        await until(lambda: status == [True, False], "offline")
        assert states[-1][2] is False                  # don't stay muted
        assert notes[-1] == no_answer("COM23") and "ticked" in notes[-1]
        fake.answer = True
        await until(lambda: status == [True, False, True], "back")
        await until(lambda: states[-1][2] is True and notes[-1] is None, "TX again")
        await src.stop()

    asyncio.run(scenario())


def test_link_drop_unmutes_and_reopens():
    async def scenario():
        fake = FakeSdrConsole()
        await fake.start()
        states, status, notes = [], [], []
        src = SdrConsoleSource(fake.url, 57600, lambda *s: states.append(s), status.append,
                               poll_interval=0.02, on_note=notes.append, retry=0.05)
        src.start()
        fake.set_state(tx=True)
        await until(lambda: states and states[-1][2] is True, "TX")
        port = fake.port
        await fake.stop()                              # the port goes away mid-transmission
        await until(lambda: status[-1] is False and states[-1][2] is False, "dropped, unmuted")
        await until(lambda: notes and notes[-1] and "could not open" in notes[-1], "reopen fails")
        fake2 = FakeSdrConsole()
        await fake2.start(port)
        await until(lambda: status[-1] is True, "reopened")
        await src.stop()
        await fake2.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("text, expect", [
    ("could not open port 'COM23': FileNotFoundError(2, 'The system cannot find the file "
     "specified.', None, 2)", "COM23 does not exist"),
    ("could not open port 'COM23': PermissionError(13, 'Access is denied.', None, 5)",
     "COM23 is in use by another program"),
])
def test_port_missing_or_in_use(text, expect):
    import serial

    def refuse(port, baud):
        raise serial.SerialException(text)

    assert describe_open_error("COM23", serial.SerialException(text)).startswith(expect)

    async def scenario():
        _, src, _, states, status, notes = await started(opener=refuse)
        await until(lambda: notes and notes[-1], "note")
        assert notes[-1].startswith(expect) and status == [] and states == []
        await src.stop()

    asyncio.run(scenario())


def test_unknown_mode_still_reports_tx():
    async def scenario():
        fake, src, _, states, status, notes = await started(FakeSdrConsole(mode=6))
        await until(lambda: states and states[-1] == (10_489_730_000, None, False, 0, False), "first")
        assert notes[-1] == "SDR Console mode 6 is not one winSync knows, so the receiver is not tuned"
        fake.set_state(tx=True)
        await until(lambda: states[-1][2] is True, "TX")   # Mute on TX still works
        fake.set_state(mode=2)
        await until(lambda: states[-1][1] == 1 and notes[-1] is None, "USB")
        await src.stop()

    asyncio.run(scenario())


def test_release_all_closes_the_port():
    """The window stops the server with os._exit, which skips the normal shutdown; release_all()
    must still stop polling and close COM23."""
    async def scenario():
        _, src, ports, states, status, _ = await started()
        await until(lambda: states and status == [True], "online")
        await asyncio.to_thread(sdrconsole.release_all)
        assert not src._thread.is_alive()
        assert len(ports) == 1 and ports[0].closed

    asyncio.run(scenario())


def test_server_exit_releases_sdrconsole(monkeypatch):
    """exit_when_parent_goes(): stdin closed -> both read-only sources released before os._exit."""
    import io
    import sys

    from winSync import __main__ as main
    from winSync import omnirig
    calls = []
    monkeypatch.setattr(omnirig, "release_all", lambda: calls.append("omnirig"))
    monkeypatch.setattr(sdrconsole, "release_all", lambda: calls.append("sdrconsole"))
    monkeypatch.setattr(main.os, "_exit", lambda code: calls.append(("exit", code)))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    main.exit_when_parent_goes()
    for _ in range(100):
        if len(calls) == 3:
            break
        threading.Event().wait(0.02)
    assert sorted(calls[:2]) == ["omnirig", "sdrconsole"] and calls[2] == ("exit", 0)
