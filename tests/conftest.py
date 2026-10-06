import pytest


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path_factory, monkeypatch):
    """No test may touch the real ~/.config/yt-studio-mcp (tokens, word list,
    quota ledger, scan watermark)."""
    d = tmp_path_factory.mktemp("cfg")
    monkeypatch.setenv("YT_MCP_CONFIG_DIR", str(d))
    monkeypatch.delenv("YT_MCP_QUOTA_LEDGER", raising=False)
    monkeypatch.delenv("YT_MCP_EXPECTED_CHANNEL_ID", raising=False)
    monkeypatch.delenv("YT_MCP_SECRETS", raising=False)
    monkeypatch.delenv("YT_MCP_CREDENTIALS_FILE", raising=False)
