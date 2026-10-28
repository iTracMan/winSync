"""winSync server: CAT client + control page + WebSocket state feed + Kiwi reverse proxy."""
import asyncio
import json
import logging
import os
import tomllib
from pathlib import Path

import aiohttp
from aiohttp import web
from yarl import URL

from .bands import band_for
from .cat import CONTROL_MODES, FOLLOW_MODES, CatClient
from .omnirig import READ_ONLY, OmniRigSource
from . import sdrconsole
from .modes import THETIS_LABELS, kiwi_to_thetis, thetis_to_kiwi
from .proxy import SESSION, normalize_target, proxy_http, proxy_ws

log = logging.getLogger("winSync")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
MAX_URLS = 30
SOURCES = ("thetis", "omnirig", "sdrconsole")
# Sources winSync only reads from: Control stays "radio", with this reason
READ_ONLY_SOURCES = {"omnirig": READ_ONLY, "sdrconsole": sdrconsole.READ_ONLY}


def urls_path(cfg):
    """Where the drop-down list of receiver URLs is stored."""
    custom = cfg["server"].get("urls_file")
    if custom:
        return Path(custom).expanduser()
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "winSync" / "urls.json"


def clean_urls(items):
    out = []
    if isinstance(items, list):
        for u in items:
            if isinstance(u, str) and u.strip() and u.strip() not in out:
                out.append(u.strip())
    return out[:MAX_URLS]


def load_urls(path):
    try:
        with open(path, encoding="utf-8") as f:
            return clean_urls(json.load(f))
    except (OSError, ValueError):
        return []


