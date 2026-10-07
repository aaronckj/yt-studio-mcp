"""Channel and video tools."""

from __future__ import annotations

from pathlib import Path

from mcp.types import ToolAnnotations

from ..client import get_yt, preview
from ..validate import check_future_schedule, check_privacy, is_past

THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024
THUMBNAIL_SUFFIXES = (".jpg", ".jpeg", ".png")


def check_thumbnail_file(image_path: str) -> str | None:
    """Reject anything YouTube would refuse (or that is not a thumbnail at all)
    before spending 50 quota units -- and before an arbitrary local file can
    be published as a channel image."""
    p = Path(image_path).expanduser()
    if p.suffix.lower() not in THUMBNAIL_SUFFIXES:
        return f"thumbnail must be .jpg/.jpeg/.png (got {p.suffix or 'no extension'})"
    if not p.is_file():
        return f"no such file: {image_path}"
    size = p.stat().st_size
    if size > THUMBNAIL_MAX_BYTES:
        return f"thumbnail is {size} bytes; YouTube's limit is {THUMBNAIL_MAX_BYTES}"
    return None


def _video_summary(item: dict) -> dict:
    sn = item.get("snippet", {})
    st = item.get("statistics", {})
    vid = item.get("id")
    if not isinstance(vid, str):
        vid = sn.get("resourceId", {}).get("videoId")
    out = {
        "id": vid,
        "title": sn.get("title"),
        "published": sn.get("publishedAt"),
        "privacy": item.get("status", {}).get("privacyStatus"),
        "views": st.get("viewCount"),
        "likes": st.get("likeCount"),
        "comments": st.get("commentCount"),
    }
    # Only present on scheduled videos. Without it a schedule cannot be checked
    # after the fact -- publishedAt shows the UPLOAD time, not the release time.
    scheduled = item.get("status", {}).get("publishAt")
    if scheduled:
        out["scheduled"] = scheduled
    return out


