"""Draw integrity: tamper evidence, redraw trail, completeness, reproducibility."""

import hashlib
import json

import pytest

from yt_studio_mcp import client
from yt_studio_mcp.client import YT
from yt_studio_mcp.tools import giveaway
from yt_studio_mcp.tools.giveaway import (
    build_entries,
    canonical_hash,
    collect_raw,
    csv_safe,
    deterministic_draw,
    freeze_snapshot,
)

from .fakes import Req, tools_of

START, END = "2026-07-24T00:00:00Z", "2026-08-31T23:59:59Z"


def _entries(k=30):
    return [
        {"channel_id": f"ch{i % 7}", "video_id": f"v{i}", "comment_id": f"c{i}",
         "author": f"user{i % 7}", "published": "2026-08-01T00:00:00Z"}
        for i in range(k)
    ]


@pytest.fixture
def gdir(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_MCP_CONFIG_DIR", str(tmp_path))
    return tmp_path / "giveaways"


def _snapshot(complete=True, truncated=None):
    raw = [
        {"author_channel_id": e["channel_id"], "author": e["author"],
         "video_id": e["video_id"], "comment_id": e["comment_id"],
         "published": e["published"]}
        for e in _entries()
    ]
    collected = {"own_id": "OWN", "video_ids": [f"v{i}" for i in range(30)], "raw": raw,
                 "truncated_videos": truncated or [], "uploads_truncated": not complete}
    return freeze_snapshot(collected, START, END, 5, [])["snapshot_path"]


def test_v2_draw_is_reproducible_without_python_random():
    entries = _entries()
    digest = canonical_hash(entries)
    ranked = sorted(entries, key=lambda e: hashlib.sha256(
        f"v2|seed-1|{digest}|{e['channel_id']}|{e['video_id']}|{e['comment_id']}".encode()
    ).hexdigest())
    expected, seen = [], set()
    for e in ranked:
        if e["channel_id"] not in seen:
            seen.add(e["channel_id"])
            expected.append(e)
    assert deterministic_draw(entries, 3, "seed-1", algo="v2") == expected[:3]


def test_v2_binds_seed_to_entry_set():
    """The same seed over a different entry set gives an unrelated order."""
    a = _entries()
    b = _entries() + [{"channel_id": "chX", "video_id": "vX", "comment_id": "cX",
                       "author": "x", "published": "2026-08-01T00:00:00Z"}]
    ra = [e["comment_id"] for e in giveaway._draw_v2(a, "s")]
    rb = [e["comment_id"] for e in giveaway._draw_v2(b, "s") if e["comment_id"] != "cX"]
    assert ra != rb


def test_naive_window_rejected_and_ties_are_order_independent():
    with pytest.raises(ValueError):
        build_entries([], "2026-07-24T00:00:00", END, 5, [])
    cs = [
        {"author_channel_id": "a", "video_id": "v2", "comment_id": "c2",
         "published": "2026-08-01T00:00:00Z"},
        {"author_channel_id": "a", "video_id": "v1", "comment_id": "c1",
         "published": "2026-08-01T00:00:00Z"},
    ]
    one = build_entries(cs, START, END, 1, [])
    two = build_entries(list(reversed(cs)), START, END, 1, [])
    assert one == two and one[0]["video_id"] == "v1"


def test_draw_rejects_tampered_entries_and_meta(gdir):
    t = tools_of(giveaway)
    path = _snapshot()
    snap = json.loads(open(path).read())
    snap["max_entries_per_user"] = 99
    open(path, "w").write(json.dumps(snap))
    assert "meta" in t["draw_winners"](path, n=2, seed="s")["error"]
    path2 = _snapshot()
    assert path2 != path  # same-second collections never overwrite each other
    snap = json.loads(open(path2).read())
    snap["entries"][0]["channel_id"] = "ATTACKER"
    open(path2, "w").write(json.dumps(snap))
    assert "hash mismatch" in t["draw_winners"](path2, n=2, seed="s")["error"]


def test_incomplete_snapshot_needs_explicit_override(gdir):
    t = tools_of(giveaway)
    path = _snapshot(truncated=["v3"])
    assert "INCOMPLETE" in t["draw_winners"](path, n=2, seed="s")["error"]
    assert t["draw_winners"](path, n=2, seed="s", allow_incomplete=True)["winners"]


def test_redraw_needs_reason_and_is_logged(gdir):
    t = tools_of(giveaway)
    path = _snapshot()
    first = t["draw_winners"](path, n=2, seed="s1")
    assert "already drawn" in t["draw_winners"](path, n=2, seed="s2")["error"]
    second = t["draw_winners"](path, n=2, seed="s2", redraw_reason="winner ineligible")
    assert second["prior_draws"] == 1
    log = giveaway.read_draw_log(giveaway.resolve_giveaway_path(path))
    assert [r["seed"] for r in log] == ["s1", "s2"]
    assert log[1]["redraw_reason"] == "winner ineligible"
    assert first["winners"]


def test_rerun_same_draw_keeps_verification_codes(gdir):
    t = tools_of(giveaway)
    path = _snapshot()
    d = t["draw_winners"](path, n=2, seed="s1")
    w = d["winners"][0]["channel_id"]
    code = t["make_verification_code"](d["audit_path"], w)["code"]
    again = t["draw_winners"](path, n=2, seed="s1")
    assert again["reused_existing_audit"] is True
    assert t["make_verification_code"](again["audit_path"], w) == {
        "channel_id": w, "code": code, "existing": True}


def test_edited_audit_winners_are_detected(gdir):
    t = tools_of(giveaway)
    path = _snapshot()
    d = t["draw_winners"](path, n=2, seed="s1")
    audit = json.loads(open(d["audit_path"]).read())
    audit["winners"][0]["channel_id"] = "ATTACKER"
    open(d["audit_path"], "w").write(json.dumps(audit))
    assert "modified" in t["make_verification_code"](d["audit_path"], "ATTACKER")["error"]
    assert "error" in t["winner_announcement"](d["audit_path"])


def test_draw_log_chain_detects_edits(gdir):
    t = tools_of(giveaway)
    path = _snapshot()
    t["draw_winners"](path, n=2, seed="s1")
    t["draw_winners"](path, n=2, seed="s2", redraw_reason="r")
    log = giveaway._draw_log_path(giveaway.resolve_giveaway_path(path))
    lines = log.read_text().splitlines()
    log.write_text(lines[1] + "\n")  # drop the first (unfavourable) draw
    assert "chain broken" in t["draw_winners"](path, n=2, seed="s3", redraw_reason="x")["error"]


def test_seed_never_reaches_a_filename(gdir):
    t = tools_of(giveaway)
    path = _snapshot()
    d = t["draw_winners"](path, n=1, seed="../../evil")
    assert "evil" not in d["audit_path"] and ".." not in d["audit_path"].split("giveaways")[1]


def test_csv_formula_injection_neutralised():
    assert csv_safe("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
    assert csv_safe("@SUM(1)").startswith("'")
    assert csv_safe("normal name") == "normal name"


class _Pages:
    def __init__(self, pages):
        self.pages = pages
        self.i = 0

    def list(self, **kw):
        p = self.pages[min(self.i, len(self.pages) - 1)]
        self.i += 1
        return Req(p)


class _Data:
    def __init__(self, n_comments):
        self.n = n_comments

    def channels(self):
        class C:
            def list(self, **kw):
                return Req({"items": [{"id": "OWN", "contentDetails": {
                    "relatedPlaylists": {"uploads": "UU"}}}]})
        return C()

    def playlistItems(self):  # noqa: N802
        return _Pages([{"items": [{"snippet": {"resourceId": {"videoId": "v1"}}}]}])

    def commentThreads(self):  # noqa: N802
        if getattr(self, "disabled", False):
            from googleapiclient.errors import HttpError

            class R:
                status = 403
                reason = "x"
            body = json.dumps({"error": {"errors": [{"reason": "commentsDisabled"}]}})

            class Bad:
                def list(self, **kw):
                    return Req(error=HttpError(R(), body.encode()))
            return Bad()

        def thread(i):
            return {"snippet": {"topLevelComment": {"id": f"c{i}", "snippet": {
                "authorChannelId": {"value": f"u{i}"}, "authorDisplayName": f"u{i}",
                "publishedAt": "2026-08-01T00:00:00Z"}}}}
        return _Pages([{"items": [thread(i) for i in range(self.n)], "nextPageToken": "more"}])


def test_collection_reports_truncation_instead_of_dropping_silently():
    yt = YT(data_service=_Data(n_comments=3))
    out = collect_raw(yt, comment_limit_per_video=3)
    assert out["truncated_videos"] == ["v1"]
    assert client  # module imported for monkeypatch symmetry


def test_comments_disabled_video_does_not_abort_collection():
    """2026-09-01: the giveaway close collection died on one such video."""
    data = _Data(n_comments=2)
    data.disabled = True
    out = collect_raw(YT(data_service=data))
    assert out["comments_disabled_videos"] == ["v1"] and out["truncated_videos"] == []
