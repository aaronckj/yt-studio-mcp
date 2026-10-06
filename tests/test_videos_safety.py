"""update_video / upload_video must never publish, or fail to publish, by accident."""

import pytest

from yt_studio_mcp import client
from yt_studio_mcp.client import YT
from yt_studio_mcp.tools import videos

from .fakes import FakeData, tools_of, video

FUTURE = "2099-01-01T17:00:00Z"
PAST = "2020-01-01T17:00:00Z"


@pytest.fixture
def env(monkeypatch):
    def make(**vids):
        data = FakeData(vids)
        monkeypatch.setattr(client, "_yt", YT(data_service=data))
        return data, tools_of(videos)

    return make


def test_publish_now_clears_stale_publish_at(env):
    """The 2026-08-25 missed-schedule fix: privacy=public alone must work."""
    data, t = env(v1=video("v1", publish_at=PAST))
    out = t["update_video"]("v1", privacy="public")
    assert out["goes_public_now"] is True
    (op, body), = data.rec.writes()
    assert body["status"]["privacyStatus"] == "public"
    assert "publishAt" not in body["status"]


def test_public_plus_publish_at_is_refused_not_silently_private(env):
    data, t = env(v1=video("v1"))
    out = t["update_video"]("v1", privacy="public", publish_at=FUTURE)
    assert "error" in out
    assert data.rec.writes() == []


def test_past_or_naive_publish_at_refused(env):
    data, t = env(v1=video("v1"))
    assert "error" in t["update_video"]("v1", publish_at=PAST)
    assert "error" in t["update_video"]("v1", publish_at="2099-01-01T17:00:00")
    assert data.rec.writes() == []


def test_invalid_privacy_refused(env):
    data, t = env(v1=video("v1"))
    assert "error" in t["update_video"]("v1", privacy="Public")
    assert data.rec.writes() == []


def test_title_edit_on_stale_schedule_explains_instead_of_failing(env):
    data, t = env(v1=video("v1", publish_at=PAST))
    out = t["update_video"]("v1", title="new")
    assert "already past" in out["error"]
    assert data.rec.writes() == []


def test_title_edit_preserves_future_schedule(env):
    data, t = env(v1=video("v1", publish_at=FUTURE))
    t["update_video"]("v1", title="new")
    (_, body), = data.rec.writes()
    assert body["status"] == {"privacyStatus": "private", "publishAt": FUTURE}


def test_reschedule_forces_private(env):
    data, t = env(v1=video("v1", privacy="unlisted"))
    out = t["update_video"]("v1", publish_at=FUTURE)
    (_, body), = data.rec.writes()
    assert body["status"]["privacyStatus"] == "private"
    assert out["goes_public_now"] is False


def test_dry_run_writes_nothing_and_flags_public(env):
    data, t = env(v1=video("v1"))
    out = t["update_video"]("v1", privacy="public", dry_run=True)
    assert out["preview"] is True and out["would"]["goes_public_now"] is True
    assert data.rec.writes() == []


def test_missing_video_is_an_error_not_a_crash(env):
    _, t = env()
    assert "not found" in t["update_video"]("nope", title="x")["error"]


def test_upload_refuses_public_with_schedule(env, tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    _, t = env()
    assert "error" in t["upload_video"](str(f), "t", privacy="public", publish_at=FUTURE)
    assert "error" in t["upload_video"](str(f), "t", privacy="publc")
    assert "error" in t["upload_video"](str(tmp_path / "missing.mp4"), "t", dry_run=True)
    assert t["upload_video"](str(f), "t", publish_at=FUTURE, dry_run=True)["preview"]


def test_thumbnail_validated_before_quota(env, tmp_path):
    _, t = env()
    bad = tmp_path / "creds.json"
    bad.write_text("{}")
    assert "error" in t["set_thumbnail"]("v1", str(bad))
    big = tmp_path / "big.jpg"
    big.write_bytes(b"\0" * (2 * 1024 * 1024 + 1))
    assert "error" in t["set_thumbnail"]("v1", str(big))
    ok = tmp_path / "ok.jpg"
    ok.write_bytes(b"\xff\xd8")
    assert t["set_thumbnail"]("v1", str(ok), dry_run=True)["preview"]
    assert client._yt.quota.spent == 0
