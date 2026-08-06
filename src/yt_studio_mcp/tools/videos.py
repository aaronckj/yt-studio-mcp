"""Channel and video tools."""

from __future__ import annotations

from ..client import get_yt, preview


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
    @mcp.tool()
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

    @mcp.tool()
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
        cur = yt.call(
            yt.data.channels().list(part="brandingSettings", mine=True), op="list"
        )["items"][0]
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

        publish_at (RFC3339, e.g. 2026-08-07T17:00:00Z) reschedules a video.
        YouTube only honours it while privacyStatus is private, so this forces
        private unless an explicit privacy is passed. Pass publish_at="" to
        clear the schedule and leave the video private indefinitely.
        """
        yt = get_yt()
        current = yt.call(
            yt.data.videos().list(part="snippet,status", id=video_id), op="list"
        )["items"][0]
        snippet = current["snippet"]
        status = current["status"]
        changes = {}
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
        if publish_at is not None:
            if publish_at == "":
                status.pop("publishAt", None)
                changes["publish_at"] = "cleared"
            else:
                status["publishAt"] = publish_at
                changes["publish_at"] = publish_at
                # A scheduled video must be private, or YouTube drops the time.
                if status.get("privacyStatus") != "private":
                    status["privacyStatus"] = "private"
                    changes["privacy"] = "private (forced by publish_at)"
        if dry_run:
            return preview("update_video", {"video_id": video_id, **changes})
        body = {"id": video_id, "snippet": snippet, "status": status}
        yt.call(yt.data.videos().update(part="snippet,status", body=body), op="update")
        return {"updated": video_id, "changes": changes}

    @mcp.tool()
    def set_thumbnail(video_id: str, image_path: str, dry_run: bool = False) -> dict:
        """Set a custom thumbnail from a local image file (JPG/PNG, <2MB)."""
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
        return {"thumbnail_set": video_id}

    @mcp.tool()
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
        (RFC3339) schedules publication and requires privacy=private."""
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
        media = MediaFileUpload(file_path, chunksize=8 * 1024 * 1024, resumable=True)
        request = yt.data.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            _, response = request.next_chunk()
        yt.quota.add(1600)
        return {"uploaded": response["id"], "title": title, "privacy": privacy}

    @mcp.tool()
    def delete_video(video_id: str, dry_run: bool = False) -> dict:
        """Permanently delete a video. Irreversible."""
        yt = get_yt()
        if dry_run:
            return preview("delete_video", {"video_id": video_id, "irreversible": True})
        yt.call(yt.data.videos().delete(id=video_id), op="delete")
        return {"deleted": video_id}
