"""Two-way sync (Kiwi -> radio): the only set commands that can ever reach Thetis, and when."""
import asyncio
import re
import time

import pytest

from winSync import cat
from winSync.bands import uk_band_for
from winSync.cat import CAT_TO_B_NOTE, SAFE_COMMANDS, CatClient, UnsafeCommand
from winSync.modes import kiwi_to_thetis
from tests.fake_thetis import FakeThetis
from tests.test_cat import until

# Every CAT command Thetis v2.10.3.15 (MI0BOT) knows: the codes in its CAT/CATStructs.xml, which
# are also every command CATParser.cs dispatches.
THETIS_CAT_CODES = """
    AC AG AI AL AM AN AR AS BC BD BP BU BY CA CG CH CI CM CN CT DC DN DQ EX FA FB FC FD FR FS FT FW 
    GT ID IF IS KS KY LK LM LT MC MD MF MG ML MO MR MU MW NB NL NR NT OF OI OS PA PB PC PI PK PL PM 
    PR PS QC QI QR RA RC RD RG RL RM RT RU RX SA SB SC SD SH SI SL SM SQ SR SS ST SU SV TC TD TI TN 
    TO TS TX TY UL UP VD VG VR VX XT ZZAA ZZAB ZZAC ZZAD ZZAE ZZAF ZZAG ZZAI ZZAP ZZAR ZZAS ZZAT 
    ZZAU ZZAY ZZBA ZZBB ZZBD ZZBE ZZBF ZZBG ZZBI ZZBM ZZBP ZZBR ZZBS ZZBT ZZBU ZZBY ZZCB ZZCD ZZCF 
    ZZCI ZZCL ZZCM ZZCN ZZCO ZZCP ZZCS ZZCT ZZCU ZZDA ZZDB ZZDC ZZDD ZZDE ZZDF ZZDG ZZDH ZZDM ZZDN 
    ZZDO ZZDP ZZDQ ZZDR ZZDU ZZDX ZZDY ZZEA ZZEB ZZEM ZZER ZZET ZZFA ZZFB ZZFD ZZFH ZZFI ZZFJ ZZFL 
    ZZFM ZZFR ZZFS ZZFT ZZFV ZZFW ZZFX ZZFY ZZGA ZZGE ZZGL ZZGR ZZGT ZZGU ZZHA ZZHR ZZHT ZZHU ZZHV 
    ZZHW ZZHX ZZID ZZIF ZZIO ZZIS ZZIT ZZIU ZZIV ZZJP ZZJQ ZZJR ZZJS ZZKM ZZKO ZZKS ZZKY ZZLA ZZLB 
    ZZLC ZZLD ZZLE ZZLF ZZLG ZZLH ZZLI ZZMA ZZMB ZZMD ZZME ZZMF ZZMG ZZML ZZMN ZZMO ZZMR ZZMS ZZMT 
    ZZMU ZZMV ZZMW ZZMX ZZMY ZZMZ ZZNA ZZNB ZZNC ZZND ZZNE ZZNF ZZNG ZZNH ZZNL ZZNM ZZNN ZZNO ZZNR 
    ZZNS ZZNT ZZNU ZZNV ZZNW ZZOA ZZOB ZZOC ZZOD ZZOE ZZOF ZZOG ZZOH ZZOJ ZZOL ZZOS ZZOT ZZOU ZZOV 
    ZZOW ZZOX ZZOZ ZZPA ZZPB ZZPC ZZPD ZZPE ZZPK ZZPL ZZPO ZZPS ZZPY ZZPZ ZZQK ZZQM ZZQR ZZQS ZZRA 
    ZZRB ZZRC ZZRD ZZRF ZZRH ZZRL ZZRM ZZRS ZZRT ZZRU ZZRV ZZRX ZZRY ZZSA ZZSB ZZSD ZZSF ZZSG ZZSH 
    ZZSM ZZSN ZZSO ZZSP ZZSQ ZZSR ZZSS ZZST ZZSU ZZSV ZZSW ZZSX ZZSY ZZSZ ZZTA ZZTB ZZTF ZZTH ZZTI 
    ZZTL ZZTM ZZTO ZZTP ZZTS ZZTU ZZTV ZZTX ZZUA ZZUP ZZUS ZZUT ZZUX ZZUY ZZVA ZZVB ZZVC ZZVD ZZVE 
    ZZVF ZZVG ZZVH ZZVI ZZVJ ZZVK ZZVL ZZVM ZZVN ZZVO ZZVP ZZVQ ZZVR ZZVS ZZVT ZZVU ZZVV ZZVW ZZVX 
    ZZVY ZZVZ ZZWA ZZWB ZZWC ZZWD ZZWE ZZWF ZZWG ZZWH ZZWJ ZZWK ZZWL ZZWM ZZWN ZZWO ZZWP ZZWQ ZZWR 
    ZZWS ZZWT ZZWU ZZWV ZZWW ZZXA ZZXC ZZXD ZZXF ZZXH ZZXN ZZXO ZZXS ZZXT ZZXU ZZXV ZZYA ZZYB ZZYC 
    ZZYR ZZZA ZZZB ZZZD ZZZE ZZZM ZZZN ZZZO ZZZP ZZZQ ZZZR ZZZS ZZZT ZZZU ZZZV ZZZW ZZZZ
""".split()

