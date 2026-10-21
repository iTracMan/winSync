import asyncio
import gzip
import json
import tempfile

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from winSync.server import STATE, build_app, load_config
from tests.fake_thetis import FakeThetis

KIWI_HTML = "<html><head><title>Fake Kiwi</title></head><body>hi</body></html>"
JS = b"var x = 1;" * 100


def fake_kiwi():
    """Minimal stand-in for a KiwiSDR: HTML page, always-gzipped JS, echoing websocket."""
    app = web.Application()

    async def index(request):
        return web.Response(text=KIWI_HTML, content_type="text/html")

    async def js(request):
        return web.Response(body=gzip.compress(JS), headers={
            "Content-Encoding": "gzip", "Content-Type": "application/javascript"})

    async def ws(request):
        w = web.WebSocketResponse()
        await w.prepare(request)
        async for msg in w:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await w.send_str("echo:" + msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await w.send_bytes(msg.data[::-1])
        return w

    app.router.add_get("/", index)
    app.router.add_get("/kiwisdr.min.js", js)
    app.router.add_get("/ws/kiwi/{ts}/SND", ws)  # real KiwiSDR 1.902 path
    return app


def run(coro):
    return asyncio.run(coro)


async def make_stack(cat_port):
    kiwi = TestServer(fake_kiwi())
    await kiwi.start_server()
    cfg = load_config(None)
    cfg["server"]["urls_file"] = tempfile.mkdtemp() + "/urls.json"
    cfg["cat"]["port"] = cat_port
    cfg["kiwi"]["url"] = f"http://127.0.0.1:{kiwi.port}/"
    client = TestClient(TestServer(build_app(cfg)))
    await client.start_server()
    return kiwi, client


def test_proxy_injects_bridge_and_passes_gzip_through():
    async def scenario():
        kiwi, client = await make_stack(1)
        # iframe request: HTML gets the bridge tag
        r = await client.get("/", headers={"Sec-Fetch-Dest": "iframe"})
        text = await r.text()
        assert r.status == 200
        assert '<script src="/_winsync/static/bridge.js"></script>' in text
        assert "Fake Kiwi" in text
        # gzip JS is passed through untouched with its Content-Encoding
        r = await client.get("/kiwisdr.min.js", skip_auto_headers=("Accept-Encoding",),
                             auto_decompress=False)
        assert r.headers["Content-Encoding"] == "gzip"
        assert gzip.decompress(await r.read()) == JS
        # top-level navigation is redirected to the control page
        r = await client.get("/", headers={"Sec-Fetch-Dest": "document"}, allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "/_winsync/"
        await client.close()
        await kiwi.close()

    run(scenario())


def test_proxy_relays_websocket_text_and_binary():
    async def scenario():
        kiwi, client = await make_stack(1)
        ws = await client.ws_connect("/ws/kiwi/12345/SND")
        await ws.send_str("SET auth t=kiwi")
        assert (await ws.receive()).data == "echo:SET auth t=kiwi"
        await ws.send_bytes(b"abc")
        assert (await ws.receive()).data == b"cba"
        await ws.close()
        await client.close()
        await kiwi.close()

    run(scenario())


def test_state_feed_follows_thetis():
    async def scenario():
        fake = FakeThetis()
        await fake.start()
        kiwi, client = await make_stack(fake.port)
        ws = await client.ws_connect("/_winsync/ws")

        async def wait_for(pred):
            for _ in range(100):
                msg = json.loads((await asyncio.wait_for(ws.receive(), 3)).data)
                if pred(msg):
                    return msg
            raise AssertionError("condition never met")

        m = await wait_for(lambda m: m["freq_hz"] == 14_200_000 and m["kiwi_mode"] == "usb")
        assert m["band"] == "20m" and m["mode_label"] == "USB" and m["cat_connected"]
        assert m["tx"] is False and m["rx"] == 0 and m["vfo"] == "A"
        assert m["rx2_on"] is False and m["follow"] == "auto"

        fake.set_state(freq_a=7_030_000, mode_a=4)
        m = await wait_for(lambda m: m["freq_hz"] == 7_030_000 and m["cat_mode"] == 4)
        assert m["band"] == "40m" and m["kiwi_mode"] == "cw" and m["mode_label"] == "CWU"

        fake.set_state(mox=True)
        m = await wait_for(lambda m: m["tx"] is True)
        assert m["freq_hz"] == 7_030_000
        fake.set_state(mox=False)

        # RX2 on and VFO B tuned: the Kiwi follows VFO B
        fake.set_state(rx2=True)
        await wait_for(lambda m: m["rx2_on"] is True)
        fake.set_state(freq_b=3_749_400, mode_b=1)
        m = await wait_for(lambda m: m["rx"] == 1 and m["freq_hz"] == 3_749_400)
        assert m["vfo"] == "B" and m["band"] == "80m" and m["kiwi_mode"] == "usb"

        # The page's Follow selector
        r = await client.post("/_winsync/api/follow", json={"follow": "A"})
        assert (await r.json())["follow"] == "A"
        m = await wait_for(lambda m: m["follow"] == "A" and m["vfo"] == "A")
        assert m["freq_hz"] == 7_030_000
        r = await client.post("/_winsync/api/follow", json={"follow": "C"})
        assert r.status == 400
        r = await client.post("/_winsync/api/follow", json={"follow": "B"},
                              headers={"Origin": "http://evil.example"})
        assert r.status == 403

        await ws.close()
        await client.close()
        await kiwi.close()
        await fake.stop()

    run(scenario())


def test_target_api_validates_and_rejects_cross_site():
    async def scenario():
        kiwi, client = await make_stack(1)
        r = await client.post("/_winsync/api/target", json={"url": "ftp://x"})
        assert r.status == 400
        r = await client.post("/_winsync/api/target", json={"url": "kiwi.example.org:8073/foo?x=1"})
        assert (await r.json())["target"] == "http://kiwi.example.org:8073"
        r = await client.post("/_winsync/api/target", json={"url": "http://a.b/"},
                              headers={"Origin": "http://evil.example"})
        assert r.status == 403
        await client.close()
        await kiwi.close()

    run(scenario())


def test_urls_persist_across_restart(tmp_path):
    async def scenario():
        cfg = load_config(None)
        cfg["server"]["urls_file"] = str(tmp_path / "sub" / "urls.json")
        client = TestClient(TestServer(build_app(cfg)))
        await client.start_server()
        assert (await (await client.get("/_winsync/api/urls")).json())["urls"] == []
        r = await client.put("/_winsync/api/urls", json={"urls": ["http://a", "http://b", "http://a", "", 5]})
        assert (await r.json())["urls"] == ["http://a", "http://b"]
        r = await client.put("/_winsync/api/urls", json={"urls": "x"})
        assert r.status == 400
        r = await client.put("/_winsync/api/urls", json={"urls": []},
                             headers={"Origin": "http://evil.example"})
        assert r.status == 403
        await client.close()

        client = TestClient(TestServer(build_app(cfg)))   # "restart"
        await client.start_server()
        assert (await (await client.get("/_winsync/api/urls")).json())["urls"] == ["http://a", "http://b"]
        await client.close()

        (tmp_path / "sub" / "urls.json").write_text("{corrupt")
        client = TestClient(TestServer(build_app(cfg)))
        await client.start_server()
        assert (await (await client.get("/_winsync/api/urls")).json())["urls"] == []
        await client.close()

    run(scenario())


def test_kiwi_to_radio_api():
    async def scenario():
        fake = FakeThetis(freq=7_100_000, mode=0)
        fake.freq_b = 3_700_000                   # A and B differ, so CAT to VFO B can be checked
        await fake.start()
        kiwi, client = await make_stack(fake.port)
        ws = await client.ws_connect("/_winsync/ws")

        async def wait_for(pred):
            for _ in range(100):
                msg = json.loads((await asyncio.wait_for(ws.receive(), 3)).data)
                if pred(msg):
                    return msg
            raise AssertionError("condition never met")

        m = await wait_for(lambda m: m["freq_hz"] == 7_100_000)
        assert m["control"] == "radio" and m["control_note"] is None

        # Default: radio controls Kiwi, so the radio is never moved
        r = await client.post("/_winsync/api/tune", json={"freq_hz": 7_074_000, "kiwi_mode": "usb"})
        j = await r.json()
        assert j["moved"] is False and "Radio controls Kiwi" in j["message"]

        r = await client.post("/_winsync/api/control", json={"control": "both"})
        assert (await r.json())["control"] == "both"
        await wait_for(lambda m: m["control"] == "both")
        for _ in range(100):                      # ZZFA; polled: CAT to VFO B seen to be off
            if client.app[STATE].cat.cat_to_b is False:
                break
            await asyncio.sleep(0.02)

        r = await client.post("/_winsync/api/tune", json={"freq_hz": 7_074_000, "kiwi_mode": "usn"})
        j = await r.json()
        assert j == {"moved": True, "freq_hz": 7_074_000, "band": "40m", "kiwi_mode": "usb"}
        m = await wait_for(lambda m: m["freq_hz"] == 7_074_000 and m["mode_label"] == "USB")
        assert fake.freq_a == 7_074_000 and fake.mode_a == 1

        # Outside the UK bands: Kiwi only, and the page is told why
        r = await client.post("/_winsync/api/tune", json={"freq_hz": 7_300_000, "kiwi_mode": "usb"})
        j = await r.json()
        assert j["moved"] is False and j["out_of_band"] is True
        assert j["message"] == "Radio not moved: 7.300000 MHz is outside the UK amateur bands"
        assert fake.freq_a == 7_074_000

        # TX: refused
        fake.set_state(mox=True)
        await wait_for(lambda m: m["tx"] is True)
        r = await client.post("/_winsync/api/tune", json={"freq_hz": 7_080_000})
        assert (await r.json())["message"] == "Radio not moved: the radio is transmitting"
        fake.set_state(mox=False)

        for bad in ({"freq_hz": "7074000"}, {"freq_hz": 7074000.5}, {"freq_hz": True}, {},
                    {"freq_hz": 7_074_000, "kiwi_mode": 3}):
            assert (await client.post("/_winsync/api/tune", json=bad)).status == 400
        assert (await client.post("/_winsync/api/control", json={"control": "x"})).status == 400
        evil = {"Origin": "http://evil.example"}
        assert (await client.post("/_winsync/api/tune", json={"freq_hz": 7_074_000},
                                  headers=evil)).status == 403
        assert (await client.post("/_winsync/api/control", json={"control": "radio"},
                                  headers=evil)).status == 403
        assert fake.freq_a == 7_074_000

        await ws.close()
        await client.close()
        await kiwi.close()
        await fake.stop()

    run(scenario())


def test_dry_run_never_sends_and_diag_is_opt_in():
    async def scenario():
        fake = FakeThetis(freq=7_100_000, mode=0)
        fake.freq_b = 3_700_000
        await fake.start()
        kiwi, client = await make_stack(fake.port)
        st = client.app[STATE]
        st.cfg["sync"]["dry_run"] = True
        await client.post("/_winsync/api/control", json={"control": "both"})
        for _ in range(100):
            if st.cat.cat_to_b is False:
                break
            await asyncio.sleep(0.02)
        r = await client.post("/_winsync/api/tune", json={"freq_hz": 7_074_000, "kiwi_mode": "lsb"})
        j = await r.json()
        assert j["moved"] is False and j["message"] == "Dry run, radio not moved: would have moved it"
        r = await client.post("/_winsync/api/tune", json={"freq_hz": 7_300_000, "kiwi_mode": "lsb"})
        assert (await r.json())["out_of_band"] is True
        await asyncio.sleep(0.3)
        assert fake.freq_a == 7_100_000
        assert not [c for c in fake.received if c.startswith(("ZZFA0", "ZZMD0"))]

        assert (await client.post("/_winsync/api/diag", json={"events": ["x"]})).status == 403
        st.cfg["sync"]["diag"] = True
        assert (await client.post("/_winsync/api/diag", json={"events": ["x"]})).status == 200
        assert (await client.post("/_winsync/api/diag", json={"events": ["x"]},
                                  headers={"Origin": "http://evil.example"})).status == 403
        await client.close()
        await kiwi.close()
        await fake.stop()

    run(scenario())


def test_omnirig_source_is_read_only(monkeypatch):
    """With [cat] source = "omnirig": Control is forced to "radio", the page can't change that, and
    /api/tune never moves the radio. Uses the real COM path, so on a PC without OmniRig it also
    checks the "not installed" note; with OmniRig installed it only checks what doesn't depend on it."""
    from winSync import omnirig

    async def scenario():
        kiwi = TestServer(fake_kiwi())
        await kiwi.start_server()
        cfg = load_config(None)
        cfg["server"]["urls_file"] = tempfile.mkdtemp() + "/urls.json"
        cfg["cat"]["source"] = "omnirig"
        cfg["sync"]["control"] = "both"
        cfg["kiwi"]["url"] = f"http://127.0.0.1:{kiwi.port}/"
        client = TestClient(TestServer(build_app(cfg)))
        await client.start_server()
        st = client.app[STATE]
        assert st.source == "omnirig" and st.control == "radio"
        assert isinstance(st.cat, omnirig.OmniRigSource)
        r = await client.post("/_winsync/api/control", json={"control": "kiwi"})
        assert r.status == 400 and (await r.json())["error"] == omnirig.READ_ONLY
        assert (await client.post("/_winsync/api/control", json={"control": "radio"})).status == 200
        r = await client.post("/_winsync/api/tune", json={"freq_hz": 7_074_000, "kiwi_mode": "lsb"})
        j = await r.json()
        assert j["moved"] is False and j["message"] == "Radio not moved: " + omnirig.READ_ONLY
        async with client.ws_connect("/_winsync/ws") as ws:
            snap = json.loads((await ws.receive()).data)
        assert snap["source"] == "omnirig" and snap["control"] == "radio"
        assert (await (await client.get("/_winsync/api/config")).json())["source"] == "omnirig"
        try:
            import winreg
            winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, omnirig.PROG_ID).Close()
            installed = True
        except OSError:
            installed = False
        if not installed:
            for _ in range(100):
                if st.control_note:
                    break
                await asyncio.sleep(0.02)
            assert st.control_note == omnirig.NOT_INSTALLED and st.cat_connected is False
        await client.close()
        await kiwi.close()

    run(scenario())
