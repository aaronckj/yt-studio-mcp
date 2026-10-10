"""Offline tests for Twitch raids and the scope report. No network."""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error
import urllib.parse

import pytest

from yt_studio_mcp import twitch as tw


class FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode() if payload is not None else b""

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _NullStore:
    def get(self, key):
        return None

    def set(self, key, value):
        return None


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "csec")
    monkeypatch.setenv("TWITCH_REFRESH_TOKEN", "rtok")
    monkeypatch.setattr(tw, "get_store", lambda: _NullStore())


ME = {"id": "42", "login": "ourchannel", "display_name": "OurChannel",
      "email": "owner@example.com", "broadcaster_type": ""}


def _http_error(url, code, message):
    body = json.dumps({"error": "x", "status": code, "message": message}).encode()
    return urllib.error.HTTPError(url, code, message, {}, io.BytesIO(body))


def _router(calls, table, errors=None):
    """table: {(METHOD, url fragment): payload}; errors: {(METHOD, frag): (code, message)}."""
    errors = errors or {}

    def fake_urlopen(req, timeout=None):
        method, url = req.method or "GET", req.full_url
        calls.append((method, url, req.data, dict(req.header_items())))
        for (m, frag), (code, msg) in errors.items():
            if m == method and frag in url:
                raise _http_error(url, code, msg)
        for (m, frag), payload in table.items():
            if m == method and frag in url:
                return FakeResp(payload)
        raise AssertionError(f"unexpected {method} {url}")

    return fake_urlopen


def _base(extra=None):
    t = {
        ("POST", "oauth2/token"): {"access_token": "atok", "refresh_token": "rtok"},
        ("GET", "helix/users?login="): {"data": [{"id": "777", "login": "pixel_kid",
                                                   "display_name": "Pixel_Kid"}]},
        ("GET", "helix/users"): {"data": [ME]},
    }
    t.update(extra or {})
    return t


# ---- scopes -----------------------------------------------------------------------------------

SENSITIVE = ("channel:read:stream_key", "user:read:email", "user:manage:whispers",
             "user:read:whispers", "whispers:read", "whispers:edit")


@pytest.fixture()
def extended(monkeypatch):
    monkeypatch.setenv("YT_MCP_TWITCH_EXTENDED_SCOPES", "1")


def test_default_scopes_are_least_privilege(monkeypatch):
    monkeypatch.delenv("YT_MCP_TWITCH_EXTENDED_SCOPES", raising=False)
    assert tw.DEFAULT_SCOPES == ("channel:manage:broadcast", "channel:manage:raids")
    assert tw.scope_list() == tw.DEFAULT_SCOPES
    assert tw.scopes() == "channel:manage:broadcast channel:manage:raids"
    assert tw.SCOPES == tw.scopes(), "the module constant stays the default set"


@pytest.mark.parametrize("val", ["0", "", "yes", "true "])
def test_only_exactly_1_turns_on_extended(monkeypatch, val):
    monkeypatch.setenv("YT_MCP_TWITCH_EXTENDED_SCOPES", val)
    assert tw.scope_list() == tw.DEFAULT_SCOPES


def test_extended_scopes_are_the_full_broadcaster_set(extended):
    req = tw.scope_list()
    assert req == tw.EXTENDED_SCOPES
    assert tw.scopes().split() == list(req)
    assert len(req) == len(set(req)), "no duplicates"
    assert set(tw.DEFAULT_SCOPES) <= set(req)
    for s in ("bits:read", "clips:edit", "moderation:read", "channel:bot", "user:bot",
              "user:read:chat", "user:write:chat", "chat:read", "chat:edit",
              "moderator:manage:shoutouts", "moderator:read:followers",
              "channel:read:subscriptions", "channel:manage:polls",
              "analytics:read:extensions", "analytics:read:games",
              "user:read:follows", "user:read:subscriptions", "user:edit:broadcast"):
        assert s in req, s


@pytest.mark.parametrize("mode", ["default", "extended"])
def test_sensitive_scopes_are_never_requested(monkeypatch, mode):
    """A leaked token must not expose the stream key, the account email or private messages."""
    if mode == "extended":
        monkeypatch.setenv("YT_MCP_TWITCH_EXTENDED_SCOPES", "1")
    else:
        monkeypatch.delenv("YT_MCP_TWITCH_EXTENDED_SCOPES", raising=False)
    from yt_studio_mcp.twitch_auth import authorize_url

    q = urllib.parse.parse_qs(urllib.parse.urlsplit(authorize_url("cid", "st")).query)
    sent = q["scope"][0].split()
    assert sent == list(tw.scope_list())
    for s in SENSITIVE:
        assert s not in tw.scope_list() and s not in sent, s
        assert s not in tw.EXTENDED_SCOPES, s
        assert s in tw.EXCLUDED_SCOPES, s


