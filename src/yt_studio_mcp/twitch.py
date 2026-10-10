"""Twitch Helix client: refresh-token auth, channel read/update.

Multistreaming (Aitum/Restream and friends) copies the VIDEO to Twitch but not
the METADATA -- Twitch keeps whatever title and category were last set on the
channel. So scheduling a YouTube broadcast is only half the setup; this module
is the other half.

Credentials live in the same secret store as the Google ones (YT_MCP_SECRETS):

    twitch_client_id       from a Twitch application (dev.twitch.tv/console/apps)
    twitch_client_secret   same application
    twitch_refresh_token   user grant carrying scopes() (below)

An app (client-credentials) token CANNOT modify a channel -- Helix requires a
USER token for that scope. Run `yt-studio-mcp twitch-auth` once to mint it.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .secrets import get_store

HELIX = "https://api.twitch.tv/helix"
OAUTH = "https://id.twitch.tv/oauth2"
# What the package asks for by default: least privilege for what it does
# (set the channel title/category, start and cancel raids).
DEFAULT_SCOPES: tuple[str, ...] = (
    "channel:manage:broadcast",
    "channel:manage:raids",
)
EXTENDED_ENV = "YT_MCP_TWITCH_EXTENDED_SCOPES"
# Opt-in with YT_MCP_TWITCH_EXTENDED_SCOPES=1 in the environment that builds the
# authorize URL (and the one that runs twitch_status, so "missing" is judged
# against the same list).
# Owner, 2026-10-10: "give it all the twitch access you can".
# Every broadcaster-user scope Twitch documents, one per line, MINUS the ones in
# EXCLUDED_SCOPES. Changing the requested list means the owner re-approves once
# (twitch_authorize_url -> twitch_finish_auth); twitch_status says which are missing.
EXTENDED_SCOPES: tuple[str, ...] = (
    "analytics:read:extensions",
    "analytics:read:games",
    "bits:read",
    "channel:bot",
    "channel:edit:commercial",
    "channel:manage:ads",
    "channel:manage:broadcast",
    "channel:manage:extensions",
    "channel:manage:guest_star",
    "channel:manage:moderators",
    "channel:manage:polls",
    "channel:manage:predictions",
    "channel:manage:raids",
    "channel:manage:redemptions",
    "channel:manage:schedule",
    "channel:manage:videos",
    "channel:manage:vips",
    "channel:moderate",
    "channel:read:ads",
    "channel:read:charity",
    "channel:read:editors",
    "channel:read:goals",
    "channel:read:guest_star",
    "channel:read:hype_train",
    "channel:read:polls",
    "channel:read:predictions",
    "channel:read:redemptions",
    "channel:read:subscriptions",
    "channel:read:vips",
    "chat:edit",
    "chat:read",
    "clips:edit",
    "moderation:read",
    "moderator:manage:announcements",
    "moderator:manage:automod",
    "moderator:manage:automod_settings",
    "moderator:manage:banned_users",
    "moderator:manage:blocked_terms",
    "moderator:manage:chat_messages",
    "moderator:manage:chat_settings",
    "moderator:manage:guest_star",
    "moderator:manage:shield_mode",
    "moderator:manage:shoutouts",
    "moderator:manage:unban_requests",
    "moderator:manage:warnings",
    "moderator:read:automod_settings",
    "moderator:read:banned_users",
    "moderator:read:blocked_terms",
    "moderator:read:chat_messages",
    "moderator:read:chat_settings",
    "moderator:read:chatters",
    "moderator:read:followers",
    "moderator:read:guest_star",
    "moderator:read:moderators",
    "moderator:read:shield_mode",
    "moderator:read:shoutouts",
    "moderator:read:suspicious_users",
    "moderator:read:unban_requests",
    "moderator:read:vips",
    "moderator:read:warnings",
    "user:bot",
    "user:edit",
    "user:edit:broadcast",
    "user:manage:blocked_users",
    "user:manage:chat_color",
    "user:read:blocked_users",
    "user:read:broadcast",
    "user:read:chat",
    "user:read:emotes",
    "user:read:follows",
    "user:read:moderated_channels",
    "user:read:subscriptions",
    "user:write:chat",
)
# Owner, 2026-10-10: "ok we can pull those 3 back". Never requested, because a
# leaked token would then expose the stream key (anyone could stream as us), the
# account email, or let someone read and send private messages as us. The
# whisper scopes (legacy IRC and Helix read) go for the same reason.
EXCLUDED_SCOPES: tuple[str, ...] = (
    "channel:read:stream_key",
    "user:read:email",
    "user:manage:whispers",
    "user:read:whispers",
    "whispers:read",
    "whispers:edit",
)


def scope_list() -> tuple[str, ...]:
    """The scopes to request, read at call time: EXTENDED only when the env var is exactly "1"."""
    return EXTENDED_SCOPES if os.environ.get(EXTENDED_ENV) == "1" else DEFAULT_SCOPES


def scopes() -> str:
    return " ".join(scope_list())


SCOPES = " ".join(DEFAULT_SCOPES)  # back-compat constant; use scopes() for the live value
RAID_SCOPE = "channel:manage:raids"
REAPPROVE_HINT = (
    "Re-approve Twitch to enable raids: open the link from twitch_authorize_url as the "
    "channel owner, approve, then pass the ?code=... to twitch_finish_auth."
)
# Fields Helix may return on /users that must never leave this module.
_PRIVATE_USER_FIELDS = frozenset({"email"})
_LOGIN = re.compile(r"[a-z0-9_]{3,25}")


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
    """status / message carry the HTTP code and Twitch's own message, when there was one."""

    def __init__(self, msg: str, status: int | None = None, message: str = "") -> None:
        super().__init__(msg)
        self.status = status
        self.message = message


