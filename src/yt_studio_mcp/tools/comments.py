"""Comment listing, posting, and moderation tools."""

from __future__ import annotations

from ..client import get_yt, preview


def _thread_summary(t: dict) -> dict:
    top = t["snippet"]["topLevelComment"]["snippet"]
    return {
        "thread_id": t["id"],
        "comment_id": t["snippet"]["topLevelComment"]["id"],
        "video_id": t["snippet"].get("videoId"),
        "author": top.get("authorDisplayName"),
        "author_channel_id": top.get("authorChannelId", {}).get("value"),
        "text": top.get("textOriginal", top.get("textDisplay", "")),
        "published": top.get("publishedAt"),
        "like_count": top.get("likeCount"),
        "reply_count": t["snippet"].get("totalReplyCount", 0),
    }


def register(mcp) -> None:
    @mcp.tool()
    def list_comments(
        video_id: str | None = None,
        limit: int = 100,
        search_terms: str | None = None,
    ) -> list[dict]:
        """List comment threads for one video, or channel-wide when video_id is omitted."""
        yt = get_yt()
        params: dict = {"part": "snippet", "order": "time", "textFormat": "plainText"}
        if video_id:
            params["videoId"] = video_id
        else:
            params["allThreadsRelatedToChannelId"] = _my_channel_id(yt)
        if search_terms:
            params["searchTerms"] = search_terms
        items = yt.paginate(yt.data.commentThreads(), "list", limit=limit, **params)
        return [_thread_summary(t) for t in items]

    @mcp.tool()
    def list_held_comments(limit: int = 100, status: str = "heldForReview") -> list[dict]:
        """Comments awaiting moderation, channel-wide.

        Held comments are invisible to the public until approved, so without
        this they simply accumulate unseen — the scan pipeline could flag them
        but nothing could show them for review. status: heldForReview |
        likelySpam | published. Approve or reject with moderate_comment.
        """
        yt = get_yt()
        items = yt.paginate(
            yt.data.commentThreads(),
            "list",
            limit=limit,
            part="snippet",
            allThreadsRelatedToChannelId=_my_channel_id(yt),
            moderationStatus=status,
            textFormat="plainText",
        )
        return [_thread_summary(t) for t in items]

    @mcp.tool()
    def post_video_question(video_id: str, text: str, dry_run: bool = False) -> dict:
        """Post a top-level comment on a video as the channel (e.g. a question
        prompt for viewers). Note: the API cannot pin comments — pin manually
        in YouTube Studio if desired."""
        if dry_run:
            return preview("post_video_question", {"video_id": video_id, "text": text})
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
        return {
            "posted": res["id"],
            "video_id": video_id,
            "note": "pin manually in YouTube Studio if desired (no pin API)",
        }

    @mcp.tool()
    def reply_to_comment(comment_id: str, text: str, dry_run: bool = False) -> dict:
        """Reply to a comment as the channel."""
        if dry_run:
            return preview("reply_to_comment", {"comment_id": comment_id, "text": text})
        yt = get_yt()
        res = yt.call(
            yt.data.comments().insert(
                part="snippet",
                body={"snippet": {"parentId": comment_id, "textOriginal": text}},
            ),
            op="insert",
        )
        return {"replied": res["id"], "parent": comment_id}

    @mcp.tool()
    def moderate_comment(comment_id: str, status: str, dry_run: bool = False) -> dict:
        """Set moderation status: published | heldForReview | rejected."""
        if status not in ("published", "heldForReview", "rejected"):
            return {"error": "status must be published|heldForReview|rejected"}
        if dry_run:
            return preview("moderate_comment", {"comment_id": comment_id, "status": status})
        yt = get_yt()
        yt.call(
            yt.data.comments().setModerationStatus(id=comment_id, moderationStatus=status),
            op="setModerationStatus",
        )
        return {"moderated": comment_id, "status": status}

    @mcp.tool()
    def mark_spam(comment_id: str, dry_run: bool = False) -> dict:
        """Flag a comment as spam."""
        if dry_run:
            return preview("mark_spam", {"comment_id": comment_id})
        yt = get_yt()
        yt.call(yt.data.comments().markAsSpam(id=comment_id), op="markAsSpam")
        return {"marked_spam": comment_id}

    @mcp.tool()
    def delete_comment(comment_id: str, dry_run: bool = False) -> dict:
        """Delete a comment authored by the channel (own comments only)."""
        if dry_run:
            return preview("delete_comment", {"comment_id": comment_id})
        yt = get_yt()
        yt.call(yt.data.comments().delete(id=comment_id), op="delete")
        return {"deleted": comment_id}


def _my_channel_id(yt) -> str:
    res = yt.call(yt.data.channels().list(part="id", mine=True), op="list")
    return res["items"][0]["id"]