SUFFIXES = ["", "0", "1", "00", "01", "08", "11", "000", "0000", "+0001", "0001",
            "00007100000", "0000710000", "000071000000", "00014200000", "00007300000"]


def ready_client(vfo=0, control="both"):
    """A client in a state where a set for the followed VFO is allowed: connected, receiving,
    fresh reading, CAT to VFO B off. Records what it writes."""
    written = []
    c = CatClient("127.0.0.1", 1, lambda *a: None, lambda *a: None, control=control,
                  follow="AB"[vfo])
    c._writer = type("W", (), {"write": lambda self, b: written.append(b)})()
    c.freq_a, c.mode_a, c.freq_b, c.mode_b = 7_100_000, 0, 14_200_000, 1
    c.vfo, c.cat_to_b = vfo, False
    c._sampled_at = time.monotonic()
    return c, written


@pytest.mark.parametrize("vfo", [0, 1])
def test_every_other_thetis_command_is_refused(vfo):
    """Even in the most permissive state, with sets enabled, nothing but frequency and mode of the
    followed VFO (in band, in RX) gets through: no TX, TUNE, PTT, power, or any other set."""
    assert len(THETIS_CAT_CODES) == 421
    c, written = ready_client(vfo)
    passed = []
    for code in THETIS_CAT_CODES:
        for sfx in SUFFIXES:
            cmd = f"{code}{sfx};".encode()
            if cmd in SAFE_COMMANDS:
                continue
            try:
                c._send(cmd, sets=True)
                passed.append(cmd)
            except UnsafeCommand:
                pass
    f, m = (b"ZZFA", b"ZZMD") if vfo == 0 else (b"ZZFB", b"ZZME")
    # 7.1 and 14.2 MHz are in band (7.3 isn't); of the suffixes only 00 and 01 are allowed modes
    assert sorted(passed) == sorted([f + b"00007100000;", f + b"00014200000;", m + b"00;", m + b"01;"])
    assert written == passed


def test_sets_need_the_sets_flag_and_the_poll_batches_never_set():
    c, written = ready_client()
    with pytest.raises(UnsafeCommand):
        c._send(b"ZZFA00007100000;")                 # sets=False: reads only, as in v1.0
    for batch in (cat.POLL_BATCH, cat.CONTROL_BATCH):
        assert not any(cat.SET_RE.fullmatch(p) for p in re.findall(rb"[^;]*;", batch))
    assert written == []


