"""Offline tests for the Twitch Helix layer. No network."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse

import pytest

from yt_studio_mcp import twitch as tw


class FakeResp:
    def __init__(self, payload, status=200):
        self._b = json.dumps(payload).encode() if payload is not None else b""
        self.status = status

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "csec")
    monkeypatch.setenv("TWITCH_REFRESH_TOKEN", "rtok")
    monkeypatch.setattr(tw, "get_store", lambda: _NullStore())


class _NullStore:
    def get(self, key):
        return None

    def set(self, key, value):
        return None


def _router(calls, table):
    def fake_urlopen(req, timeout=None):
        url = req.full_url
        calls.append((req.method or "GET", url, req.data))
        for frag, payload in table.items():
            if frag in url:
                return FakeResp(payload)
        raise AssertionError(f"unexpected URL {url}")

    return fake_urlopen


def test_missing_credentials_is_actionable(monkeypatch):
    for k in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TWITCH_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(tw.TwitchNotConfigured) as e:
        tw.Twitch()
    assert "twitch_client_id" in e.value.missing
    assert "dev.twitch.tv" in str(e.value)


def test_set_channel_resolves_category_and_patches(monkeypatch):
    calls = []
    table = {
        "oauth2/token": {"access_token": "atok", "refresh_token": "rtok"},
        "helix/users": {"data": [{"id": "42", "login": "thecipher"}]},
        "helix/games": {"data": [{"id": "999", "name": "Hollow Knight: Silksong"}]},
        "helix/channels": {"data": [{"title": "old", "game_name": "Hollow Knight"}]},
    }
    monkeypatch.setattr(tw, "urlopen", _router(calls, table))
    out = tw.set_channel("Silksong Stream 12", "Hollow Knight: Silksong")
    patches = [c for c in calls if c[0] == "PATCH"]
    assert len(patches) == 1, "channel must be modified exactly once"
    body = json.loads(patches[0][2])
    assert body["title"] == "Silksong Stream 12"
    assert body["game_id"] == "999", "must send game_id, not the name"
    assert out["category_resolved"] == "Hollow Knight: Silksong"
    assert out["login"] == "thecipher"


def test_unknown_category_raises_before_patching(monkeypatch):
    calls = []
    table = {
        "oauth2/token": {"access_token": "atok"},
        "helix/users": {"data": [{"id": "42", "login": "x"}]},
        "helix/games": {"data": []},
        "search/categories": {"data": []},
        "helix/channels": {"data": [{"title": "old", "game_name": "g"}]},
    }
    monkeypatch.setattr(tw, "urlopen", _router(calls, table))
    with pytest.raises(tw.TwitchError):
        tw.set_channel("t", "Not A Real Game")
    assert not [c for c in calls if c[0] == "PATCH"], "must not patch on a bad category"


def test_title_only_does_not_touch_category(monkeypatch):
    calls = []
    table = {
        "oauth2/token": {"access_token": "atok"},
        "helix/users": {"data": [{"id": "42", "login": "x"}]},
        "helix/channels": {"data": [{"title": "old", "game_name": "g"}]},
    }
    monkeypatch.setattr(tw, "urlopen", _router(calls, table))
    tw.set_channel("just a title", None)
    body = json.loads([c for c in calls if c[0] == "PATCH"][0][2])
    assert "game_id" not in body


def test_rotated_refresh_token_is_persisted(monkeypatch):
    saved = {}

    class Store:
        def get(self, k):
            return None

        def set(self, k, v):
            saved[k] = v

    monkeypatch.setattr(tw, "get_store", lambda: Store())
    table = {
        "oauth2/token": {"access_token": "atok", "refresh_token": "NEWREFRESH"},
        "helix/users": {"data": [{"id": "1", "login": "x"}]},
        "helix/channels": {"data": [{}]},
    }
    monkeypatch.setattr(tw, "urlopen", _router([], table))
    tw.Twitch().get_channel()
    assert saved["twitch_refresh_token"] == "NEWREFRESH"


def test_timeout_is_wrapped_not_leaked(monkeypatch):
    def boom(req, timeout=None):
        raise TimeoutError("timed out")  # an OSError, NOT a URLError

    monkeypatch.setattr(tw, "urlopen", boom)
    with pytest.raises(tw.TwitchError) as e:
        tw.Twitch().me()
    assert "timed out" in str(e.value)


def test_secrets_never_reach_the_error_message(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(tw, "urlopen", boom)
    url = f"{tw.OAUTH}/token?client_secret=SUPERSECRET&access_token=TOK"
    with pytest.raises(tw.TwitchError) as e:
        tw._http(url)
    assert "SUPERSECRET" not in str(e.value)
    assert "TOK" not in str(e.value)
    assert "REDACTED" in str(e.value)
