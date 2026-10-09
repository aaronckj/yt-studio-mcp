"""list_comments(include_replies=True): every reply's author and text, flagged when it is the channel's own."""

from __future__ import annotations

from yt_studio_mcp import client
from yt_studio_mcp.client import YT
from yt_studio_mcp.tools import comments

from .fakes import Req, tools_of

ME = "UCme"


def _snip(author, cid, text, ts):
    return {"authorDisplayName": author, "authorChannelId": {"value": cid}, "textOriginal": text,
            "publishedAt": ts, "likeCount": 0}


class _Data:
    def __init__(self):
        self.reply_calls = []

    def channels(self):
        class C:
            def list(self, **kw):
                return Req({"items": [{"id": ME}]})
        return C()

    def commentThreads(self):
        class T:
            def list(self, **kw):
                return Req({"items": [
                    {"id": "t1", "snippet": {"videoId": "v1", "totalReplyCount": 2,
                                             "topLevelComment": {"id": "t1", "snippet": _snip("@fan", "UCfan", "great", "2026-10-06T01:00:00Z")}}},
                    {"id": "t2", "snippet": {"videoId": "v2", "totalReplyCount": 0,
                                             "topLevelComment": {"id": "t2", "snippet": _snip("@other", "UCo", "hi", "2026-10-07T01:00:00Z")}}},
                ]})
        return T()

    def comments(self):
        data = self
        class R:
            def list(self, parentId=None, **kw):
                data.reply_calls.append(parentId)
                return Req({"items": [
                    {"id": "r1", "snippet": _snip("@The_Channel", ME, "thanks!", "2026-10-06T02:00:00Z")},
                    {"id": "r2", "snippet": _snip("@fan2", "UCfan2", "same", "2026-10-06T03:00:00Z")},
                ]})
        return R()


def test_replies_come_with_author_text_and_owner_flag(monkeypatch):
    data = _Data()
    monkeypatch.setattr(client, "_yt", YT(data_service=data))
    out = tools_of(comments)["list_comments"](include_replies=True)
    t1 = next(t for t in out if t["thread_id"] == "t1")
    assert [(r["author"], r["text"], r["is_channel"]) for r in t1["replies"]] == [
        ("@The_Channel", "thanks!", True), ("@fan2", "same", False)]
    assert t1["replies"][1]["author_channel_id"] == "UCfan2" and t1["replies"][0]["published"] == "2026-10-06T02:00:00Z"
    assert next(t for t in out if t["thread_id"] == "t2")["replies"] == []
    assert data.reply_calls == ["t1"]                 # only threads that have replies cost a call


def test_default_stays_threads_only(monkeypatch):
    data = _Data()
    monkeypatch.setattr(client, "_yt", YT(data_service=data))
    out = tools_of(comments)["list_comments"]()
    assert "replies" not in out[0] and data.reply_calls == []
