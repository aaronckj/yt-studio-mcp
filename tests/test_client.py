import json

import pytest
from googleapiclient.errors import HttpError

from yt_studio_mcp.client import COSTS, YT, ApiError, preview


class FakeResp:
    def __init__(self, status):
        self.status = status
        self.reason = "err"


class FakeRequest:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self.result


def http_error(status, reason, message="boom"):
    content = json.dumps(
        {"error": {"message": message, "errors": [{"reason": reason}]}}
    ).encode()
    return HttpError(FakeResp(status), content)


def test_call_success_tracks_quota():
    yt = YT(data_service=object())
    out = yt.call(FakeRequest(result={"items": []}), op="videos.insert")
    assert out == {"items": []}
    assert yt.quota.spent == COSTS["videos.insert"]
    assert yt.quota.calls == 1


def test_call_maps_errors_with_hint():
    yt = YT(data_service=object())
    with pytest.raises(ApiError) as ei:
        yt.call(FakeRequest(error=http_error(403, "quotaExceeded")), op="list")
    assert ei.value.reason == "quotaExceeded"
    assert "quota" in ei.value.hint.lower()
    assert ei.value.status == 403


def test_op_cost_fallback():
    yt = YT(data_service=object())
    yt.call(FakeRequest(result={}), op="comments.update")
    assert yt.quota.spent == COSTS["update"]


def test_preview_shape():
    p = preview("update_video", {"video_id": "x", "title": "t"})
    assert p["preview"] is True
    assert p["would"]["title"] == "t"


def test_paginate_stops_at_limit():
    class FakeResource:
        def __init__(self):
            self.pages = [
                {"items": [{"id": i} for i in range(50)], "nextPageToken": "t2"},
                {"items": [{"id": i} for i in range(50, 100)]},
            ]
            self.n = 0

        def list(self, **kw):
            page = self.pages[self.n]
            self.n += 1
            return FakeRequest(result=page)

    yt = YT(data_service=object())
    items = yt.paginate(FakeResource(), "list", limit=60)
    assert len(items) == 60


def test_quota_ledger_survives_restart_and_can_be_shared(tmp_path, monkeypatch):
    from yt_studio_mcp.client import QuotaTracker

    shared = tmp_path / "shared-ledger.json"
    monkeypatch.setenv("YT_MCP_QUOTA_LEDGER", str(shared))
    YT(data_service=object()).call(FakeRequest(result={}), op="update")
    YT(data_service=object()).call(FakeRequest(result={}), op="list")  # "restart"
    assert QuotaTracker.today()["units"] == COSTS["update"] + COSTS["list"]
    data = json.loads(shared.read_text())
    data["day"] = "1999-01-01"
    shared.write_text(json.dumps(data))
    assert QuotaTracker.today()["units"] == 0  # a new Pacific day starts at zero


def test_failed_upload_counts_quota():
    class Up:
        def next_chunk(self):
            raise http_error(400, "invalidPublishAt")

    yt = YT(data_service=object())
    with pytest.raises(ApiError):
        yt.upload(Up())
    assert yt.quota.spent == COSTS["videos.insert"]