@pytest.mark.parametrize("bad", [
    b"TX;", b"TX1;", b"RX;", b"ZZTX1;", b"ZZTU1;", b"ZZXU;", b"ZZZZ;", b"ZZSP1;", b"ZZPC100;",
    b"ZZFA0007100000;", b"ZZFA000071000000;", b"ZZFA+0007100000;", b"ZZFA 0007100000;",
    b"ZZFA00007100000", b"zzfa00007100000;", b"ZZFA00007100000;ZZTX1;", b"ZZTX1;ZZFA00007100000;",
    b"ZZFA00007100000;\r\nZZTX1;", b"ZZFA00007100000;x", b"ZZFA00007100000;;",
    b"ZZMD08;", b"ZZMD02;", b"ZZMD11;", b"ZZMD07;", b"ZZMD09;", b"ZZMD12;", b"ZZMD1;",
    b"ZZFB00007100000;", b"ZZME01;",                 # VFO B while following A
    b"ZZFA00007300000;", b"ZZFA00007200000;", b"ZZFA00001809999;", b"ZZFA00005000000;",
    b"ZZFA00000000000;", b"ZZFA99999999999;",        # outside the UK bands
    b"FA00007100000;", b"MD1;",                      # Kenwood sets are never used
])
def test_nasty_variants_are_refused(bad):
    c, written = ready_client(0)
    with pytest.raises(UnsafeCommand):
        c._send(bad, sets=True)
    assert written == []


def test_refused_in_every_unsafe_state():
    def refused(setup, what):
        c, written = ready_client(0)
        setup(c)
        reason = c.request_tune(7_074_000, 1)
        assert reason and what in reason, reason
        with pytest.raises(UnsafeCommand):
            c._send(b"ZZFA00007074000;", sets=True)
        assert written == [] and c._want is None

    refused(lambda c: setattr(c, "control", "radio"), "Radio controls receiver")
    refused(lambda c: setattr(c, "tx", True), "transmitting")
    refused(lambda c: setattr(c, "_tx_ended", time.monotonic()), "transmitting")   # hold-off
    refused(lambda c: setattr(c, "_sampled_at", time.monotonic() - 1), "fresh")
    refused(lambda c: setattr(c, "_writer", None), "not connected")
    refused(lambda c: setattr(c, "freq_a", None), "not connected")
    refused(lambda c: setattr(c, "cat_to_b", True), "CAT to VFO B")
    refused(lambda c: setattr(c, "cat_to_b", None), "CAT to VFO B")
    refused(lambda c: setattr(c, "_fault", "stopped"), "stopped")

    # Follow switched to VFO B between the request and the next poll: the VFO A change is dropped
    c, written = ready_client(0)
    assert c.request_tune(7_074_000, 1) is None
    c.vfo = 1
    assert c._take_sets() == b"" and written == []
    with pytest.raises(UnsafeCommand):
        c._send(b"ZZFA00007074000;", sets=True)

    c, _ = ready_client(0)
    assert c.request_tune(7_300_000, 1) == "7.300000 MHz is outside the UK amateur bands"
    assert "mode 8" in c.request_tune(7_074_000, 8)
    assert c.request_tune(7_074_000, 1) is None


def test_change_is_dropped_not_kept_if_tx_starts_before_it_is_sent():
    c, written = ready_client(0)
    assert c.request_tune(7_074_000) is None
    c.tx = True                                      # MOX pressed before the next poll
    assert c._take_sets() == b""
    assert c._want is None                           # and it is not sent later either
    c.tx = False
    assert c._take_sets() == b""
    assert written == []


def test_only_what_changed_is_sent():
    c, _ = ready_client(0)
    c.request_tune(7_100_000, 1)
    assert c._take_sets() == b"ZZMD01;"
    c.request_tune(7_074_000, 0)
    assert c._take_sets() == b"ZZFA00007074000;"
    c.request_tune(7_100_000, None)
    assert c._take_sets() == b""
    c2, _ = ready_client(1)
    c2.request_tune(14_074_000, 4)
    assert c2._take_sets() == b"ZZFB00014074000;ZZME04;"


