"""Per-instance state isolation via YT_MCP_CONFIG_DIR (offline)."""

import json
from pathlib import Path

from yt_studio_mcp import paths
from yt_studio_mcp.secrets import FileStore, VaultproxyStore
from yt_studio_mcp.tools import banned_words, giveaway, questions, thumbnails


def test_default_is_unchanged(monkeypatch):
    monkeypatch.delenv("YT_MCP_CONFIG_DIR", raising=False)
    monkeypatch.delenv("YT_MCP_CREDENTIALS_FILE", raising=False)
    base = Path.home() / ".config" / "yt-studio-mcp"
    assert paths.config_dir() == base
    assert paths.credentials_path() == base / "credentials.json"
    assert FileStore().path == base / "credentials.json"
    # former module constants still resolve to the same places
    assert banned_words.WORDLIST_PATH == base / "banned_words.json"
    assert banned_words.SCAN_STATE_PATH == base / "scan_state.json"
    assert giveaway.SNAPSHOT_DIR == base / "giveaways"
    assert questions.STATE_PATH == base / "question_comments.json"
    assert thumbnails.OUT_DIR == base / "thumbnails"


def test_blank_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("YT_MCP_CONFIG_DIR", "  ")
    assert paths.config_dir() == paths.DEFAULT_CONFIG_DIR


def test_two_instances_are_isolated(monkeypatch, tmp_path):
    a, b = tmp_path / "chan-a", tmp_path / "chan-b"
    src = tmp_path / "frame.jpg"
    src.write_bytes(b"\xff\xd8fake")

    monkeypatch.setenv("YT_MCP_CONFIG_DIR", str(a))
    FileStore().set("refresh_token", "token-a")
    banned_words._save_words(["alpha"])
    questions._save_state({"vid-a": {"comment_id": "c1"}})
    assert thumbnails.archive_thumbnail(src, video_id="vid-a")
    paths.scan_state_path().parent.mkdir(parents=True, exist_ok=True)
    paths.scan_state_path().write_text(json.dumps({"last_scan": "A"}))

    monkeypatch.setenv("YT_MCP_CONFIG_DIR", str(b))
    store_b = FileStore()
    assert store_b.get("refresh_token") is None
    assert banned_words._load_words() == []
    assert questions._load_state() == {}
    assert not paths.scan_state_path().exists()
    assert not paths.thumbnails_dir().exists()
    store_b.set("refresh_token", "token-b")
    assert giveaway.resolve_giveaway_path(str(b / "giveaways" / "x.json"))

    # instance A untouched by B
    monkeypatch.setenv("YT_MCP_CONFIG_DIR", str(a))
    assert FileStore().get("refresh_token") == "token-a"
    assert banned_words._load_words() == ["alpha"]
    assert len(list((a / "thumbnails").glob("vid-a-*.jpg"))) == 1
    # and each instance's giveaway confinement rejects the other's tree
    import pytest

    with pytest.raises(ValueError):
        giveaway.resolve_giveaway_path(str(b / "giveaways" / "x.json"))


def test_credentials_file_override(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_MCP_CONFIG_DIR", str(tmp_path / "dir"))
    monkeypatch.setenv("YT_MCP_CREDENTIALS_FILE", str(tmp_path / "elsewhere.json"))
    assert FileStore().path == tmp_path / "elsewhere.json"
    assert paths.wordlist_path() == tmp_path / "dir" / "banned_words.json"


def test_vaultproxy_prefix(monkeypatch):
    monkeypatch.delenv("YT_MCP_VAULTPROXY_PREFIX", raising=False)
    assert VaultproxyStore().prefix == "yt-studio-mcp"
    monkeypatch.setenv("YT_MCP_VAULTPROXY_PREFIX", "yt-second")
    assert VaultproxyStore().prefix == "yt-second"
