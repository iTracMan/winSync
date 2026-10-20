"""Fake Thetis TCP/IP CAT server for tests and manual runs.

Manual: python -m tests.fake_thetis [port]  -- then type e.g. `a 14074000 1`, `b 7030000 4`,
`rx2 1` or `tx 1` (Thetis mode numbers: 0 LSB, 1 USB, 3 CWL, 4 CWU, 7 DIGU, ...).
Mimics Thetis v2.10.3.15 (MI0BOT): greeting banner on connect, answers the ZZ read commands winSync
uses with the same layouts as the real one (see the probe results in the README), "?;" to anything
else, and never pushes changes.
"""
import asyncio
import sys

BANNER = "#Thetis TCP/IP Cat - Thetis v2.10.3.15 x64 (09/04/26) HL2 (MI0BOT)#;"


class FakeThetis:
    def __init__(self, port=0, freq=14_200_000, mode=1):
        self.port = port
        self.freq_a, self.mode_a = freq, mode        # VFO A / RX1, Thetis mode numbers
        self.freq_b, self.mode_b = 7_100_000, 0      # VFO B / RX2
        self.rx2 = False
        self.mox = self.tun = False
        self.answer = True                           # False: swallow requests (a hung Thetis)
        self.received = []
        self.writers = []
        self._server = None

    async def start(self):
        self._server = await asyncio.start_server(self._client, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        for w in self.writers:
            w.close()
        self._server.close()
        await self._server.wait_closed()

    def reply(self, cmd):
        if cmd == "ZZIF":   # freq step rit ritOn xitOn mem TX mode(2) fr scan split balance
            return (f"ZZIF{self.freq_a:011d}0011+00000" "0" "0" "000"
                    f"{int(self.mox)}{self.mode_a:02d}" "0" "0" "0" "0000;")
        if cmd == "ZZFB":
            return f"ZZFB{self.freq_b:011d};"
        if cmd == "ZZME":
            return f"ZZME{self.mode_b:02d};"
        if cmd == "ZZRS":
            return f"ZZRS{int(self.rx2)};"
        if cmd == "ZZXV":
            return f"ZZXV{(self.mox << 6) | (self.tun << 7):04d};"
        return "?;"

    async def _client(self, reader, writer):
        self.writers.append(writer)
        writer.write(BANNER.encode())
        buf = ""
        try:
            while data := await reader.read(256):
                buf += data.decode()
                *cmds, buf = buf.split(";")
                for cmd in cmds:
                    self.received.append(cmd)
                    if self.answer:
                        writer.write(self.reply(cmd).encode())
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            self.writers.remove(writer)
            writer.close()

    def set_state(self, freq_a=None, mode_a=None, freq_b=None, mode_b=None, rx2=None, mox=None,
                  tun=None):
        for name, value in locals().items():
            if name != "self" and value is not None:
                setattr(self, name, value)


async def _main(port):
    fake = FakeThetis(port)
    await fake.start()
    print(f"fake Thetis CAT on 127.0.0.1:{fake.port}; "
          "enter 'a <Hz> <mode>', 'b <Hz> <mode>', 'rx2 0|1', 'tx 0|1' or 'tun 0|1'")
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        try:
            what, *args = line.split()
            n = [int(a) for a in args]
            if what == "a":
                fake.set_state(freq_a=n[0], mode_a=n[1])
            elif what == "b":
                fake.set_state(freq_b=n[0], mode_b=n[1])
            elif what in ("rx2", "tx", "tun"):
                fake.set_state(**{"mox" if what == "tx" else what: bool(n[0])})
            else:
                raise ValueError
        except (ValueError, IndexError):
            print("usage: a <Hz> <mode> | b <Hz> <mode> | rx2 0|1 | tx 0|1 | tun 0|1")


if __name__ == "__main__":
    asyncio.run(_main(int(sys.argv[1]) if len(sys.argv) > 1 else 13013))
