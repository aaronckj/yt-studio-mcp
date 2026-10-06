"""The spam scan must fail CLOSED: a state problem never means 'skip comments'."""

import json

import pytest

from yt_studio_mcp import client, llm, scan_cli
from yt_studio_mcp.client import YT
from yt_studio_mcp.tools import banned_words

from .fakes import tools_of
from .test_moderation_safety import SPAM, _Data, _thread

TS_NEW = "2026-10-06T00:00:00Z"
OLD_MARK = "2026-01-01T00:00:00Z"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_MCP_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("YT_MCP_NTFY_URL", raising=False)
    monkeypatch.delenv("YT_MCP_LLM_URL", raising=False)
    (tmp_path / "banned_words.json").write_text(json.dumps(["spamword"]))
    (tmp_path / "scan_state.json").write_text(json.dumps({"last_scan": OLD_MARK}))
    return tmp_path


def _use(monkeypatch, data):
    monkeypatch.setattr(client, "_yt", YT(data_service=data))


def _mark(cfg):
    return json.loads((cfg / "scan_state.json").read_text())["last_scan"]


@pytest.mark.parametrize("content", [None, "", "{not json", '{"a": 1}', "[]", "[1, 2]"])
def test_cli_refuses_missing_or_bad_wordlist(cfg, monkeypatch, content):
    wl = cfg / "banned_words.json"
    if content is None:
        wl.unlink()
    else:
        wl.write_text(content)
    _use(monkeypatch, _Data([_thread(1, SPAM, TS_NEW)], []))
    assert scan_cli.main(["--incremental", "--auto-action", "heldForReview"]) != 0
    assert _mark(cfg) == OLD_MARK


@pytest.mark.parametrize("content", ["", "{bad", '["x"]', '{"last_scan": 5}',
                                     '{"last_scan": "yesterday"}'])
def test_cli_refuses_corrupt_watermark(cfg, monkeypatch, content):
    (cfg / "scan_state.json").write_text(content)
    data = _Data([_thread(1, SPAM, TS_NEW)], [])
    _use(monkeypatch, data)
    assert scan_cli.main(["--incremental", "--auto-action", "heldForReview"]) == 1
    assert (cfg / "scan_state.json").read_text() == content
    assert data.writes == []


def test_tool_refuses_missing_wordlist_even_with_extra_words(cfg, monkeypatch):
    (cfg / "banned_words.json").unlink()
    _use(monkeypatch, _Data([_thread(1, SPAM, TS_NEW)], []))
    out = tools_of(banned_words)["scan_comments"](extra_words=["x"], incremental=True)
    assert "error" in out and _mark(cfg) == OLD_MARK


def test_llm_failure_holds_watermark(cfg, monkeypatch):
    monkeypatch.setenv("YT_MCP_LLM_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("YT_MCP_LLM_MODEL", "m")
    monkeypatch.setattr(llm, "classify_comment", lambda a, t: {"error": "down"})
    _use(monkeypatch, _Data([_thread(1, "clean text", TS_NEW)], []))
    assert scan_cli.main(["--incremental", "--llm"]) == 4
    assert _mark(cfg) == OLD_MARK


def test_truncated_scan_holds_watermark(cfg, monkeypatch):
    _use(monkeypatch, _Data([_thread(1, "clean", TS_NEW)], [], more=True))
    assert scan_cli.main(["--incremental", "--limit", "1"]) == 4
    assert _mark(cfg) == OLD_MARK
    out = tools_of(banned_words)["scan_comments"](limit=1, incremental=True)
    assert out["truncated"] and "watermark_now" not in out
    assert _mark(cfg) == OLD_MARK


def test_clean_run_advances_watermark(cfg, monkeypatch):
    _use(monkeypatch, _Data([_thread(1, "clean", TS_NEW)], []))
    assert scan_cli.main(["--incremental"]) == 0
    assert _mark(cfg) == TS_NEW
    assert not list(cfg.glob("*.tmp")) and not list(cfg.glob(".*.tmp"))


def test_concurrent_scan_is_refused(cfg, monkeypatch):
    data = _Data([_thread(1, SPAM, TS_NEW)], [])
    _use(monkeypatch, data)
    with banned_words.scan_lock():
        assert scan_cli.main(["--incremental", "--auto-action", "heldForReview"]) == 5
        out = tools_of(banned_words)["scan_comments"](incremental=True)
        assert "another scan" in out["error"]
    assert data.writes == [] and _mark(cfg) == OLD_MARK


def test_watermark_writes_use_unique_temp_files(cfg, monkeypatch):
    names = []
    real = banned_words.os.replace

    def spy(src, dst):
        names.append(str(src))
        real(src, dst)

    monkeypatch.setattr(banned_words.os, "replace", spy)
    banned_words.write_watermark(TS_NEW)
    banned_words.write_watermark(TS_NEW)
    assert len(set(names)) == 2


def test_wordlist_edits_are_atomic_and_strict(cfg):
    (cfg / "banned_words.json").write_text("{corrupt")
    t = tools_of(banned_words)
    out = t["banned_words_add"](["new"])
    assert "error" in out
    assert (cfg / "banned_words.json").read_text() == "{corrupt"  # not clobbered


def test_incremental_single_video_scan_refused(cfg, monkeypatch):
    """A per-video scan must not move the channel-wide watermark."""
    _use(monkeypatch, _Data([_thread(1, "clean", TS_NEW)], []))
    out = tools_of(banned_words)["scan_comments"](video_id="v1", incremental=True)
    assert "channel-wide" in out["error"] and _mark(cfg) == OLD_MARK


def test_removing_every_word_is_refused(cfg):
    out = tools_of(banned_words)["banned_words_remove"](["spamword"])
    assert "error" in out
    assert json.loads((cfg / "banned_words.json").read_text()) == ["spamword"]
