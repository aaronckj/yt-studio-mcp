"""Validation that must happen before any API call is spent."""

import pytest

from yt_studio_mcp import client
from yt_studio_mcp.client import YT
from yt_studio_mcp.tools import analytics, bulk, live, playlists, thumbnails

from .fakes import Req, tools_of


class _Explode:
    def __getattr__(self, name):
        raise AssertionError(f"API touched: {name}")


@pytest.fixture
def no_api(monkeypatch):
    monkeypatch.setattr(client, "_yt", YT(data_service=_Explode(),
                                          analytics_service=_Explode()))


def test_broadcast_validation(no_api):
    t = tools_of(live)
    assert "error" in t["create_broadcast"]("t", "2099-01-01T18:00:00Z", privacy="publik")
    assert "error" in t["create_broadcast"]("t", "2099-01-01T18:00:00")  # naive
    assert "error" in t["create_broadcast"]("t", "2020-01-01T18:00:00Z")  # past
    p = t["create_broadcast"]("t", "2099-01-01T18:00:00Z", dry_run=True)
    assert p["would"]["listed_publicly_now"] is True


def test_playlist_privacy_validated(no_api):
    t = tools_of(playlists)
    assert "error" in t["create_playlist"]("x", privacy="hidden")
    assert "error" in t["update_playlist"]("PL1", privacy="hidden")


def test_analytics_filter_injection_refused(no_api):
    out = tools_of(analytics)["video_report"]("abc;country==US", "2026-01-01", "2026-01-02")
    assert "error" in out[0]


def test_bulk_rejects_blanking_footer_and_bad_mode(no_api):
    t = tools_of(bulk)
    assert "error" in t["batch_update_descriptions"]("footer", "   ")
    assert "error" in t["batch_update_descriptions"]("prepend", "x")
    assert "error" in t["batch_update_descriptions"]("replace", "x")


def test_bulk_partial_failure_is_reported(monkeypatch):
    import json

    from googleapiclient.errors import HttpError

    class R:
        status = 403
        reason = "x"

    class Videos:
        def __init__(self):
            self.updates = []

        def list(self, part=None, id=None):  # noqa: A002
            return Req({"items": [{"id": v, "snippet": {"title": v, "description": "d"}}
                                  for v in id.split(",") if v != "gone"]})

        def update(self, part=None, body=None):
            self.updates.append(body["id"])
            if len(self.updates) == 2:
                err = json.dumps({"error": {"errors": [{"reason": "quotaExceeded"}]}})
                return Req(error=HttpError(R(), err.encode()))
            return Req({})

    class Data:
        v = Videos()

        def videos(self):
            return self.v

    data = Data()
    monkeypatch.setattr(client, "_yt", YT(data_service=data))
    out = tools_of(bulk)["batch_update_tags"](add=["new"], video_ids=["a", "b", "c", "gone"])
    assert [c["video_id"] for c in out["changed"]] == ["a"]
    assert [f["video_id"] for f in out["failed"]] == ["b", "c"]
    assert out["not_found"] == ["gone"]
    assert data.v.updates == ["a", "b"]  # nothing attempted after quota ran out


def test_image_result_url_must_be_https():
    with pytest.raises(RuntimeError):
        thumbnails._fetch_result_url("file:///home/user/.ssh/id_ed25519")
    with pytest.raises(RuntimeError):
        thumbnails._fetch_result_url("http://example.com/x.png")
