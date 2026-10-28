"""Fake SDR Console CAT port for tests and manual runs. Never needs the radio.

Answers FA; IF; MD; NA; the way SDR Console v3.4 did live (see sdrconsole.py), and "?;" to anything
else, including TX; and RX; (which in the real SDR Console switch to transmit and receive). Never pushes
changes (auto-information is always off). Every command received is kept in .received.

Manual, over TCP (no COM port needed; point winSync at it with [sdrconsole] port =
"socket://127.0.0.1:7300"):
    python -m tests.fake_sdrconsole 7300
or on one end of a spare com0com pair (winSync on the other end), e.g.
    python -m tests.fake_sdrconsole COM22
then type `f 10489730000`, `m 2` (SDR Console mode digit) or `tx 1` / `tx 0`.
"""
import asyncio
import sys
import threading

NAME = "ADALM-PLUTO"       # what SDR Console's NA; returns is not confirmed live; any text works


class FakeSdrConsole:
    def __init__(self, freq=10_489_730_000, mode=2, name=NAME):
        self.freq, self.mode, self.tx, self.name = freq, mode, False, name
        self.answer = True                   # False: swallow requests (SDR Console not answering)
        self.received = []
        self.writers = []
        self.port = None
        self._server = None

    def reply(self, cmd):
        if cmd == "IF":
            return f"IF{self.freq:011d}0000+{'0' * 10}{int(self.tx)}{self.mode}{'0' * 7};"
        if cmd == "FA":
            return f"FA{self.freq:011d};"
        if cmd == "MD":
            return f"MD{self.mode};"
        if cmd == "NA":
            return f"NA{self.name};"
        return "?;"

    def feed(self, text, buf=""):
        """Handle received text; returns (reply text, unfinished remainder)."""
        *cmds, buf = (buf + text).split(";")
        out = ""
        for cmd in cmds:
            self.received.append(cmd + ";")
            if self.answer:
                out += self.reply(cmd)
        return out, buf

    def set_state(self, freq=None, mode=None, tx=None):
        for name, value in (("freq", freq), ("mode", mode), ("tx", tx)):
            if value is not None:
                setattr(self, name, value)

    # --- over TCP: winSync opens socket://127.0.0.1:<port> ----------------------------------
    @property
    def url(self):
        return f"socket://127.0.0.1:{self.port}"

    async def start(self, port=0):
        self._server = await asyncio.start_server(self._client, "127.0.0.1", port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        for w in list(self.writers):
            w.close()
        self._server.close()
        await self._server.wait_closed()

    async def _client(self, reader, writer):
        self.writers.append(writer)
        buf = ""
        try:
            while data := await reader.read(256):
                out, buf = self.feed(data.decode("ascii", errors="replace"), buf)
                if out:
                    writer.write(out.encode())
                    await writer.drain()
        except ConnectionError:
            pass
        finally:
            if writer in self.writers:
                self.writers.remove(writer)
            writer.close()

    # --- on a COM port (one end of a com0com pair) -----------------------------------------
    def serve_serial(self, name, baud=57600, stop=None):
        import serial
        stop = stop or threading.Event()
        with serial.Serial(name, baud, timeout=0.05) as ser:
            buf = ""
            while not stop.is_set():
                data = ser.read(max(1, ser.in_waiting))
                if data:
                    out, buf = self.feed(data.decode("ascii", errors="replace"), buf)
                    if out:
                        ser.write(out.encode())


def _commands(fake):
    print("enter 'f <Hz>', 'm <mode digit>' or 'tx 0|1'")
    for line in sys.stdin:
        try:
            what, value = line.split()
            fake.set_state(**{{"f": "freq", "m": "mode", "tx": "tx"}[what]: int(value)})
            if what == "tx":
                fake.tx = bool(fake.tx)
            print("now", fake.reply("IF"))
        except (ValueError, KeyError):
            print("usage: f <Hz> | m <0-9> | tx 0|1")


def main(where):
    fake = FakeSdrConsole()
    if where.upper().startswith("COM"):
        print(f"fake SDR Console on {where}")
        threading.Thread(target=fake.serve_serial, args=(where,), daemon=True).start()
        _commands(fake)
        return

    async def run():
        await fake.start(int(where))
        print(f"fake SDR Console on {fake.url}")
        await asyncio.get_running_loop().run_in_executor(None, _commands, fake)
    asyncio.run(run())


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "7300")
