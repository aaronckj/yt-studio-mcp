"""Moderation loops must not act beyond what they were told, nor lose work."""

import json

import pytest

from yt_studio_mcp import client, llm, watch_cli
from yt_studio_mcp.client import YT, ApiError
from yt_studio_mcp.tools import banned_words

from .fakes import Req, tools_of

SPAM = "buy spamword now"


def _thread(i, text, ts):
    return {"snippet": {"videoId": "live1", "topLevelComment": {"id": f"c{i}", "snippet": {
        "authorChannelId": {"value": f"u{i}"}, "authorDisplayName": f"u{i}",
        "textOriginal": text, "publishedAt": ts}}}}


class _Data:
    def __init__(self, threads, chat, live_cycles=2, fail_moderation=False, more=False):
        self.threads, self.chat = threads, chat
        self.live_cycles = live_cycles
        self.fail_moderation = fail_moderation
        self.more = more
        self.writes = []

    def channels(self):
        class C:
            def list(self, **kw):
                return Req({"items": [{"id": "OWN"}]})
        return C()

    def liveBroadcasts(self):  # noqa: N802
        outer = self

        class B:
            def list(self, **kw):
                if outer.live_cycles <= 0:
                    return Req({"items": []})
                outer.live_cycles -= 1
                return Req({"items": [{"id": "live1", "snippet": {
                    "liveChatId": "chat1", "title": "t"}}]})
        return B()

    def commentThreads(self):  # noqa: N802
        outer = self

        class T:
            def list(self, **kw):
                page = {"items": outer.threads}
                if outer.more:
                    page["nextPageToken"] = "x"
                return Req(page)
        return T()

    def liveChatMessages(self):  # noqa: N802
        outer = self

        class M:
            def list(self, **kw):
                return Req({"items": outer.chat})

            def delete(self, id=None):  # noqa: A002
                outer.writes.append(("chat.delete", id))
                return Req({})
        return M()

    def comments(self):
        outer = self

        class Cm:
            def setModerationStatus(self, id=None, moderationStatus=None):  # noqa: N802,A002
                if outer.fail_moderation:
                    from googleapiclient.errors import HttpError

                    class R:
                        status = 403
                        reason = "x"
                    body = json.dumps({"error": {"errors": [{"reason": "quotaExceeded"}]}})
                    return Req(error=HttpError(R(), body.encode()))
                outer.writes.append(("moderate", id, moderationStatus))
                return Req({})
        return Cm()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_MCP_CONFIG_DIR", str(tmp_path))
    (tmp_path / "banned_words.json").write_text(json.dumps(["spamword"]))
    monkeypatch.setattr(watch_cli.time, "sleep", lambda s: None)
    monkeypatch.delenv("YT_MCP_NTFY_URL", raising=False)
    return tmp_path


def test_watch_observe_only_never_deletes_chat(cfg, monkeypatch):
    chat = [{"id": "m1", "authorDetails": {"displayName": "x", "channelId": "u9"},
             "snippet": {"displayMessage": SPAM}}]
    data = _Data([_thread(1, SPAM, "2026-10-06T00:00:00Z")], chat)
    monkeypatch.setattr(client, "_yt", YT(data_service=data))
    assert watch_cli.run_watch(15, "none", False) == 0
    assert data.live_cycles == 0  # the loop really ran a cycle
    assert data.writes == []


def test_watch_acts_when_asked(cfg, monkeypatch):
    chat = [{"id": "m1", "authorDetails": {"displayName": "x", "channelId": "u9"},
             "snippet": {"displayMessage": SPAM}}]
    data = _Data([_thread(1, SPAM, "2026-10-06T00:00:00Z")], chat)
    monkeypatch.setattr(client, "_yt", YT(data_service=data))
    watch_cli.run_watch(15, "heldForReview", False)
    assert ("chat.delete", "m1") in data.writes
    assert ("moderate", "c1", "heldForReview") in data.writes


def test_scan_watermark_not_advanced_when_moderation_fails(cfg, monkeypatch):
    data = _Data([_thread(1, SPAM, "2026-10-06T00:00:00Z")], [], fail_moderation=True)
    monkeypatch.setattr(client, "_yt", YT(data_service=data))
    out = tools_of(banned_words)["scan_comments"](
        auto_action="heldForReview", incremental=True)
    assert out["moderation"]["failed"]
    assert "watermark_now" not in out
    assert not (cfg / "scan_state.json").exists()


def test_scan_reports_truncation_gap(cfg, monkeypatch):
    (cfg / "scan_state.json").write_text(json.dumps({"last_scan": "2026-01-01T00:00:00Z"}))
    data = _Data([_thread(1, "hello", "2026-10-06T00:00:00Z")], [], more=True)
    monkeypatch.setattr(client, "_yt", YT(data_service=data))
    out = tools_of(banned_words)["scan_comments"](limit=1, incremental=True)
    assert out["truncated"]["unscanned_after"] == "2026-01-01T00:00:00Z"


def test_apply_moderation_stops_on_quota_and_reports_rest():
    data = _Data([], [], fail_moderation=True)
    applied, failed = banned_words.apply_moderation(
        YT(data_service=data), [{"comment_id": "a"}, {"comment_id": "b"}], "rejected")
    assert applied == [] and [f["comment_id"] for f in failed] == ["a", "b"]
    assert ApiError


@pytest.mark.parametrize("raw,expected", [
    ('{"spam": "false", "reason": "x"}', False),
    ('{"spam": "true", "reason": "x"}', True),
    ('{"spam": true, "reason": "x"}', True),
    ('{"spam": 1, "reason": "x"}', False),
])
def test_llm_verdict_is_strict(monkeypatch, raw, expected):
    monkeypatch.setenv("YT_MCP_LLM_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("YT_MCP_LLM_MODEL", "m")

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": raw}}]}).encode()

    monkeypatch.setattr(llm, "urlopen", lambda *a, **k: Resp())
    assert llm.classify_comment("a", "b")["spam"] is expected
