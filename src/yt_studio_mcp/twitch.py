"""Twitch Helix client: refresh-token auth, channel read/update.

Multistreaming (Aitum/Restream and friends) copies the VIDEO to Twitch but not
the METADATA -- Twitch keeps whatever title and category were last set on the
channel. So scheduling a YouTube broadcast is only half the setup; this module
is the other half.

Credentials live in the same secret store as the Google ones (YT_MCP_SECRETS):

    twitch_client_id       from a Twitch application (dev.twitch.tv/console/apps)
    twitch_client_secret   same application
    twitch_refresh_token   user grant carrying `channel:manage:broadcast`

An app (client-credentials) token CANNOT modify a channel -- Helix requires a
USER token for that scope. Run `yt-studio-mcp twitch-auth` once to mint it.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .secrets import get_store

HELIX = "https://api.twitch.tv/helix"
OAUTH = "https://id.twitch.tv/oauth2"
SCOPES = "channel:manage:broadcast"


class TwitchNotConfigured(RuntimeError):
    """Raised when Twitch credentials are absent -- actionable, not a crash."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(
            "Twitch is not configured; missing "
            + ", ".join(missing)
            + ". Create an app at https://dev.twitch.tv/console/apps, then run "
            "`yt-studio-mcp twitch-auth --client-id ... --client-secret ...`."
        )


class TwitchError(RuntimeError):
    pass


def _redact(url: str) -> str:
    """Never let a token or secret reach an exception message."""
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    # REDACTED, not "<redacted>": urlencode would percent-encode the angle
    # brackets and the message becomes unreadable in a log.
    safe = [(k, "REDACTED" if "token" in k or "secret" in k else v) for k, v in q]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(safe), "")
    )


def _http(url: str, *, method: str = "GET", headers: dict | None = None,
          data: bytes | None = None, timeout: int = 15) -> dict:
    req = Request(url, method=method, data=data, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    # ORDER MATTERS: URLError is an OSError; a bare TimeoutError is an OSError
    # but NOT a URLError. Flipping these silently swallows timeouts.
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise TwitchError(f"HTTP {exc.code} from {_redact(url)}: {body}") from exc
    except URLError as exc:
        raise TwitchError(f"cannot reach {_redact(url)}: {exc.reason}") from exc
    except OSError as exc:
        raise TwitchError(f"network error on {_redact(url)}: {exc}") from exc
    return json.loads(raw) if raw else {}


def _creds() -> tuple[str, str, str]:
    store = get_store()
    cid = os.environ.get("TWITCH_CLIENT_ID") or store.get("twitch_client_id")
    sec = os.environ.get("TWITCH_CLIENT_SECRET") or store.get("twitch_client_secret")
    ref = os.environ.get("TWITCH_REFRESH_TOKEN") or store.get("twitch_refresh_token")
    missing = [n for n, v in (("twitch_client_id", cid), ("twitch_client_secret", sec),
                              ("twitch_refresh_token", ref)) if not v]
    if missing:
        raise TwitchNotConfigured(missing)
    return cid, sec, ref


class Twitch:
    """Thin Helix wrapper. One access token per instance."""

    def __init__(self) -> None:
        self.client_id, self._secret, self._refresh = _creds()
        self._token: str | None = None
        self._user: dict | None = None

    def _access_token(self) -> str:
        if self._token:
            return self._token
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self._refresh,
            "client_id": self.client_id,
            "client_secret": self._secret,
        }).encode()
        res = _http(f"{OAUTH}/token", method="POST", data=body,
                    headers={"content-type": "application/x-www-form-urlencoded"})
        self._token = res["access_token"]
        # Twitch rotates refresh tokens; persist the new one or the next run fails.
        new_refresh = res.get("refresh_token")
        if new_refresh and new_refresh != self._refresh:
            try:
                get_store().set("twitch_refresh_token", new_refresh)
                self._refresh = new_refresh
            except NotImplementedError:
                pass  # env backend is read-only; the old token still works this run
        return self._token

    def _headers(self) -> dict:
        return {"Client-Id": self.client_id,
                "Authorization": f"Bearer {self._access_token()}"}

    def me(self) -> dict:
        if self._user is None:
            data = _http(f"{HELIX}/users", headers=self._headers())
            items = data.get("data") or []
            if not items:
                raise TwitchError("token resolved to no user; re-run `yt-studio-mcp twitch-auth`")
            self._user = items[0]
        return self._user

    def broadcaster_id(self) -> str:
        return self.me()["id"]

    def get_channel(self) -> dict:
        bid = self.broadcaster_id()
        data = _http(f"{HELIX}/channels?broadcaster_id={bid}", headers=self._headers())
        items = data.get("data") or []
        return items[0] if items else {}

    def find_category(self, name: str) -> dict | None:
        """Exact-ish game lookup. Twitch needs a game_id, not a name."""
        q = urllib.parse.urlencode({"name": name})
        data = _http(f"{HELIX}/games?{q}", headers=self._headers())
        items = data.get("data") or []
        if items:
            return items[0]
        q = urllib.parse.urlencode({"query": name, "first": 10})
        data = _http(f"{HELIX}/search/categories?{q}", headers=self._headers())
        items = data.get("data") or []
        for it in items:
            if it["name"].lower() == name.lower():
                return it
        return items[0] if items else None

    def modify_channel(self, title: str | None = None, game_id: str | None = None,
                       tags: list[str] | None = None) -> None:
        body: dict = {}
        if title is not None:
            body["title"] = title
        if game_id is not None:
            body["game_id"] = game_id
        if tags is not None:
            body["tags"] = tags
        if not body:
            return
        bid = self.broadcaster_id()
        headers = self._headers() | {"content-type": "application/json"}
        # PATCH /channels returns 204 with an empty body on success.
        _http(f"{HELIX}/channels?broadcaster_id={bid}", method="PATCH",
              headers=headers, data=json.dumps(body).encode())


def set_channel(title: str | None, game_name: str | None,
                tags: list[str] | None = None) -> dict:
    """Set Twitch title/category. Returns what changed, for reporting."""
    tw = Twitch()
    before = tw.get_channel()
    game_id = None
    resolved = None
    if game_name:
        cat = tw.find_category(game_name)
        if not cat:
            raise TwitchError(f"no Twitch category matches {game_name!r}")
        game_id, resolved = cat["id"], cat["name"]
    tw.modify_channel(title=title, game_id=game_id, tags=tags)
    after = tw.get_channel()
    return {
        "login": tw.me().get("login"),
        "title_before": before.get("title"),
        "title_after": after.get("title"),
        "category_before": before.get("game_name"),
        "category_after": after.get("game_name"),
        "category_requested": game_name,
        "category_resolved": resolved,
    }
