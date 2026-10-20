import asyncio
import re

import pytest

from winSync.bands import band_for
from winSync.cat import POLL_BATCH, SAFE_COMMANDS, CatClient, parse_frame, split_frames
from winSync.modes import thetis_to_kiwi
from tests.fake_thetis import BANNER, FakeThetis


# Real replies from Thetis v2.10.3.15 (MI0BOT HL2), captured by the probe on 2026-10-19
REAL_ZZIF_RX = "ZZIF000037500000011+00000000000000000000"


def test_split_frames_partial_and_multiple():
    frames, rest = split_frames(BANNER + "ZZFB00003749400;ZZME01;ZZRS")
    assert frames == [BANNER[:-1], "ZZFB00003749400", "ZZME01"]
    assert rest == "ZZRS"


def test_parse_real_thetis_replies():
    assert parse_frame(REAL_ZZIF_RX) == ("if", (3_750_000, False, 0))       # VFO A 3.750 LSB, RX
    assert parse_frame("ZZFB00003749400") == ("freq_b", 3_749_400)
    assert parse_frame("ZZME01") == ("mode_b", 1)                           # USB
    assert parse_frame("ZZRS1") == ("rx2", True) and parse_frame("ZZRS0") == ("rx2", False)
    assert parse_frame("ZZXV0016") == ("xv", 16)
    # TX flag and two-digit mode in ZZIF
    assert parse_frame("ZZIF00007030000" "0011" "-00050" "1" "0" "000" "1" "04" "0000000") == \
        ("if", (7_030_000, True, 4))
    for other in (BANNER[:-1], "?", "IF000037500000011+0000000000010000000", "FA00003750000",
                  "ZZMD00", "ZZIF001"):
        assert parse_frame(other) is None


def test_bands_and_modes():
    assert band_for(3_750_000) == "80m" and band_for(1_000_000) == "GEN"
    assert [thetis_to_kiwi(m) for m in range(12)] == \
        ["lsb", "usb", "am", "cw", "cw", "nbfm", "am", "usb", None, "lsb", "sam", "am"]
    assert thetis_to_kiwi(7, {"7": "usn"}) == "usn"
    assert thetis_to_kiwi(1, {"1": "bogus"}) is None     # invalid Kiwi mode is rejected


def test_whitelist_is_reads_only():
    """Only the reads checked in Thetis's source; never a command with a parameter."""
    assert SAFE_COMMANDS == {b"ZZIF;", b"ZZFB;", b"ZZME;", b"ZZRS;", b"ZZXV;"}
    assert set(re.findall(rb"[^;]*;", POLL_BATCH)) <= SAFE_COMMANDS


async def until(pred, what="condition"):
    for _ in range(200):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timeout waiting for {what}")


async def started(fake=None, **kw):
    fake = fake or FakeThetis()
    await fake.start()
    states, status = [], []
    client = CatClient("127.0.0.1", fake.port, lambda *s: states.append(s), status.append,
                       poll_interval=0.02, **kw)
    client.start()
    return fake, client, states, status


def test_refuses_anything_off_the_whitelist():
    client = CatClient("127.0.0.1", 1, None, None)
    client._writer = type("W", (), {"write": lambda self, b: None})()
    for bad in (b"ZZTX1;", b"TX;", b"ZZIF;ZZTU1;", b"ZZIF", b"ZZIF;x", b"ZZFA00007000000;"):
        with pytest.raises(AssertionError):
            client._send(bad)


def test_initial_state_and_polled_changes():
    async def scenario():
        fake, client, states, status = await started()
        await until(lambda: states and states[-1] == (14_200_000, 1, False, 0, False), "first state")
        assert status[0] is True
        fake.set_state(freq_a=7_074_000, mode_a=7)
        await until(lambda: states[-1] == (7_074_000, 7, False, 0, False), "VFO A change")
        await client.stop()
        await fake.stop()

    asyncio.run(scenario())


def test_only_safe_commands_are_sent():
    """TX;/ZZTX1;/ZZTU1; key the transmitter. winSync must only ever send the five reads."""
    async def scenario():
        fake, client, states, _ = await started()
        await asyncio.sleep(0.5)
        await client.stop()
        await fake.stop()
        assert fake.received and set(fake.received) <= {"ZZIF", "ZZFB", "ZZME", "ZZRS", "ZZXV"}

    asyncio.run(scenario())