def save_urls(path, urls):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(urls, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("could not save receiver list to %s: %s", path, e)


class State:
    """Mutable runtime state (aiohttp apps must not have their own keys reassigned after start)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.clients = set()
        self.freq_hz = None
        self.cat_mode = None
        self.cat_connected = False
        self.tx = False
        self.vfo = 0           # VFO being followed: 0 = A / RX1, 1 = B / RX2
        self.rx2_on = False
        self.follow = cfg["sync"]["follow"] if cfg["sync"]["follow"] in FOLLOW_MODES else "auto"
        source = cfg["cat"].get("source")
        self.source = source if source in SOURCES else "thetis"
        self.control = cfg["sync"]["control"] if cfg["sync"]["control"] in CONTROL_MODES else "radio"
        if self.source in READ_ONLY_SOURCES and self.control != "radio":
            log.warning("control = %r ignored: %s", self.control, READ_ONLY_SOURCES[self.source])
            self.control = "radio"
        self.control_note = None   # lasting reason the Kiwi can't change the radio
        self.cat = None
        # SDR Console (QO-100) has a receiver of its own; the others use [kiwi] url
        url = (self.source == "sdrconsole" and cfg["sdrconsole"].get("url")) or cfg["kiwi"]["url"]
        self.target = normalize_target(url) if url else None
        self.urls_file = urls_path(cfg)
        self.urls = load_urls(self.urls_file)


STATE = web.AppKey("state", State)

DEFAULTS = {
    "server": {"host": "127.0.0.1", "port": 8090, "urls_file": ""},
    "cat": {"source": "thetis", "host": "127.0.0.1", "port": 13013},
    "omnirig": {"rig": 1},
    "sdrconsole": {"port": "COM23", "baud": 57600, "url": "https://eshail.batc.org.uk/nb/"},
    "kiwi": {"url": ""},
    "sync": {"enabled": True, "zoom_on_band_change": True, "mute_on_tx": False, "follow": "auto",
             "control": "radio", "dry_run": False, "diag": False},
    "modes": {},
}


def load_config(path):
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    if path and Path(path).exists():
        with open(path, "rb") as f:
            for section, values in tomllib.load(f).items():
                cfg.setdefault(section, {}).update(values)
    return cfg


def same_origin(request):
    """Reject cross-site requests (a random web page must not be able to drive winSync)."""
    origin = request.headers.get("Origin")
    if not origin:
        return True
    return URL(origin).authority == request.host


def snapshot(st):
    freq, mode = st.freq_hz, st.cat_mode
    return {
        "type": "state",
        "cat_connected": st.cat_connected,
        "tx": st.tx,
        "rx": st.vfo,
        "vfo": "B" if st.vfo == 1 else "A",
        "rx2_on": st.rx2_on,
        "follow": st.follow,
        "control": st.control,
        "source": st.source,
        "radio_name": getattr(st.cat, "radio_name", None),   # SDR Console's NA; answer
        "control_note": st.control_note,
        "freq_hz": freq,
        "cat_mode": mode,
        "mode_label": THETIS_LABELS.get(mode) if mode is not None else None,
        "kiwi_mode": thetis_to_kiwi(mode, st.cfg["modes"]) if mode is not None else None,
        "band": band_for(freq) if freq is not None else None,
    }


async def broadcast(st):
    msg = json.dumps(snapshot(st))
    for ws in list(st.clients):
        try:
            await ws.send_str(msg)
        except ConnectionError:
            st.clients.discard(ws)


@web.middleware
async def no_stale_ui(request, handler):
    """winSync's own page and static files must revalidate, or a browser keeps showing an old UI
    after an upgrade. (Proxied receiver files are left alone.) Static files still get 304s."""
    resp = await handler(request)
    if request.path.startswith("/_winsync/") and not resp.prepared:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def build_app(cfg):
    app = web.Application(middlewares=[no_stale_ui])
    st = State(cfg)
    app[STATE] = st

    def on_state(freq, mode, tx, vfo, rx2_on):
        st.freq_hz, st.cat_mode, st.tx, st.vfo, st.rx2_on = freq, mode, tx, vfo, rx2_on
        log.info("radio: RX%d (VFO %s) %s Hz %s %s%s", vfo + 1, "B" if vfo else "A", freq,
                 THETIS_LABELS.get(mode, mode), "TX" if tx else "RX", "" if rx2_on else ", RX2 off")
        asyncio.create_task(broadcast(st))

    def on_status(connected):
        st.cat_connected = connected
        asyncio.create_task(broadcast(st))

    def on_note(note):
        st.control_note = note
        if note and st.source == "thetis":     # OmniRig logs its own notes
            log.warning("Kiwi -> radio: %s", note)
        asyncio.create_task(broadcast(st))

    async def startup(app):
        app[SESSION] = aiohttp.ClientSession(auto_decompress=False,
                                             cookie_jar=aiohttp.DummyCookieJar())
        if st.source == "omnirig":
            log.info("CAT source: OmniRig, Rig %s (read-only)", cfg["omnirig"]["rig"])
            st.cat = cat = OmniRigSource(cfg["omnirig"]["rig"], on_state, on_status,
                                         follow=st.follow, on_note=on_note)
        elif st.source == "sdrconsole":
            sc = cfg["sdrconsole"]
            log.info("CAT source: SDR Console on %s, %s baud (read-only)", sc["port"], sc["baud"])
            st.cat = cat = sdrconsole.SdrConsoleSource(sc["port"], int(sc["baud"]), on_state,
                                                       on_status, on_note=on_note)
        else:
            st.cat = cat = CatClient(cfg["cat"]["host"], cfg["cat"]["port"], on_state, on_status,
                                     follow=st.follow, control=st.control, on_note=on_note)
        cat.start()
        yield
        await cat.stop()
        await app[SESSION].close()

    app.cleanup_ctx.append(startup)

    async def index(request):
        return web.FileResponse(STATIC_DIR / "index.html")

    async def ws_handler(request):
        if not same_origin(request):
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        st.clients.add(ws)
        await ws.send_str(json.dumps(snapshot(st)))
        try:
            async for _ in ws:  # browser->server messages are not used
                pass
        finally:
            st.clients.discard(ws)
        return ws

    async def get_config(request):
        return web.json_response({"target": st.target, "sync": cfg["sync"], "cat": cfg["cat"],
                                  "source": st.source})

    async def set_follow(request):
        if not same_origin(request):
            raise web.HTTPForbidden()
        try:
            follow = (await request.json())["follow"]
            if follow not in FOLLOW_MODES:
                raise ValueError(f"follow must be one of {', '.join(FOLLOW_MODES)}")
        except (ValueError, KeyError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        st.follow = follow
        log.info("following %s", "the last-tuned VFO" if follow == "auto" else f"VFO {follow}")
        if st.cat:
            st.cat.set_follow(follow)
        await broadcast(st)
        return web.json_response({"follow": follow})

    async def set_control(request):
        if not same_origin(request):
            raise web.HTTPForbidden()
        try:
            control = (await request.json())["control"]
            if control not in CONTROL_MODES:
                raise ValueError(f"control must be one of {', '.join(CONTROL_MODES)}")
        except (ValueError, KeyError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        if st.source in READ_ONLY_SOURCES and control != "radio":
            return web.json_response({"error": READ_ONLY_SOURCES[st.source]}, status=400)
        st.control = control
        log.info("control: %s", {"radio": "radio controls Kiwi", "kiwi": "Kiwi controls radio",
                                 "both": "both ways"}[control])
        if st.cat:
            st.cat.set_control(control)
        await broadcast(st)
        return web.json_response({"control": control})

    async def tune(request):
        """Kiwi -> radio: the user tuned the Kiwi. Moves the followed VFO if that is allowed now
        (see CatClient.request_tune), otherwise says why the radio was not moved."""
        if not same_origin(request):
            raise web.HTTPForbidden()
        try:
            body = await request.json()
            freq, kiwi_mode = body["freq_hz"], body.get("kiwi_mode")
            if type(freq) is not int or not 0 < freq < 10**10:
                raise ValueError("freq_hz must be a whole number of Hz")
            if kiwi_mode is not None and not isinstance(kiwi_mode, str):
                raise ValueError("kiwi_mode must be a string")
        except (ValueError, KeyError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        mode = kiwi_to_thetis(kiwi_mode, st.cat_mode, cfg["modes"])
        if cfg["sync"]["dry_run"]:
            # Testing aid: decide exactly as for real, but never send anything to the radio.
            reason = st.cat._refusal(st.cat.vfo, freq, mode) if st.cat else "the radio is not connected"
            log.info("DRY RUN: Kiwi -> radio %s Hz mode %s: %s", freq, mode,
                     reason or "would have moved the radio")
            return web.json_response({"moved": False, "out_of_band": bool(reason) and
                                      "UK amateur bands" in reason,
                                      "message": f"Dry run, radio not moved: {reason or 'would have moved it'}"})
        reason = st.cat.request_tune(freq, mode) if st.cat else "the radio is not connected"
        if reason:
            log.info("Kiwi -> radio: not moved to %s Hz: %s", freq, reason)
            return web.json_response({"moved": False, "out_of_band": "UK amateur bands" in reason,
                                      "message": f"Radio not moved: {reason}"})
        result_mode = mode if mode is not None else st.cat_mode
        return web.json_response({
            "moved": True, "freq_hz": freq, "band": band_for(freq),
            "kiwi_mode": thetis_to_kiwi(result_mode, cfg["modes"]) if result_mode is not None else None,
        })

    async def diag(request):
        """--diag: the page's own record of what it saw and did, written to this log."""
        if not same_origin(request) or not cfg["sync"]["diag"]:
            raise web.HTTPForbidden()
        try:
            for line in (await request.json())["events"][:200]:
                log.info("page: %s", str(line)[:300])
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "expected {\"events\": [...]}"}, status=400)
        return web.json_response({})

    async def set_target(request):
        if not same_origin(request):
            raise web.HTTPForbidden()
        try:
            st.target = normalize_target((await request.json())["url"])
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        log.info("Kiwi target set to %s", st.target)
        return web.json_response({"target": st.target})

    async def get_urls(request):
        return web.json_response({"urls": st.urls})

    async def put_urls(request):
        if not same_origin(request):
            raise web.HTTPForbidden()
        try:
            body = await request.json()
            items = body["urls"]
            if not isinstance(items, list):
                raise ValueError("urls must be a list")
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "expected {\"urls\": [...]}"}, status=400)
        st.urls = clean_urls(items)
        save_urls(st.urls_file, st.urls)
        return web.json_response({"urls": st.urls})

    async def catch_all(request):
        target = st.target
        if not target:
            return web.Response(status=503, text="winSync: no Kiwi URL set yet.")
        if request.path == "/" and request.headers.get("Sec-Fetch-Dest") == "document":
            raise web.HTTPFound("/_winsync/")  # top-level visits go to the control page
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await proxy_ws(request, target)
        return await proxy_http(request, target)

    app.router.add_get("/_winsync/", index)
    app.router.add_get("/_winsync/ws", ws_handler)
    app.router.add_get("/_winsync/api/config", get_config)
    app.router.add_post("/_winsync/api/target", set_target)
    app.router.add_post("/_winsync/api/follow", set_follow)
    app.router.add_post("/_winsync/api/control", set_control)
    app.router.add_post("/_winsync/api/tune", tune)
    app.router.add_post("/_winsync/api/diag", diag)
    app.router.add_get("/_winsync/api/urls", get_urls)
    app.router.add_put("/_winsync/api/urls", put_urls)
    app.router.add_static("/_winsync/static", STATIC_DIR)
    app.router.add_route("*", "/{tail:.*}", catch_all)
    return app