def test_refused_set_reply_is_not_counted_as_a_batch_answer():
    c, _ = ready_client(0)
    c._pending, c._sets_in_flight = 6, 1
    c._handle("?")
    assert c._pending == 6 and c._sets_in_flight == 0
    c._handle("?")                                   # a "?" for a read still counts
    assert c._pending == 5


def test_set_landing_on_the_other_vfo_stops_control():
    notes = []
    c, _ = ready_client(0)
    c.on_note = notes.append
    c._sent = (0, 7_074_000, None, 3)
    c.freq_b = 7_074_000                             # VFO B moved instead of A
    c._verify()
    assert "stopped" in c.request_tune(7_080_000)
    c._update_note()
    assert notes and "moved VFO B" in notes[-1]
    c.set_control("kiwi")                            # choosing Control again clears it
    assert c.request_tune(7_080_000) is None and notes[-1] is None


def test_uk_bands_match_thetis_uk_region():
    assert uk_band_for(1_810_000) == "160m" and uk_band_for(1_809_999) is None
    assert uk_band_for(1_999_999) == "160m" and uk_band_for(2_000_000) is None
    assert uk_band_for(3_799_999) == "80m" and uk_band_for(3_800_000) is None
    assert uk_band_for(5_250_000) == "60m" and uk_band_for(5_410_000) is None
    assert uk_band_for(7_199_999) == "40m" and uk_band_for(7_200_000) is None
    assert uk_band_for(10_120_000) == "30m" and uk_band_for(10_150_000) is None
    assert uk_band_for(29_699_999) == "10m" and uk_band_for(29_700_000) is None
    assert uk_band_for(50_029_999) is None and uk_band_for(50_030_000) == "6m"
    for gen in (198_000, 475_000, 6_000_000, 9_500_000, 11_000_000, 27_555_000, 70_200_000):
        assert uk_band_for(gen) is None


def test_kiwi_to_thetis_modes():
    assert kiwi_to_thetis("lsb", 1) == 0 and kiwi_to_thetis("usn", 0) == 1
    assert kiwi_to_thetis("usb", 7) is None          # DIGU already maps to usb: keep it
    assert kiwi_to_thetis("lsb", 9) is None          # DIGL
    assert kiwi_to_thetis("cw", 3) is None and kiwi_to_thetis("cwn", 1) == 4
    assert kiwi_to_thetis("am", 2) is None           # DSB maps to am
    assert kiwi_to_thetis("sau", 6) == 10 and kiwi_to_thetis("nnfm", 1) == 5
    for none in ("iq", "drm", "qam", "bogus", None):
        assert kiwi_to_thetis(none, 1) is None
    assert kiwi_to_thetis("usb", 7, {"7": "usn"}) is None


async def started(fake, **kw):
    await fake.start()
    notes = []
    client = CatClient("127.0.0.1", fake.port, lambda *s: None, lambda c: None,
                       poll_interval=0.02, on_note=notes.append, **kw)
    client.start()
    return client, notes


def sets_received(fake):
    return [c for c in fake.received if re.fullmatch(r"ZZ(F[AB]\d{11}|M[DE]\d\d)", c)]


def test_end_to_end_kiwi_to_radio():
    async def scenario():
        fake = FakeThetis()
        client, _ = await started(fake, control="kiwi")
        await until(lambda: client.cat_to_b is False, "redirect check")
        assert client.request_tune(7_074_000, 1) is None
        await until(lambda: fake.freq_a == 7_074_000, "VFO A set")
        await until(lambda: client.freq_hz == 7_074_000 and client._sent is None, "read back")
        assert fake.mode_a == 1 and fake.freq_b == 7_100_000
        assert sets_received(fake) == ["ZZFA00007074000"]          # mode was already USB
        assert set(fake.received) - {"ZZFA00007074000"} <= {
            "ZZIF", "ZZFB", "ZZME", "ZZRS", "ZZXV", "ZZFA"}
        await client.stop()
        await fake.stop()

    asyncio.run(scenario())


