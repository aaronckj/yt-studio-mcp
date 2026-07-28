import pytest

from yt_studio_mcp.auth import AuthError, get_credentials
from yt_studio_mcp.secrets import FileStore

STORED = {
    "refresh_token": "rt-1",
    "client_id": "cid-1",
    "client_secret": "cs-1",
}


def make_store(tmp_path, data):
    store = FileStore(path=tmp_path / "creds.json")
    for k, v in data.items():
        store.set(k, v)
    return store


def test_get_credentials_builds_from_store(tmp_path, monkeypatch):
    store = make_store(tmp_path, STORED)
    monkeypatch.setattr("yt_studio_mcp.auth._refresh", lambda creds: None)
    creds = get_credentials(store=store)
    assert creds.refresh_token == "rt-1"
    assert creds.client_id == "cid-1"


def test_get_credentials_missing_token_raises(tmp_path):
    store = make_store(tmp_path, {})
    with pytest.raises(AuthError, match="yt-studio-mcp auth"):
        get_credentials(store=store)
