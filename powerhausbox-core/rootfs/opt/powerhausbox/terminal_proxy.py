"""Standalone aiohttp terminal proxy for ttyd WebSocket support.

Runs on port 7682 (internal only), proxies all requests to ttyd on port 7681.
Token validation is performed against Studio. Flask cannot handle WebSocket,
so this dedicated proxy handles both HTTP and WebSocket traffic for ttyd.
"""

import asyncio
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import aiohttp
from aiohttp import web, ClientSession, WSMsgType

LISTEN_PORT = 7682
TTYD_HOST = "127.0.0.1"
TTYD_PORT = 7681
TTYD_BASE_PATH = "/_powerhausbox/api/terminal"

SECRETS_FILE = Path(os.getenv("SECRETS_FILE", "/data/pairing_secrets.json"))
OPTIONS_FILE = Path(os.getenv("OPTIONS_FILE", "/data/options.json"))
TTYD_CREDENTIAL_FILE = Path("/data/ttyd_credential")

TERMINAL_TOKEN_CACHE_TTL = 60
TERMINAL_TOKEN_CACHE_MAX = 256

# State
_ttyd_credential = ""
_token_cache: dict[str, float] = {}


def _log(msg: str) -> None:
    print(f"[powerhausbox-terminal-proxy] {msg}", flush=True)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _get_studio_base_url() -> str:
    options = _read_json(OPTIONS_FILE)
    url = str(options.get("studio_base_url", "") or "").strip()
    return url.rstrip("/") or os.getenv("STUDIO_BASE_URL", "https://studio.powerhaus.ai").rstrip("/")


def _get_box_api_token() -> str:
    secrets = _read_json(SECRETS_FILE)
    return str(secrets.get("box_api_token", "")).strip()


def _validate_token(token: str) -> bool:
    """Validate terminal token against Studio with local caching."""
    if not token:
        return False

    now = time.time()

    # Prune cache if too large
    if len(_token_cache) > TERMINAL_TOKEN_CACHE_MAX:
        expired = [k for k, v in _token_cache.items() if v <= now]
        for k in expired:
            del _token_cache[k]

    cached = _token_cache.get(token)
    if cached and cached > now:
        return True

    box_token = _get_box_api_token()
    if not box_token:
        return False

    base_url = _get_studio_base_url()
    try:
        payload = json.dumps({"token": token}).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/api/addon/terminal/validate/",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {box_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("valid"):
                _token_cache[token] = now + TERMINAL_TOKEN_CACHE_TTL
                return True
    except Exception:
        pass
    return False


def _ttyd_auth_header() -> dict[str, str]:
    """Build Basic Auth header for internal ttyd credential."""
    if not _ttyd_credential:
        return {}
    cred = base64.b64encode(f"powerhaus:{_ttyd_credential}".encode()).decode()
    return {"Authorization": f"Basic {cred}"}


def _check_token(request: web.Request) -> str | None:
    """Extract and validate token from query string. Returns error message or None."""
    token = request.query.get("token", "")
    if not token:
        return "Terminal token required"
    if not _validate_token(token):
        return "Invalid or expired terminal token"
    return None


async def handle_terminal_ws(request: web.Request) -> web.WebSocketResponse:
    """Proxy WebSocket connections to ttyd."""
    error = _check_token(request)
    if error:
        return web.Response(text=json.dumps({"error": error}), status=401, content_type="application/json")

    ws_client = web.WebSocketResponse(protocols=["tty"])
    await ws_client.prepare(request)

    ttyd_ws_url = f"ws://{TTYD_HOST}:{TTYD_PORT}{TTYD_BASE_PATH}/ws"
    headers = _ttyd_auth_header()

    async with ClientSession() as session:
        try:
            async with session.ws_connect(ttyd_ws_url, protocols=["tty"], headers=headers) as ttyd_ws:
                async def relay_from_ttyd():
                    async for msg in ttyd_ws:
                        if msg.type == WSMsgType.TEXT:
                            await ws_client.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await ws_client.send_bytes(msg.data)
                        elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                            break

                async def relay_from_client():
                    async for msg in ws_client:
                        if msg.type == WSMsgType.TEXT:
                            await ttyd_ws.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await ttyd_ws.send_bytes(msg.data)
                        elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                            break

                await asyncio.gather(relay_from_ttyd(), relay_from_client())
        except Exception as exc:
            _log(f"WebSocket proxy error: {exc}")

    return ws_client


async def handle_terminal_http(request: web.Request) -> web.Response:
    """Proxy HTTP requests to ttyd."""
    error = _check_token(request)
    if error:
        return web.json_response({"error": error}, status=401)

    path = request.match_info.get("path", "")
    ttyd_url = f"http://{TTYD_HOST}:{TTYD_PORT}{TTYD_BASE_PATH}/{path}"
    query = request.query_string
    if query:
        ttyd_url += f"?{query}"

    auth = None
    if _ttyd_credential:
        auth = aiohttp.BasicAuth("powerhaus", _ttyd_credential)

    async with ClientSession(auth=auth) as session:
        try:
            async with session.request(
                method=request.method,
                url=ttyd_url,
                headers={k: v for k, v in request.headers.items()
                         if k.lower() not in ("host", "content-length", "authorization")},
                data=await request.read(),
            ) as resp:
                body = await resp.read()
                return web.Response(
                    body=body,
                    status=resp.status,
                    headers={k: v for k, v in resp.headers.items()
                             if k.lower() not in ("transfer-encoding", "content-encoding")},
                )
        except Exception as exc:
            _log(f"HTTP proxy error: {exc}")
            return web.json_response({"error": "Terminal not available"}, status=502)


def main():
    global _ttyd_credential

    # Load ttyd credential
    try:
        _ttyd_credential = TTYD_CREDENTIAL_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        _log("No ttyd credential found")

    app = web.Application()

    # WebSocket route must come before the catch-all
    app.router.add_route("*", f"{TTYD_BASE_PATH}/ws", handle_terminal_ws)
    app.router.add_route("*", f"{TTYD_BASE_PATH}/{{path:.*}}", handle_terminal_http)
    app.router.add_get(f"{TTYD_BASE_PATH}", handle_terminal_http)

    _log(f"Terminal proxy starting on port {LISTEN_PORT}")
    web.run_app(app, host="127.0.0.1", port=LISTEN_PORT, print=None)


if __name__ == "__main__":
    main()
