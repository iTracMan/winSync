"""Reverse proxy for a single user-chosen KiwiSDR, served same-origin so the control page can
call into the Kiwi page's JavaScript (the winSync equivalent of CatSync's script injection).

Kiwi is proxied at the root path (its WebSocket URLs are absolute, e.g. /<ts>/SND); winSync's own
routes live under /_winsync/ so they never collide.
"""
import asyncio
import logging
import re
import zlib

import aiohttp
from aiohttp import web
from yarl import URL

log = logging.getLogger("winSync.proxy")
SESSION = web.AppKey("session", aiohttp.ClientSession)

BRIDGE_TAG = '<script src="/_winsync/static/bridge.js"></script>'
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade", "content-length",
}
# Headers we never forward upstream (browser/host specific, or we set our own).
DROP_REQ = HOP_BY_HOP | {"host", "origin", "referer", "accept-encoding", "sec-websocket-key",
                          "sec-websocket-version", "sec-websocket-extensions",
                          "sec-websocket-protocol"}
_HEAD_RE = re.compile(r"<head[^>]*>", re.I)


def normalize_target(url):
    """Reduce a user-entered Kiwi URL to scheme://host[:port]. Raises ValueError if unusable."""
    url = url.strip()
    if "://" not in url:
        url = "http://" + url
    u = URL(url)
    if u.scheme not in ("http", "https") or not u.host:
        raise ValueError("Kiwi URL must be http(s)://host[:port]")
    return str(u.origin())


def page_target(url):
    """A user-entered receiver URL -> (origin for the proxy, path of the receiver page, page URL).
    The whole site is proxied at the root (WebSDR pages use root paths such as /~~stream even when
    the page itself is elsewhere), but the page may live below it, e.g. Goonhilly's QO-100 WebSDR at
    https://eshail.batc.org.uk/nb/. No query string: Kiwi appends the page's query to its WebSocket
    URLs. With path "/" the page URL is the bare origin, as before."""
    origin = normalize_target(url)
    url = url.strip()
    path = URL(url if "://" in url else "http://" + url).raw_path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return origin, path, origin if path == "/" else origin + path


def inject_bridge(html):
    """Insert the bridge script tag into a Kiwi HTML page."""
    m = _HEAD_RE.search(html)
    if m:
        return html[: m.end()] + BRIDGE_TAG + html[m.end():]
    return BRIDGE_TAG + html


def _decode(body, encoding):
    enc = (encoding or "").lower()
    if enc == "gzip":
        return zlib.decompress(body, 16 + zlib.MAX_WBITS)
    if enc == "deflate":
        return zlib.decompress(body)
    if enc in ("", "identity"):
        return body
    raise ValueError(f"unsupported content-encoding {enc}")


async def proxy_http(request, target):
    session = request.app[SESSION]
    base = URL(target)
    url = base.join(URL(request.raw_path))
    headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQ}
    headers["Host"] = base.authority
    headers["Accept-Encoding"] = "gzip, deflate"
    body = await request.read()
    try:
        async with session.request(request.method, url, headers=headers, data=body or None,
                                   allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=30)) as up:
            data = await up.read()
            resp_headers = {k: v for k, v in up.headers.items() if k.lower() not in HOP_BY_HOP}
            ctype = up.headers.get("Content-Type", "")
            if "text/html" in ctype.lower() and up.status == 200:
                try:
                    data = _decode(data, up.headers.get("Content-Encoding"))
                    resp_headers = {k: v for k, v in resp_headers.items()
                                    if k.lower() != "content-encoding"}
                    data = inject_bridge(data.decode("utf-8", errors="replace")).encode("utf-8")
                except (ValueError, zlib.error) as e:
                    log.warning("cannot inject bridge into %s: %s", url, e)
            loc = resp_headers.get("Location")
            if loc and loc.startswith(target):
                resp_headers["Location"] = loc[len(target):] or "/"
            return web.Response(status=up.status, headers=resp_headers, body=data)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("upstream %s failed: %s", url, e)
        return web.Response(status=502, text=f"winSync: cannot reach Kiwi at {target}: {e}")


async def proxy_ws(request, target):
    session = request.app[SESSION]
    base = URL(target)
    ws_scheme = "wss" if base.scheme == "https" else "ws"
    url = base.join(URL(request.raw_path)).with_scheme(ws_scheme)
    try:
        up = await session.ws_connect(url, headers={"Origin": target}, max_msg_size=0,
                                      timeout=aiohttp.ClientWSTimeout(ws_close=10))
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("upstream ws %s failed: %s", url, e)
        return web.Response(status=502, text=f"winSync: cannot reach Kiwi websocket: {e}")

    down = web.WebSocketResponse(max_msg_size=0)
    await down.prepare(request)

    async def pump(src, dst):
        async for msg in src:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await dst.send_str(msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await dst.send_bytes(msg.data)
            else:
                break

    tasks = [asyncio.create_task(pump(down, up)), asyncio.create_task(pump(up, down))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await up.close()
        await down.close()
    return down