def test_authorize_url_follows_the_mode_at_call_time(monkeypatch):
    from yt_studio_mcp.twitch_auth import authorize_url

    monkeypatch.delenv("YT_MCP_TWITCH_EXTENDED_SCOPES", raising=False)
    short = urllib.parse.parse_qs(urllib.parse.urlsplit(authorize_url("c", "s")).query)["scope"][0]
    monkeypatch.setenv("YT_MCP_TWITCH_EXTENDED_SCOPES", "1")
    full = urllib.parse.parse_qs(urllib.parse.urlsplit(authorize_url("c", "s")).query)["scope"][0]
    assert short.split() == list(tw.DEFAULT_SCOPES)
    assert full.split() == list(tw.EXTENDED_SCOPES)


def test_scope_report_lists_granted_requested_missing(monkeypatch, extended):
    calls = []
    table = _base({("GET", "oauth2/validate"): {
        "client_id": "cid", "login": "ourchannel", "user_id": "42", "expires_in": 100,
        "scopes": ["channel:manage:broadcast", "some:old:scope"]}})
    monkeypatch.setattr(tw, "urlopen", _router(calls, table))
    rep = tw.Twitch().scope_report()
    assert rep["granted"] == ["channel:manage:broadcast", "some:old:scope"]
    assert rep["requested"] == list(tw.EXTENDED_SCOPES)
    assert "channel:manage:raids" in rep["missing"]
    assert "channel:manage:broadcast" not in rep["missing"]
    assert rep["extra"] == ["some:old:scope"]
    assert rep["raids_enabled"] is False
    assert "re-approve" in rep["hint"].lower()
    v = [c for c in calls if "oauth2/validate" in c[1]][0]
    assert v[3].get("Authorization") == "OAuth atok", "validate uses the OAuth prefix"


def test_scope_report_default_mode_only_needs_the_two(monkeypatch):
    monkeypatch.delenv("YT_MCP_TWITCH_EXTENDED_SCOPES", raising=False)
    table = _base({("GET", "oauth2/validate"): {"scopes": ["channel:manage:broadcast"]}})
    monkeypatch.setattr(tw, "urlopen", _router([], table))
    rep = tw.Twitch().scope_report()
    assert rep["requested"] == list(tw.DEFAULT_SCOPES)
    assert rep["missing"] == ["channel:manage:raids"]
    assert rep["raids_enabled"] is False and "re-approve" in rep["hint"].lower()


def test_scope_report_all_granted(monkeypatch, extended):
    table = _base({("GET", "oauth2/validate"): {"scopes": list(tw.EXTENDED_SCOPES)}})
    monkeypatch.setattr(tw, "urlopen", _router([], table))
    rep = tw.Twitch().scope_report()
    assert rep["missing"] == [] and rep["raids_enabled"] is True and rep["hint"] == ""


def test_status_reports_scopes_and_never_email_or_stream_key(monkeypatch):
    from yt_studio_mcp.server import build_app

    table = _base({
        ("GET", "oauth2/validate"): {"scopes": ["channel:manage:broadcast"]},
        ("GET", "helix/channels"): {"data": [{"title": "t", "game_name": "g"}]},
        ("GET", "streams/key"): {"data": [{"stream_key": "live_SECRETKEY"}]},
    })
    monkeypatch.setattr(tw, "urlopen", _router([], table))
    app = build_app()
    res = asyncio.run(app.call_tool("twitch_status", {}))
    blob = json.dumps(res, default=str)
    assert "owner@example.com" not in blob and "email" not in blob
    assert "SECRETKEY" not in blob and "stream_key" not in blob
    out = json.loads(res[0][0].text) if isinstance(res, tuple) else json.loads(res[0].text)
    assert out["scopes"]["missing"] and "channel:manage:raids" in out["scopes"]["missing"]
    assert out["raids_enabled"] is False
    assert "re-approve" in out["scopes"]["hint"].lower()


def test_no_code_path_reads_the_stream_key():
    import inspect

    from yt_studio_mcp.tools import twitch as tools_twitch

    for mod in (tw, tools_twitch):
        assert "streams/key" not in inspect.getsource(mod)


def test_me_strips_email(monkeypatch):
    monkeypatch.setattr(tw, "urlopen", _router([], _base()))
    assert "email" not in tw.Twitch().me()


# ---- login validation -------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "ab", "a" * 26, "has space", "semi;colon", "x&login=y",
                                 None, 5])