def test_radio_control_sends_v1_reads_only():
    async def scenario():
        fake = FakeThetis()
        client, _ = await started(fake)              # control defaults to "radio"
        await until(lambda: client.freq_hz is not None)
        assert client.request_tune(7_074_000, 1) == "Control is set to Radio controls receiver"
        await asyncio.sleep(0.3)
        assert set(fake.received) == {"ZZIF", "ZZFB", "ZZME", "ZZRS", "ZZXV"}
        assert fake.freq_a == 14_200_000
        await client.stop()
        await fake.stop()

    asyncio.run(scenario())


def test_cat_to_vfo_b_blocks_vfo_a_but_not_b():
    async def scenario():
        fake = FakeThetis()
        fake.cat_to_b = True
        client, notes = await started(fake, control="both")
        await until(lambda: client.cat_to_b is True, "redirect seen")
        await until(lambda: notes and notes[-1] == CAT_TO_B_NOTE, "note")
        assert client.request_tune(7_074_000) == CAT_TO_B_NOTE
        client.set_follow("B")
        await until(lambda: notes[-1] is None, "note cleared for VFO B")
        assert client.request_tune(3_700_000, 0) is None
        await until(lambda: fake.freq_b == 3_700_000, "VFO B set")
        assert fake.freq_a == 14_200_000 and fake.mode_b == 0
        assert sets_received(fake) == ["ZZFB00003700000"]          # VFO B was already LSB
        fake.cat_to_b = False
        await until(lambda: client.cat_to_b is False, "redirect off again")
        await client.stop()
        await fake.stop()

    asyncio.run(scenario())


def test_nothing_is_sent_during_or_just_after_tx():
    async def scenario():
        fake = FakeThetis()
        client, _ = await started(fake, control="kiwi")
        await until(lambda: client.cat_to_b is False)
        fake.set_state(tun=True)
        await until(lambda: client.tx, "TUNE")
        assert client.request_tune(7_074_000) == "the radio is transmitting"
        fake.set_state(tun=False)
        await until(lambda: not client.tx, "RX")
        assert client.request_tune(7_074_000) == "the radio is transmitting"   # hold-off
        await asyncio.sleep(cat.TX_HOLDOFF)
        assert client.request_tune(7_074_000) is None
        await until(lambda: fake.freq_a == 7_074_000)
        assert sets_received(fake) == ["ZZFA00007074000"]
        await client.stop()
        await fake.stop()

    asyncio.run(scenario())


def test_redirect_is_only_decided_while_a_and_b_differ():
    c, _ = ready_client(0)
    c.cat_to_b = None
    c.freq_a = c.freq_b = c._zzfa = 7_100_000        # A = B: ZZFA; can't tell which it read
    c._check_redirect()
    assert c.cat_to_b is None and "different frequencies" in c.request_tune(7_074_000)
    c.freq_b = 14_200_000
    c._check_redirect()
    assert c.cat_to_b is False
    c.freq_b = 7_100_000                             # once known it stays known...
    c._check_redirect()
    assert c.cat_to_b is False
    c.freq_b, c._zzfa = 14_200_000, 14_200_000       # ...until ZZFA; reports VFO B
    c._check_redirect()
    assert c.cat_to_b is True


def test_runaway_kiwi_stops_control():
    """A Kiwi that keeps moving on its own (stuck auto-repeat step, a loop): after RUNAWAY_SETS
    changes within RUNAWAY_WINDOW s nothing more is sent until Control is chosen again."""
    notes = []
    c, _ = ready_client(0)
    c.on_note = notes.append
    sent = []
    for i in range(cat.RUNAWAY_SETS + 3):
        assert c.request_tune(7_160_990 - 10 * i) is None or c._fault
        sets = c._take_sets()
        if sets:
            sent.append(sets)
            c.freq_a = 7_160_990 - 10 * i
    assert len(sent) == cat.RUNAWAY_SETS
    assert "stopped changing the radio" in c.request_tune(7_100_000)
    c._update_note()
    assert "more than 5 times" in notes[-1]
    c.set_control("both")
    assert c.request_tune(7_100_000) is None and c._take_sets() == b"ZZFA00007100000;"