def test_tx_from_mox_or_tune_and_cleared_on_disconnect():
    async def scenario():
        fake, client, states, status = await started()
        await until(lambda: states and states[-1][2] is False)
        fake.set_state(mox=True)
        await until(lambda: states[-1][2] is True, "MOX")
        fake.set_state(mox=False)
        await until(lambda: states[-1][2] is False, "MOX off")
        fake.set_state(tun=True)                                  # TUNE is TX too (ZZXV bit 7)
        await until(lambda: states[-1][2] is True, "TUNE")
        await fake.stop()           # link lost while transmitting -> TX must be reported as cleared
        await until(lambda: states[-1][2] is False and False in status, "TX cleared")
        await client.stop()

    asyncio.run(scenario())


def test_auto_follows_last_tuned_vfo_only_with_rx2_on():
    async def scenario():
        fake, client, states, _ = await started()
        await until(lambda: states and states[-1] == (14_200_000, 1, False, 0, False))

        # RX2 off: VFO B changes are ignored
        fake.set_state(freq_b=7_030_000)
        await until(lambda: client.freq_b == 7_030_000)
        await asyncio.sleep(0.1)
        assert states[-1] == (14_200_000, 1, False, 0, False)

        # RX2 on: still VFO A until VFO B is tuned
        fake.set_state(rx2=True)
        await until(lambda: states[-1] == (14_200_000, 1, False, 0, True), "RX2 on")
        fake.set_state(freq_b=7_031_000)
        await until(lambda: states[-1] == (7_031_000, 0, False, 1, True), "follow VFO B")
        fake.set_state(mode_b=4)                                   # a mode change counts too
        await until(lambda: states[-1] == (7_031_000, 4, False, 1, True), "VFO B mode")

        # Tuning VFO A brings it back
        fake.set_state(freq_a=14_201_000)
        await until(lambda: states[-1] == (14_201_000, 1, False, 0, True), "back to VFO A")

        # Both moving at once (VFO sync) keeps the current VFO
        fake.set_state(freq_a=14_202_000, freq_b=14_202_000)
        await until(lambda: states[-1][0] == 14_202_000)
        await asyncio.sleep(0.1)
        assert states[-1][3] == 0

        # RX2 switched off while following B: back to A
        fake.set_state(freq_b=7_040_000)
        await until(lambda: states[-1][3] == 1)
        fake.set_state(rx2=False)
        await until(lambda: states[-1] == (14_202_000, 1, False, 0, False), "RX2 off")
        await client.stop()
        await fake.stop()

    asyncio.run(scenario())


def test_fixed_follow_and_switching_at_runtime():
    async def scenario():
        fake, client, states, _ = await started(follow="B")
        await until(lambda: states and states[-1] == (7_100_000, 0, False, 1, False), "VFO B")
        fake.set_state(freq_a=14_100_000)                  # VFO A changes don't matter
        await asyncio.sleep(0.15)
        assert states[-1][3] == 1
        client.set_follow("A")
        assert states[-1] == (14_100_000, 1, False, 0, False)
        client.set_follow("auto")
        with pytest.raises(ValueError):
            client.set_follow("C")
        await client.stop()
        await fake.stop()

    asyncio.run(scenario())


def test_reconnects_after_drop_and_after_thetis_stops_answering():
    async def scenario():
        fake, client, states, status = await started()
        client.stall_timeout = 0.3
        await until(lambda: states)
        fake.answer = False                                 # Thetis hangs: no replies at all
        await until(lambda: False in status, "stall detected")
        fake.answer = True
        await until(lambda: status.count(True) >= 2, "reconnected")

        port = fake.port
        await fake.stop()
        fake2 = FakeThetis(port=port, freq=3_573_000, mode=1)
        await fake2.start()
        await until(lambda: states[-1][0] == 3_573_000, "state from the new Thetis")
        await client.stop()
        await fake2.stop()

    asyncio.run(scenario())