def test_invalid_logins_rejected_before_any_network(monkeypatch, bad):
    def boom(*a, **k):
        raise AssertionError("no network for an invalid login")

    monkeypatch.setattr(tw, "urlopen", boom)
    with pytest.raises(tw.TwitchRaidError) as e:
        tw.raid_start(bad)
    assert e.value.code == "invalid_login"


def test_valid_login_normalised():
    assert tw.valid_login("Pixel_Kid") == "pixel_kid"
    assert tw.valid_login("  @pixel_kid ") == "pixel_kid"
    assert tw.valid_login("no") is None


# ---- raid_start -------------------------------------------------------------------------------

def test_raid_start_resolves_login_and_posts(monkeypatch):
    calls = []
    table = _base({("POST", "helix/raids"): {"data": [{"created_at": "2026-10-10T20:00:00Z",
                                                          "is_mature": False}]}})
    monkeypatch.setattr(tw, "urlopen", _router(calls, table))
    out = tw.raid_start("Pixel_Kid")
    post = [c for c in calls if c[0] == "POST" and "helix/raids" in c[1]]
    assert len(post) == 1
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(post[0][1]).query)
    assert q == {"from_broadcaster_id": ["42"], "to_broadcaster_id": ["777"]}
    assert out == {"ok": True, "from": "ourchannel", "to": "pixel_kid", "to_display": "Pixel_Kid",
                   "to_id": "777", "created_at": "2026-10-10T20:00:00Z", "is_mature": False}


def test_raid_start_unknown_login(monkeypatch):
    calls = []
    table = _base({("GET", "helix/users?login="): {"data": []}})
    monkeypatch.setattr(tw, "urlopen", _router(calls, table))
    with pytest.raises(tw.TwitchRaidError) as e:
        tw.raid_start("nobody_here")
    assert e.value.code == "not_found"
    assert not [c for c in calls if c[0] == "POST" and "raids" in c[1]]


def test_raid_start_refuses_self(monkeypatch):
    table = _base({("GET", "helix/users?login="): {"data": [{"id": "42", "login": "ourchannel"}]}})
    monkeypatch.setattr(tw, "urlopen", _router([], table))
    with pytest.raises(tw.TwitchRaidError) as e:
        tw.raid_start("ourchannel")
    assert e.value.code == "self_raid"


@pytest.mark.parametrize("status,message,code", [
    (401, "Missing scope: channel:manage:raids", "missing_scope"),
    (429, "Too Many Requests", "rate_limited"),
    (409, "The broadcaster is already in the process of raiding another broadcaster",
     "already_raiding"),
    (400, "The broadcaster is not live", "not_live"),
    (400, "The channel is offline", "not_live"),
    (400, "The targeted channel's settings do not allow you to raid them", "target_refuses"),
    (400, "Cannot raid this broadcaster", "target_refuses"),
    (404, "The targeted channel was not found", "not_found"),
    (500, "Internal Server Error", "twitch_error"),
])
def test_raid_start_maps_twitch_errors(monkeypatch, status, message, code):
    table = _base()
    errors = {("POST", "helix/raids"): (status, message)}
    monkeypatch.setattr(tw, "urlopen", _router([], table, errors))
    with pytest.raises(tw.TwitchRaidError) as e:
        tw.raid_start("pixel_kid")
    assert e.value.code == code
    assert e.value.status == status
    assert e.value.hint
    if code == "missing_scope":
        assert "re-approve" in e.value.hint.lower()
    if code == "rate_limited":
        assert "10" in e.value.hint


def test_raid_error_never_carries_token(monkeypatch):
    errors = {("POST", "helix/raids"): (401, "Invalid OAuth token")}
    monkeypatch.setattr(tw, "urlopen", _router([], _base(), errors))
    with pytest.raises(tw.TwitchRaidError) as e:
        tw.raid_start("pixel_kid")
    assert "atok" not in str(e.value) and "rtok" not in str(e.value) and "csec" not in str(e.value)
    assert e.value.as_dict()["ok"] is False and e.value.as_dict()["code"] == "missing_scope"


# ---- raid_cancel ------------------------------------------------------------------------------

def test_raid_cancel_deletes_ours(monkeypatch):
    calls = []
    table = _base({("DELETE", "helix/raids"): None})
    monkeypatch.setattr(tw, "urlopen", _router(calls, table))
    out = tw.raid_cancel()
    d = [c for c in calls if c[0] == "DELETE"]
    assert len(d) == 1
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(d[0][1]).query) == {"broadcaster_id": ["42"]}
    assert out == {"ok": True, "cancelled": True, "from": "ourchannel"}