class TwitchRaidError(TwitchError):
    """A raid call that failed for a reason the caller can act on.

    code: invalid_login | not_found | self_raid | missing_scope | rate_limited |
          already_raiding | not_live | target_refuses | no_pending_raid | twitch_error
    """

    def __init__(self, code: str, msg: str, hint: str = "", status: int | None = None) -> None:
        super().__init__(msg, status=status, message=msg)
        self.code = code
        self.hint = hint

    def as_dict(self) -> dict:
        return {"ok": False, "code": self.code, "error": str(self), "hint": self.hint,
                "status": self.status}


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
        try:
            message = str(json.loads(body).get("message") or "")
        except (ValueError, AttributeError):
            message = body
        raise TwitchError(f"HTTP {exc.code} from {_redact(url)}: {body}",
                          status=exc.code, message=message) from exc
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
            self._user = {k: v for k, v in items[0].items() if k not in _PRIVATE_USER_FIELDS}
        return self._user

    def scope_report(self) -> dict:
        """Granted vs requested scopes, from /oauth2/validate (needs the `OAuth` prefix)."""
        res = _http(f"{OAUTH}/validate",
                    headers={"Authorization": f"OAuth {self._access_token()}"})
        granted = sorted(res.get("scopes") or [])
        requested = scope_list()
        missing = [s for s in requested if s not in granted]
        raids = RAID_SCOPE in granted
        hint = ""
        if missing:
            hint = (REAPPROVE_HINT if not raids else
                    "Re-approve Twitch to grant the missing scopes: twitch_authorize_url, "
                    "approve as the channel owner, then twitch_finish_auth(code=...).")
        return {"granted": granted, "requested": list(requested), "missing": missing,
                "extra": [s for s in granted if s not in requested],
                "raids_enabled": raids, "hint": hint}

    def users_by_login(self, logins: list[str]) -> list[dict]:
        """Public /users rows for up to 100 logins (private fields stripped)."""
        if not logins:
            return []
        q = urllib.parse.urlencode([("login", x) for x in logins])
        data = _http(f"{HELIX}/users?{q}", headers=self._headers())
        return [{k: v for k, v in u.items() if k not in _PRIVATE_USER_FIELDS}
                for u in data.get("data") or []]

    def live_streams(self, logins: list[str]) -> list[dict]:
        """Live /streams rows for up to 100 logins; offline channels are simply absent."""
        if not logins:
            return []
        q = urllib.parse.urlencode([("user_login", x) for x in logins] + [("first", 100)])
        return _http(f"{HELIX}/streams?{q}", headers=self._headers()).get("data") or []

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
        if items:
            # Never fall back to the first fuzzy hit: that silently put a live
            # channel under the wrong game. Make the caller pick.
            raise TwitchError(
                f"no exact Twitch category named {name!r}; did you mean one of: "
                + ", ".join(repr(i["name"]) for i in items[:10])
            )
        return None

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


# ---- raids ------------------------------------------------------------------------------------
# Twitch runs a raid after a ~90 s countdown (or when the owner presses "Raid Now"),
# and drops it if our stream ends first. Helix allows 10 raid requests per 10 minutes.

def valid_login(login) -> str | None:
    """Lower-cased Twitch login, or None. Logins are [A-Za-z0-9_]{3,25}; '@' is tolerated."""
    if not isinstance(login, str):
        return None
    x = login.strip().lstrip("@").lower()
    return x if _LOGIN.fullmatch(x) else None


