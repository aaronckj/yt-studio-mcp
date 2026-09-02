"""One-time Twitch OAuth: mint a user refresh token with channel:manage:broadcast.

Twitch will NOT let an app (client-credentials) token modify a channel, so this
has to be a real user grant. Run once:

    yt-studio-mcp twitch-auth --client-id ... --client-secret ...

Register the app first at https://dev.twitch.tv/console/apps with the OAuth
redirect URL set to exactly http://localhost:8271 (Twitch requires an exact match).
"""

from __future__ import annotations

import http.server
import json
import secrets as pysecrets
import threading
import urllib.parse
import webbrowser
from urllib.request import Request, urlopen

from .secrets import get_store
from .twitch import OAUTH, SCOPES

REDIRECT = "http://localhost:8271"
PORT = 8271


class _Catch(http.server.BaseHTTPRequestHandler):
    code: str | None = None
    state: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        _Catch.code = (q.get("code") or [None])[0]
        _Catch.state = (q.get("state") or [None])[0]
        ok = bool(_Catch.code)
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<h2>Twitch connected. You can close this tab.</h2>" if ok
            else b"<h2>No code returned. Check the app's redirect URL.</h2>"
        )

    def log_message(self, *a) -> None:  # silence the default stderr spam
        return


def authorize_url(client_id: str, state: str) -> str:
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "force_verify": "true",
    })
    return f"{OAUTH}/authorize?{params}"


def exchange_code(client_id: str, client_secret: str, code: str) -> str:
    """Finish the flow from a pasted ?code=... value.

    The listener below only works when the browser runs on the SAME machine --
    the redirect goes to the browser's own localhost. On a headless box, open
    the printed URL anywhere, let it fail to load, and paste the code from the
    address bar into `twitch-auth --code`.
    """
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT,
    }).encode()
    req = Request(f"{OAUTH}/token", data=body, method="POST",
                  headers={"content-type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=20) as resp:
        tok = json.loads(resp.read())
    store = get_store()
    store.set("twitch_client_id", client_id)
    store.set("twitch_client_secret", client_secret)
    store.set("twitch_refresh_token", tok["refresh_token"])
    return "Twitch connected; refresh token stored. scopes: " + ",".join(tok.get("scope") or [])


def run_twitch_auth(client_id: str, client_secret: str) -> str:
    state = pysecrets.token_urlsafe(16)
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "force_verify": "true",
    })
    url = f"{OAUTH}/authorize?{params}"

    srv = http.server.HTTPServer(("localhost", PORT), _Catch)
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()
    print(f"Authorize as the CHANNEL OWNER in the browser:\n{url}\n")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - headless box, the printed URL is the fallback
        pass
    t.join(timeout=300)
    srv.server_close()

    if not _Catch.code:
        raise SystemExit("no authorization code received (timed out after 5 min)")
    if _Catch.state != state:
        raise SystemExit("state mismatch -- aborting, possible CSRF")

    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": _Catch.code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT,
    }).encode()
    req = Request(f"{OAUTH}/token", data=body, method="POST",
                  headers={"content-type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=20) as resp:
        tok = json.loads(resp.read())

    store = get_store()
    store.set("twitch_client_id", client_id)
    store.set("twitch_client_secret", client_secret)
    store.set("twitch_refresh_token", tok["refresh_token"])
    granted = ",".join(tok.get("scope") or [])
    return f"Twitch connected; refresh token stored. scopes: {granted}"