def test_raid_cancel_with_nothing_pending(monkeypatch):
    msg = "The broadcaster doesn't have a pending raid to cancel"
    errors = {("DELETE", "helix/raids"): (404, msg)}
    monkeypatch.setattr(tw, "urlopen", _router([], _base(), errors))
    with pytest.raises(tw.TwitchRaidError) as e:
        tw.raid_cancel()
    assert e.value.code == "no_pending_raid"


# ---- raid_candidates --------------------------------------------------------------------------

def test_candidates_live_status_game_viewers(monkeypatch):
    calls = []
    table = _base({
        ("GET", "helix/users?login="): {"data": [
            {"id": "777", "login": "pixel_kid", "display_name": "Pixel_Kid",
             "broadcaster_type": "affiliate", "email": "x@example.com"},
            {"id": "888", "login": "sleepy", "display_name": "Sleepy", "broadcaster_type": ""}]},
        ("GET", "helix/streams"): {"data": [
            {"user_id": "777", "user_login": "pixel_kid", "game_name": "Celeste", "viewer_count": 3,
             "title": "any% practice", "started_at": "2026-10-10T19:00:00Z", "type": "live"}]},
    })
    monkeypatch.setattr(tw, "urlopen", _router(calls, table))
    out = tw.raid_candidates(["Pixel_Kid", "sleepy", "ghost_user", "bad login", "pixel_kid"])
    assert out["invalid"] == ["bad login"]
    by = {c["login"]: c for c in out["channels"]}
    assert set(by) == {"pixel_kid", "sleepy", "ghost_user"}
    assert by["pixel_kid"] == {"login": "pixel_kid", "display_name": "Pixel_Kid", "id": "777",
                               "exists": True, "broadcaster_type": "affiliate", "live": True,
                               "game_name": "Celeste", "viewer_count": 3,
                               "title": "any% practice", "started_at": "2026-10-10T19:00:00Z"}
    assert by["sleepy"]["live"] is False and by["sleepy"]["viewer_count"] is None
    assert by["ghost_user"]["exists"] is False and by["ghost_user"]["live"] is False
    s = [c for c in calls if "helix/streams" in c[1]][0]
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(s[1]).query)
    assert sorted(q["user_login"]) == ["ghost_user", "pixel_kid", "sleepy"]
    assert q["first"] == ["100"]
    assert "email" not in json.dumps(out)


def test_candidates_batches_100(monkeypatch):
    calls = []
    table = _base({("GET", "helix/users?login="): {"data": []},
                   ("GET", "helix/streams"): {"data": []}})
    monkeypatch.setattr(tw, "urlopen", _router(calls, table))
    tw.raid_candidates([f"user_{i:03d}" for i in range(150)])
    assert len([c for c in calls if "helix/streams" in c[1]]) == 2
    assert len([c for c in calls if "helix/users?login=" in c[1]]) == 2


def test_candidates_empty_makes_no_calls(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no network")

    monkeypatch.setattr(tw, "urlopen", boom)
    assert tw.raid_candidates([]) == {"channels": [], "invalid": []}


# ---- MCP tools --------------------------------------------------------------------------------

def _call(name, args):
    from yt_studio_mcp.server import build_app

    res = asyncio.run(build_app().call_tool(name, args))
    content = res[0] if isinstance(res, tuple) else res
    return json.loads(content[0].text)


def test_tool_raid_dry_run_validates_and_previews(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("dry run must not touch Twitch")

    monkeypatch.setattr(tw, "urlopen", boom)
    out = _call("twitch_raid", {"to_login": "Pixel_Kid", "dry_run": True})
    assert out["preview"] is True and out["would"]["to_login"] == "pixel_kid"
    bad = _call("twitch_raid", {"to_login": "no", "dry_run": True})
    assert bad["ok"] is False and bad["code"] == "invalid_login"
    c = _call("twitch_raid_cancel", {"dry_run": True})
    assert c["preview"] is True


def test_tool_raid_returns_structured_error(monkeypatch):
    errors = {("POST", "helix/raids"): (401, "Missing scope: channel:manage:raids")}
    monkeypatch.setattr(tw, "urlopen", _router([], _base(), errors))
    out = _call("twitch_raid", {"to_login": "pixel_kid"})
    assert out["ok"] is False and out["code"] == "missing_scope"
    assert "twitch_authorize_url" in out["hint"]


def test_tool_candidates(monkeypatch):
    table = _base({("GET", "helix/users?login="): {"data": []},
                   ("GET", "helix/streams"): {"data": []}})
    monkeypatch.setattr(tw, "urlopen", _router([], table))
    out = _call("twitch_raid_candidates", {"logins": ["pixel_kid"]})
    assert out["channels"][0]["login"] == "pixel_kid" and out["channels"][0]["live"] is False