def _raid_error(exc: TwitchError, action: str) -> TwitchRaidError:
    status, msg = exc.status, (exc.message or "").strip()
    low = msg.lower()
    if status == 401:
        return TwitchRaidError("missing_scope",
                               f"Twitch refused the {action}: {msg or 'unauthorized'}",
                               REAPPROVE_HINT, status)
    if status == 429:
        return TwitchRaidError("rate_limited", f"too many raid requests: {msg}",
                               "Twitch allows 10 raid requests per 10 minutes; wait a few "
                               "minutes and try again.", status)
    if status == 409:
        return TwitchRaidError("already_raiding", f"a raid is already pending: {msg}",
                               "Cancel the pending raid first, or let it go out.", status)
    if status == 404:
        if action == "cancel":
            return TwitchRaidError("no_pending_raid", f"nothing to cancel: {msg}",
                                   "There is no pending raid; it may already have gone out.",
                                   status)
        return TwitchRaidError("not_found", f"Twitch could not find that channel: {msg}",
                               "Check the login spelling.", status)
    if status == 400 and ("not live" in low or "offline" in low or "not streaming" in low):
        return TwitchRaidError("not_live", f"we are not live on Twitch: {msg}",
                               "A raid can only start while our Twitch stream is live.", status)
    if status == 400 and ("allow" in low or "setting" in low or "cannot raid" in low
                          or "can't raid" in low or "blocked" in low or "not accept" in low):
        return TwitchRaidError("target_refuses", f"that channel is not accepting this raid: {msg}",
                               "They only accept raids from friends/followed channels, or "
                               "raids are switched off; pick someone else.", status)
    return TwitchRaidError("twitch_error", f"Twitch {action} failed (HTTP {status}): {msg}",
                           "Try again in a minute; if it keeps failing, raid from the "
                           "Twitch dashboard (/raid <login> in chat).", status)


def raid_start(to_login: str, tw: Twitch | None = None) -> dict:
    """Start a raid from our channel to `to_login`. Twitch sends it after ~90 s."""
    login = valid_login(to_login)
    if not login:
        raise TwitchRaidError("invalid_login", f"not a Twitch login: {to_login!r}",
                              "Logins are 3-25 letters, digits or underscores.")
    tw = tw or Twitch()
    try:
        users = tw.users_by_login([login])
        if not users:
            raise TwitchRaidError("not_found", f"no Twitch channel named {login!r}",
                                  "Check the login spelling.")
        target = users[0]
        me = tw.me()
        if target["id"] == me["id"]:
            raise TwitchRaidError("self_raid", "a channel cannot raid itself",
                                  "Pick another channel.")
        q = urllib.parse.urlencode({"from_broadcaster_id": me["id"],
                                    "to_broadcaster_id": target["id"]})
        res = _http(f"{HELIX}/raids?{q}", method="POST", headers=tw._headers())
    except TwitchRaidError:
        raise
    except TwitchError as exc:
        if exc.status is None:
            raise
        raise _raid_error(exc, "raid") from None
    row = (res.get("data") or [{}])[0]
    return {"ok": True, "from": me.get("login"), "to": target.get("login") or login,
            "to_display": target.get("display_name") or login, "to_id": target["id"],
            "created_at": row.get("created_at"), "is_mature": bool(row.get("is_mature"))}


def raid_cancel(tw: Twitch | None = None) -> dict:
    """Cancel our pending raid (only possible during the countdown)."""
    tw = tw or Twitch()
    try:
        me = tw.me()
        q = urllib.parse.urlencode({"broadcaster_id": me["id"]})
        # DELETE /raids returns 204 with an empty body on success.
        _http(f"{HELIX}/raids?{q}", method="DELETE", headers=tw._headers())
    except TwitchError as exc:
        if exc.status is None:
            raise
        raise _raid_error(exc, "cancel") from None
    return {"ok": True, "cancelled": True, "from": me.get("login")}


def raid_candidates(logins: list[str], tw: Twitch | None = None) -> dict:
    """Live-now status for each login: game, viewers, title. Invalid logins are listed, not sent."""
    seen: list[str] = []
    invalid: list[str] = []
    for raw in logins or []:
        x = valid_login(raw)
        if x is None:
            invalid.append(str(raw))
        elif x not in seen:
            seen.append(x)
    if not seen:
        return {"channels": [], "invalid": invalid}
    tw = tw or Twitch()
    users: dict[str, dict] = {}
    live: dict[str, dict] = {}
    for n in range(0, len(seen), 100):
        chunk = seen[n:n + 100]
        for u in tw.users_by_login(chunk):
            users[(u.get("login") or "").lower()] = u
        for st in tw.live_streams(chunk):
            live[(st.get("user_login") or "").lower()] = st
    out = []
    for login in seen:
        u, st = users.get(login) or {}, live.get(login)
        out.append({
            "login": login,
            "display_name": u.get("display_name") or login,
            "id": u.get("id"),
            "exists": bool(u),
            "broadcaster_type": u.get("broadcaster_type") or "",
            "live": bool(st),
            "game_name": (st or {}).get("game_name"),
            "viewer_count": (st or {}).get("viewer_count"),
            "title": (st or {}).get("title"),
            "started_at": (st or {}).get("started_at"),
        })
    return {"channels": out, "invalid": invalid}
