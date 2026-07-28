"""Per-video giveaway question comments.

Posts a formatted top-level comment (unique question + official-rules link)
on each video as the channel, and tracks which videos already have one so
re-runs never double-post. Pinning has no API: results include the YouTube
Studio comments URL for each video so the manual pin is one click.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ..client import get_yt, preview

STATE_PATH = Path.home() / ".config" / "yt-studio-mcp" / "question_comments.json"


def build_question_comment(question: str, rules_url: str) -> str:
    return (
        f"❓ QUESTION OF THE VIDEO: {question}\n\n"
        "Drop your answer in a comment — every comment on this video counts as "
        "a giveaway entry (answering is encouraged, not required).\n\n"
        f"Official rules: {rules_url}\n"
        "No purchase necessary. We never ask for payment or personal details "
        "in replies."
    )


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def studio_comments_url(video_id: str) -> str:
    return f"https://studio.youtube.com/video/{video_id}/comments"


def register(mcp) -> None:
    @mcp.tool()
    def post_giveaway_question(
        video_id: str,
        question: str,
        rules_url: str,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Post the formatted giveaway question comment (unique question +
        official-rules link) on a video as the channel. Tracks state locally and
        refuses to double-post unless force=True. Result includes the Studio URL
        for the one-click manual pin (no pin API exists)."""
        state = _load_state()
        if video_id in state and not force:
            return {
                "error": f"question already posted on {video_id} "
                f"(comment {state[video_id]['comment_id']}); use force=True to repost",
                "existing": state[video_id],
            }
        text = build_question_comment(question, rules_url)
        if dry_run:
            return preview(
                "post_giveaway_question", {"video_id": video_id, "text": text}
            )
        yt = get_yt()
        res = yt.call(
            yt.data.commentThreads().insert(
                part="snippet",
                body={
                    "snippet": {
                        "videoId": video_id,
                        "topLevelComment": {"snippet": {"textOriginal": text}},
                    }
                },
            ),
            op="insert",
        )
        comment_id = res["snippet"]["topLevelComment"]["id"] if "snippet" in res else res["id"]
        state[video_id] = {
            "comment_id": comment_id,
            "question": question,
            "posted_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        _save_state(state)
        return {
            "posted": comment_id,
            "video_id": video_id,
            "pin_here": studio_comments_url(video_id),
            "note": "open pin_here and pin the comment (no pin API)",
        }

    @mcp.tool()
    def giveaway_question_status() -> dict:
        """Cross-reference every upload against posted question comments:
        which videos still need one, which are posted (with pin links)."""
        yt = get_yt()
        me = yt.call(
            yt.data.channels().list(part="contentDetails", mine=True), op="list"
        )["items"][0]
        uploads = me["contentDetails"]["relatedPlaylists"]["uploads"]
        items = yt.paginate(
            yt.data.playlistItems(), "list", limit=500, part="snippet", playlistId=uploads
        )
        state = _load_state()
        posted, missing = [], []
        for i in items:
            vid = i["snippet"]["resourceId"]["videoId"]
            title = i["snippet"]["title"]
            if vid in state:
                posted.append(
                    {
                        "video_id": vid,
                        "title": title,
                        "question": state[vid]["question"],
                        "pin_here": studio_comments_url(vid),
                    }
                )
            else:
                missing.append({"video_id": vid, "title": title})
        return {"posted": posted, "missing": missing}
