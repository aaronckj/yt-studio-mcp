import json

from yt_studio_mcp.tools.giveaway import (
    build_entries,
    canonical_hash,
    deterministic_draw,
)

WINDOW_START = "2026-07-24T00:00:00Z"
WINDOW_END = "2026-08-31T23:59:59Z"


def comment(channel, video, cid, published, title=None):
    return {
        "author_channel_id": channel,
        "author": title or channel,
        "video_id": video,
        "comment_id": cid,
        "published": published,
    }


def test_build_entries_dedupes_per_video_and_caps():
    comments = [
        comment("chA", "v1", "c1", "2026-07-25T10:00:00Z"),
        comment("chA", "v1", "c2", "2026-07-25T11:00:00Z"),  # same video: no extra entry
        comment("chA", "v2", "c3", "2026-07-26T10:00:00Z"),
        comment("chA", "v3", "c4", "2026-07-27T10:00:00Z"),
        comment("chB", "v1", "c5", "2026-07-25T09:00:00Z"),
    ]
    entries = build_entries(
        comments, WINDOW_START, WINDOW_END, max_entries_per_user=2, exclude_channel_ids=[]
    )
    a_entries = [e for e in entries if e["channel_id"] == "chA"]
    assert len(a_entries) == 2  # capped at 2 despite 3 distinct videos
    assert [e["video_id"] for e in a_entries] == ["v1", "v2"]  # chronological first-N
    assert len([e for e in entries if e["channel_id"] == "chB"]) == 1


def test_build_entries_window_and_exclusions():
    comments = [
        comment("chA", "v1", "c1", "2026-07-23T23:59:59Z"),  # before window
        comment("chB", "v1", "c2", "2026-09-01T00:00:00Z"),  # after window
        comment("chC", "v1", "c3", "2026-08-01T00:00:00Z"),
        comment("chOWN", "v1", "c4", "2026-08-01T00:00:00Z"),
    ]
    entries = build_entries(
        comments, WINDOW_START, WINDOW_END, max_entries_per_user=5,
        exclude_channel_ids=["chOWN"],
    )
    assert [e["channel_id"] for e in entries] == ["chC"]


def test_draw_deterministic_and_distinct_channels():
    entries = [
        {"channel_id": f"ch{i % 7}", "video_id": f"v{i}", "comment_id": f"c{i}",
         "author": f"user{i % 7}", "published": f"2026-08-{(i % 28) + 1:02d}T00:00:00Z"}
        for i in range(30)
    ]
    w1 = deterministic_draw(entries, n=5, seed="stream-42")
    w2 = deterministic_draw(entries, n=5, seed="stream-42")
    assert w1 == w2  # same seed, same winners
    assert len({w["channel_id"] for w in w1}) == 5  # distinct channels
    w3 = deterministic_draw(entries, n=5, seed="other-seed")
    assert w3 != w1  # different seed, different draw (overwhelmingly)


def test_canonical_hash_tamper_detection():
    entries = [
        {"channel_id": "a", "video_id": "v", "comment_id": "c", "author": "x",
         "published": "2026-08-01T00:00:00Z"}
    ]
    h1 = canonical_hash(entries)
    tampered = json.loads(json.dumps(entries))
    tampered[0]["channel_id"] = "b"
    assert canonical_hash(tampered) != h1
    # order-independent
    e2 = entries + [
        {"channel_id": "b", "video_id": "v2", "comment_id": "c2", "author": "y",
         "published": "2026-08-02T00:00:00Z"}
    ]
    assert canonical_hash(list(reversed(e2))) == canonical_hash(e2)


def test_resolve_giveaway_path_confined():
    import pytest

    from yt_studio_mcp.tools import giveaway

    with pytest.raises(ValueError):
        giveaway.resolve_giveaway_path("/etc/passwd")
    with pytest.raises(ValueError):
        giveaway.resolve_giveaway_path(str(giveaway.SNAPSHOT_DIR / ".." / "x.json"))
    ok = giveaway.SNAPSHOT_DIR / "entries-x.json"
    assert giveaway.resolve_giveaway_path(str(ok)) == ok.resolve()
