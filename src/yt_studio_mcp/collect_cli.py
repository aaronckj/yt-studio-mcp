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


def run_collect(start: str, end: str, max_per_user: int, exclude: list[str],
                comment_limit: int | None = None) -> int:
    from .client import get_yt
    from .tools.giveaway import COMMENTS_PER_VIDEO_DEFAULT, collect_raw, freeze_snapshot

    yt = get_yt()
    collected = collect_raw(yt, comment_limit or COMMENTS_PER_VIDEO_DEFAULT)
    summary = freeze_snapshot(collected, start, end, max_per_user, exclude)
    summary["quota_spent"] = yt.quota.spent
    print(json.dumps(summary, indent=2))
    if not summary["complete"]:
        notify(
            f"⚠️ Giveaway entries frozen INCOMPLETE: {len(summary['truncated_videos'])} "
            f"video(s) hit the comment limit. Re-collect with a higher --comment-limit "
            f"before drawing.\n{summary['snapshot_path']}"
        )
        return 3
    notify(
        f"Giveaway entries frozen: {summary['entries']} entries from "
        f"{summary['participants']} participants. Hash {summary['entry_hash'][:12]}. "
        f"Ready to draw:\n{summary['snapshot_path']}"
    )
    return 0


def main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="yt-studio-mcp collect")
    p.add_argument("--start", required=True, help="RFC3339 window start")
    p.add_argument("--end", required=True, help="RFC3339 window end")
    p.add_argument("--max", type=int, default=5, dest="max_per_user")
    p.add_argument("--exclude", default="", help="comma-separated channel ids")
    p.add_argument("--comment-limit", type=int, default=None,
                   help="max top-level comments read per video (exit 3 if any video "
                   "has more: the snapshot is incomplete)")
    a = p.parse_args(argv)
    exclude = [x.strip() for x in a.exclude.split(",") if x.strip()]
    try:
        return run_collect(a.start, a.end, a.max_per_user, exclude, a.comment_limit)
    except Exception as exc:  # noqa: BLE001
        logger.error("collect failed: %s", exc)
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        notify(f"⚠️ Giveaway collection FAILED: {exc}")
        return 1
