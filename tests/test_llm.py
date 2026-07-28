import io
import json

import yt_studio_mcp.llm as llm_mod
from yt_studio_mcp.llm import classify_comment, llm_configured


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def openai_response(content):
    return json.dumps(
        {"choices": [{"message": {"content": content}}]}
    ).encode()


def test_llm_configured(monkeypatch):
    monkeypatch.delenv("YT_MCP_LLM_URL", raising=False)
    assert llm_configured() is False
    monkeypatch.setenv("YT_MCP_LLM_URL", "http://localhost:4000/v1")
    monkeypatch.setenv("YT_MCP_LLM_MODEL", "test-model")
    assert llm_configured() is True


def test_classify_spam(monkeypatch):
    monkeypatch.setenv("YT_MCP_LLM_URL", "http://localhost:4000/v1")
    monkeypatch.setenv("YT_MCP_LLM_MODEL", "test-model")
    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return FakeResp(openai_response('{"spam": true, "reason": "gift card scam"}'))

    monkeypatch.setattr(llm_mod, "urlopen", fake_urlopen)
    out = classify_comment("scammer", "claim your free gift card at bit.ly/x")
    assert out == {"spam": True, "reason": "gift card scam"}
    assert captured["url"].endswith("/chat/completions")
    assert captured["body"]["temperature"] == 0


def test_classify_handles_wrapped_json(monkeypatch):
    monkeypatch.setenv("YT_MCP_LLM_URL", "http://localhost:4000/v1")
    monkeypatch.setenv("YT_MCP_LLM_MODEL", "test-model")
    monkeypatch.setattr(
        llm_mod,
        "urlopen",
        lambda req, timeout=30: FakeResp(
            openai_response('Sure! Here is my answer:\n{"spam": false, "reason": "gamer talk"}')
        ),
    )
    out = classify_comment("kid", "this boss is so hard lol")
    assert out["spam"] is False


def test_classify_error_path(monkeypatch):
    monkeypatch.setenv("YT_MCP_LLM_URL", "http://localhost:4000/v1")
    monkeypatch.setenv("YT_MCP_LLM_MODEL", "test-model")

    def boom(req, timeout=30):
        from urllib.error import URLError

        raise URLError("connection refused")

    monkeypatch.setattr(llm_mod, "urlopen", boom)
    out = classify_comment("x", "y")
    assert "error" in out
