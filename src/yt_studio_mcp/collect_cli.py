"""Headless giveaway entry collection for cron (deadline automation).

    yt-studio-mcp collect --start <RFC3339> --end <RFC3339> [--max N] [--exclude id,id]

Walks every upload's comments, derives entries (one per distinct video per
commenter, capped), writes an auditable snapshot + SHA-256, prints a
summary, optional ntfy push. The random DRAW stays manual (public seed +
screen recording per official rules) — this only freezes the entry set.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from urllib.request import Request, urlopen

logger = logging.getLogger("yt_studio_mcp")


def notify(msg: str) -> None:
    url = os.environ.get("YT_MCP_NTFY_URL")
    if not url:
        return
    try:
        with urlopen(Request(url, data=msg.encode(), headers={"title": "Giveaway collection"})):
            pass
    except Exception as exc:  # noqa: BLE001 - best effort
        logger.error("ntfy failed: %s", exc)


def run_collect(start: str, end: str, max_per_user: int, exclude: list[str]) -> int:
    from .client import get_yt
    from .tools.giveaway import ALGO_VERSION, SNAPSHOT_DIR, build_entries, canonical_hash

    yt = get_yt()
    me = yt.call(yt.data.channels().list(part="id,contentDetails", mine=True), op="list")[
        "items"
    ][0]
    own_id = me["id"]
    uploads = me["contentDetails"]["relatedPlaylists"]["uploads"]
    video_items = yt.paginate(
        yt.data.playlistItems(), "list", limit=500, part="snippet", playlistId=uploads
    )
    video_ids = [i["snippet"]["resourceId"]["videoId"] for i in video_items]

    raw: list[dict] = []
    for vid in video_ids:
        threads = yt.paginate(
            yt.data.commentThreads(), "list", limit=2000, part="snippet",
            videoId=vid, order="time", textFormat="plainText",
        )
        for t in threads:
            top = t["snippet"]["topLevelComment"]["snippet"]
            raw.append({
                "author_channel_id": top.get("authorChannelId", {}).get("value", ""),
                "author": top.get("authorDisplayName", ""),
                "video_id": vid,
                "comment_id": t["snippet"]["topLevelComment"]["id"],
                "published": top["publishedAt"],
            })

    entries = build_entries(raw, start, end, max_per_user, [own_id, *exclude])
    digest = canonical_hash(entries)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    snapshot_path = SNAPSHOT_DIR / f"entries-{stamp}.json"
    snapshot_path.write_text(json.dumps({
        "algo": ALGO_VERSION, "window": {"start": start, "end": end},
        "max_entries_per_user": max_per_user, "excluded": [own_id, *exclude],
        "videos_scanned": len(video_ids), "comments_seen": len(raw),
        "entry_hash": digest, "entries": entries,
    }, indent=2))
    participants = len({e["channel_id"] for e in entries})
    summary = {
        "snapshot_path": str(snapshot_path), "entry_hash": digest,
        "entries": len(entries), "participants": participants,
        "videos_scanned": len(video_ids), "quota_spent": yt.quota.spent,
    }
    print(json.dumps(summary, indent=2))
    notify(
        f"Giveaway entries frozen: {len(entries)} entries from {participants} "
        f"participants. Hash {digest[:12]}. Ready to draw:\n{snapshot_path}"
    )
    return 0


def main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="yt-studio-mcp collect")
    p.add_argument("--start", required=True, help="RFC3339 window start")
    p.add_argument("--end", required=True, help="RFC3339 window end")
    p.add_argument("--max", type=int, default=5, dest="max_per_user")
    p.add_argument("--exclude", default="", help="comma-separated channel ids")
    a = p.parse_args(argv)
    exclude = [x.strip() for x in a.exclude.split(",") if x.strip()]
    try:
        return run_collect(a.start, a.end, a.max_per_user, exclude)
    except Exception as exc:  # noqa: BLE001
        logger.error("collect failed: %s", exc)
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        notify(f"⚠️ Giveaway collection FAILED: {exc}")
        return 1