def register(mcp) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def channel_info() -> dict:
        """Get the authorized channel's profile, statistics, and uploads playlist id."""
        yt = get_yt()
        res = yt.call(
            yt.data.channels().list(part="snippet,statistics,contentDetails", mine=True),
            op="list",
        )
        items = res.get("items", [])
        if not items:
            return {"error": "no channel for authorized account"}
        ch = items[0]
        return {
            "id": ch["id"],
            "title": ch["snippet"]["title"],
            "description": ch["snippet"].get("description", ""),
            "subscribers": ch["statistics"].get("subscriberCount"),
            "views": ch["statistics"].get("viewCount"),
            "video_count": ch["statistics"].get("videoCount"),
            "uploads_playlist": ch["contentDetails"]["relatedPlaylists"]["uploads"],
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def update_channel(
        description: str | None = None,
        keywords: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Update the channel description or keywords.

        The description is the channel's pitch — the first thing a visitor
        reads and the only place the channel can state what it actually is.
        keywords is a space-separated string; quote multi-word phrases.
        """
        yt = get_yt()
        items = yt.call(
            yt.data.channels().list(part="brandingSettings", mine=True), op="list"
        ).get("items", [])
        if not items:
            return {"error": "no channel for authorized account"}
        cur = items[0]
        branding = cur.get("brandingSettings", {})
        ch = branding.setdefault("channel", {})
        changes = {}
        if description is not None:
            ch["description"] = description
            changes["description"] = f"{len(description)} chars"
        if keywords is not None:
            ch["keywords"] = keywords
            changes["keywords"] = keywords
        if not changes:
            return {"error": "nothing to update"}
        if dry_run:
            return preview("update_channel", changes)
        yt.call(
            yt.data.channels().update(
                part="brandingSettings",
                body={"id": cur["id"], "brandingSettings": branding},
            ),
            op="update",
        )
        return {"updated": cur["id"], "changes": changes}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_subscribers(limit: int = 50) -> dict:
        """List PUBLIC subscribers (YouTube only reveals subscribers who set
        their subscriptions public — most don't; totals live in channel_info)."""
        yt = get_yt()
        items = yt.paginate(
            yt.data.subscriptions(),
            "list",
            limit=limit,
            part="subscriberSnippet",
            mySubscribers=True,
        )
        return {
            "public_subscribers": [
                {
                    "title": s["subscriberSnippet"].get("title"),
                    "channel_id": s["subscriberSnippet"].get("channelId"),
                }
                for s in items
            ],
            "note": "private-mode subscribers are not listable by design",
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_videos(limit: int = 25) -> list[dict]:
        """List the channel's uploaded videos, newest first, with basic stats."""
        yt = get_yt()
        uploads = channel_info()["uploads_playlist"]
        playlist_items = yt.paginate(
            yt.data.playlistItems(), "list", limit=limit, part="snippet", playlistId=uploads
        )
        ids = [i["snippet"]["resourceId"]["videoId"] for i in playlist_items]
        if not ids:
            return []
        res = yt.call(
            yt.data.videos().list(part="snippet,statistics,status", id=",".join(ids)),
            op="list",
        )
        return [_video_summary(v) for v in res.get("items", [])]

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_video(video_id: str) -> dict:
        """Get full metadata and statistics for one video."""
        yt = get_yt()
        res = yt.call(
            yt.data.videos().list(
                part="snippet,statistics,status,contentDetails", id=video_id
            ),
            op="list",
        )
        items = res.get("items", [])
        if not items:
            return {"error": f"video not found: {video_id}"}
        v = items[0]
        out = _video_summary(v)
        out.update(
            {
                "description": v["snippet"].get("description", ""),
                "tags": v["snippet"].get("tags", []),
                "category_id": v["snippet"].get("categoryId"),
                "duration": v.get("contentDetails", {}).get("duration"),
            }
        )
        return out

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def update_video(
        video_id: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        category_id: str | None = None,
        privacy: str | None = None,
        publish_at: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Update video metadata. Only supplied fields change; others are preserved.

        privacy: private|unlisted|public. publish_at (RFC3339 WITH an offset,
        e.g. 2026-08-07T17:00:00Z, must be in the future) reschedules a video
        and keeps it private until then; combining it with privacy=public or
        unlisted is refused rather than guessed at. Pass publish_at="" to clear
        the schedule and leave the video private.

        Publishing now: privacy="public" alone is enough -- any existing
        schedule (including a stale, already-past one that YouTube failed to
        fire) is cleared in the same update, because YouTube rejects a
        non-private video that still carries publishAt.
        """
        if privacy is not None and (err := check_privacy(privacy)):
            return {"error": err}
        if publish_at:
            if privacy is not None and privacy != "private":
                return {
                    "error": f"publish_at schedules a PRIVATE video to go public later; "
                    f"it cannot be combined with privacy={privacy!r}. Pass one or "
                    "the other."
                }
            if err := check_future_schedule(publish_at):
                return {"error": err}
        yt = get_yt()
        items = yt.call(
            yt.data.videos().list(part="snippet,status", id=video_id), op="list"
        ).get("items", [])
        if not items:
            return {"error": f"video not found: {video_id}"}
        current = items[0]
        snippet = current["snippet"]
        status = current["status"]
        before = {
            "privacy": status.get("privacyStatus"),
            "publish_at": status.get("publishAt"),
        }
        changes: dict = {}
        if title is not None:
            snippet["title"] = title
            changes["title"] = title
        if description is not None:
            snippet["description"] = description
            changes["description"] = f"{len(description)} chars"
        if tags is not None:
            snippet["tags"] = tags
            changes["tags"] = tags
        if category_id is not None:
            snippet["categoryId"] = category_id
            changes["category_id"] = category_id
        if privacy is not None:
            status["privacyStatus"] = privacy
            changes["privacy"] = privacy
            if privacy != "private" and status.pop("publishAt", None):
                # Re-sending the old publishAt with a non-private status is
                # rejected (invalidPublishAt) -- the reason "publish now" on a
                # missed schedule used to fail.
                changes["publish_at"] = f"cleared (privacy={privacy})"
        if publish_at is not None:
            if publish_at == "":
                status.pop("publishAt", None)
                changes["publish_at"] = "cleared"
            else:
                status["publishAt"] = publish_at
                changes["publish_at"] = publish_at
                if status.get("privacyStatus") != "private":
                    status["privacyStatus"] = "private"
                    changes["privacy"] = "private (required by publish_at)"
        if not changes:
            return {"error": "nothing to update"}
        if (
            publish_at is None
            and status.get("privacyStatus") == "private"
            and is_past(status.get("publishAt"))
        ):
            return {
                "error": f"video {video_id} carries a schedule that is already past "
                f"({status['publishAt']}) -- YouTube did not fire it and will reject "
                "any update that re-sends it. Decide explicitly: privacy='public' "
                "to publish now, publish_at=<future time> to reschedule, or "
                "publish_at='' to unschedule.",
                "before": before,
            }
        goes_public = (
            status.get("privacyStatus") == "public" and before["privacy"] != "public"
        )
        if dry_run:
            return preview(
                "update_video",
                {"video_id": video_id, "before": before, "goes_public_now": goes_public,
                 **changes},
            )
        body = {"id": video_id, "snippet": snippet, "status": status}
        yt.call(yt.data.videos().update(part="snippet,status", body=body), op="update")
        return {"updated": video_id, "before": before, "goes_public_now": goes_public,
                "changes": changes}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def set_thumbnail(video_id: str, image_path: str, dry_run: bool = False) -> dict:
        """Set a custom thumbnail from a local image file (JPG/PNG, <=2MB)."""
        if err := check_thumbnail_file(image_path):
            return {"error": err}
        if dry_run:
            return preview("set_thumbnail", {"video_id": video_id, "image_path": image_path})
        from googleapiclient.http import MediaFileUpload

        yt = get_yt()
        yt.call(
            yt.data.thumbnails().set(
                videoId=video_id, media_body=MediaFileUpload(image_path)
            ),
            op="thumbnails.set",
        )
        # Keep a local copy of what actually went live. Without this the
        # published image existed only wherever it happened to be built --
        # typically a share or a /tmp path that later got cleaned up.
        from .thumbnails import archive_thumbnail

        saved = archive_thumbnail(image_path, video_id=video_id,
                                  meta={"via": "set_thumbnail"})
        out = {"thumbnail_set": video_id}
        if saved:
            out["archived_to"] = saved
        return out

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def upload_video(
        file_path: str,
        title: str,
        description: str = "",
        tags: list[str] | None = None,
        privacy: str = "private",
        publish_at: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Upload a video (resumable). privacy: private|unlisted|public. publish_at
        (RFC3339 with offset, in the future) schedules publication and requires
        privacy=private -- anything else is refused, never uploaded public."""
        if err := check_privacy(privacy):
            return {"error": err}
        if publish_at:
            if privacy != "private":
                return {"error": "publish_at requires privacy='private' (the video goes "
                        "public at publish_at); refusing to upload it as " + privacy}
            if err := check_future_schedule(publish_at):
                return {"error": err}
        if not Path(file_path).expanduser().is_file():
            return {"error": f"no such file: {file_path}"}
        if dry_run:
            return preview(
                "upload_video",
                {"file_path": file_path, "title": title, "privacy": privacy,
                 "publish_at": publish_at, "quota_cost": 1600},
            )
        from googleapiclient.http import MediaFileUpload

        yt = get_yt()
        status: dict = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
        if publish_at:
            status["publishAt"] = publish_at
        body = {
            "snippet": {"title": title, "description": description, "tags": tags or []},
            "status": status,
        }
        media = MediaFileUpload(
            str(Path(file_path).expanduser()), chunksize=8 * 1024 * 1024, resumable=True
        )
        request = yt.data.videos().insert(part="snippet,status", body=body, media_body=media)
        response = yt.upload(request, op="videos.insert")
        return {"uploaded": response["id"], "title": title, "privacy": privacy,
                "publish_at": publish_at}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def delete_video(video_id: str, dry_run: bool = False) -> dict:
        """Permanently delete a video. Irreversible."""
        yt = get_yt()
        if dry_run:
            return preview("delete_video", {"video_id": video_id, "irreversible": True})
        yt.call(yt.data.videos().delete(id=video_id), op="delete")
        return {"deleted": video_id}
