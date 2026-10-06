"""Token storage and wrong-channel protection."""

import os
import stat
from urllib.error import HTTPError

import pytest

from yt_studio_mcp import auth, client
from yt_studio_mcp.client import YT, ChannelMismatch
from yt_studio_mcp.secrets import FileStore, VaultproxyStore

from .fakes import Req


def test_file_store_never_world_readable_even_briefly(tmp_path, monkeypatch):
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(stat.S_IMODE(os.stat(src).st_mode))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    old = os.umask(0o022)
    try:
        FileStore(path=tmp_path / "d" / "creds.json").set("refresh_token", "t")
    finally:
        os.umask(old)
    assert seen == [0o600]
    assert stat.S_IMODE((tmp_path / "d").stat().st_mode) == 0o700
    assert not list((tmp_path / "d").glob(".*.tmp"))


def test_vaultproxy_missing_item_is_none_not_unreachable(monkeypatch):
    def boom(req, timeout=10):
        raise HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr("yt_studio_mcp.secrets.urlopen", boom)
    assert VaultproxyStore(base_url="http://x").get("twitch_refresh_token") is None


class _Svc:
    def __init__(self, cid):
        self.cid = cid

    def channels(self):
        outer = self

        class C:
            def list(self, **kw):
                return Req({"items": [{"id": outer.cid, "snippet": {"title": "T"}}]})
        return C()


def test_expected_channel_guard(monkeypatch):
    monkeypatch.setenv(client.EXPECTED_CHANNEL_ENV, "UC_RIGHT")
    yt = YT()
    yt.check_channel(_Svc("UC_RIGHT"))
    with pytest.raises(ChannelMismatch):
        yt.check_channel(_Svc("UC_WRONG"))
    monkeypatch.delenv(client.EXPECTED_CHANNEL_ENV)
    YT().check_channel(_Svc("UC_WRONG"))  # opt-in only


class _Creds:
    refresh_token = "new-refresh"
    client_id = "cid"
    client_secret = "sec"


class _Flow:
    kwargs = None

    def run_local_server(self, **kw):
        _Flow.kwargs = kw
        return _Creds()


def _secret(tmp_path):
    p = tmp_path / "client_secret.json"
    p.write_text('{"installed": {"client_id": "cid", "client_secret": "sec"}}')
    return str(p)


def test_auth_refuses_to_overwrite_another_channels_token(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_channel_of", lambda c: ("UC_JW", "Other Channel"))
    store = FileStore(path=tmp_path / "creds.json")
    store.set("refresh_token", "old")
    store.set("channel_id", "UC_MAIN")
    with pytest.raises(auth.AuthError, match="Nothing was written"):
        auth.run_auth_flow(_secret(tmp_path), store, flow_factory=lambda *a, **k: _Flow())
    assert store.get("refresh_token") == "old"
    msg = auth.run_auth_flow(_secret(tmp_path), store, force=True, port=59067,
                             open_browser=False, timeout_seconds=600,
                             flow_factory=lambda *a, **k: _Flow())
    assert "UC_JW" in msg and store.get("channel_id") == "UC_JW"
    assert _Flow.kwargs == {"port": 59067, "prompt": "consent", "open_browser": False,
                            "timeout_seconds": 600}
