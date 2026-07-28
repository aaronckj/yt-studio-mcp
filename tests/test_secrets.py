import json
import stat

import pytest

from yt_studio_mcp.secrets import EnvStore, FileStore, VaultproxyStore, get_store


def test_file_store_roundtrip(tmp_path):
    store = FileStore(path=tmp_path / "creds.json")
    assert store.get("refresh_token") is None
    store.set("refresh_token", "tok-123")
    store.set("client_id", "cid")
    assert store.get("refresh_token") == "tok-123"
    assert store.get("client_id") == "cid"
    # re-open from disk
    store2 = FileStore(path=tmp_path / "creds.json")
    assert store2.get("refresh_token") == "tok-123"


def test_file_store_permissions(tmp_path):
    store = FileStore(path=tmp_path / "creds.json")
    store.set("k", "v")
    mode = stat.S_IMODE((tmp_path / "creds.json").stat().st_mode)
    assert mode == 0o600


def test_env_store_reads_prefixed_vars(monkeypatch):
    monkeypatch.setenv("YT_MCP_REFRESH_TOKEN", "envtok")
    store = EnvStore()
    assert store.get("refresh_token") == "envtok"
    assert store.get("missing") is None
    with pytest.raises(NotImplementedError):
        store.set("refresh_token", "x")


def test_get_store_selection(monkeypatch, tmp_path):
    monkeypatch.delenv("YT_MCP_SECRETS", raising=False)
    assert isinstance(get_store(), FileStore)
    monkeypatch.setenv("YT_MCP_SECRETS", "env")
    assert isinstance(get_store(), EnvStore)
    monkeypatch.setenv("YT_MCP_SECRETS", "vaultproxy")
    assert isinstance(get_store(), VaultproxyStore)
    monkeypatch.setenv("YT_MCP_SECRETS", "bogus")
    with pytest.raises(ValueError):
        get_store()


def test_vaultproxy_store(monkeypatch):
    calls = {}

    class FakeResponse:
        def __init__(self, body: bytes, status: int = 200):
            self.body = body
            self.status = status

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=10):
        calls["url"] = req.full_url
        calls["method"] = req.get_method()
        if req.get_method() == "GET":
            return FakeResponse(json.dumps({"value": "vp-tok"}).encode())
        calls["data"] = req.data
        return FakeResponse(b"{}")

    monkeypatch.setattr("yt_studio_mcp.secrets.urlopen", fake_urlopen)
    store = VaultproxyStore(base_url="http://127.0.0.1:8199")
    assert store.get("refresh_token") == "vp-tok"
    assert "refresh_token" in calls["url"]
    store.set("refresh_token", "new")
    assert calls["method"] == "PUT"
    assert json.loads(calls["data"])["value"] == "new"
