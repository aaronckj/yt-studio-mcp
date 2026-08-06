"""Playlist tools."""

from __future__ import annotations

from ..client import get_yt, preview


def register(mcp) -> None:
    @mcp.tool()
    def list_playlists(limit: int = 50) -> list[dict]:
        """List the channel's playlists."""
        yt = get_yt()
        items = yt.paginate(
            yt.data.playlists(), "list", limit=limit, part="snippet,contentDetails", mine=True
        )
        return [
            {
                "id": p["id"],
                "title": p["snippet"]["title"],
                "items": p["contentDetails"]["itemCount"],
            }
            for p in items
        ]

    @mcp.tool()
    def create_playlist(
        title: str, description: str = "", privacy: str = "public", dry_run: bool = False
    ) -> dict:
        """Create a playlist. privacy: public|unlisted|private."""
        if dry_run:
            return preview("create_playlist", {"title": title, "privacy": privacy})
        yt = get_yt()
        res = yt.call(
            yt.data.playlists().insert(
                part="snippet,status",
                body={
                    "snippet": {"title": title, "description": description},
                    "status": {"privacyStatus": privacy},
                },
            ),
            op="insert",
        )
        return {"created": res["id"], "title": title}

    @mcp.tool()
    def update_playlist(
        playlist_id: str,
        title: str | None = None,
        description: str | None = None,
        privacy: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Rename a playlist or change its description/privacy.

        Only supplied fields change. A playlist's title is the label a viewer
        sees on every video in it, so this is how a series gets named without
        recreating the playlist and losing its members and URL.
        """
        yt = get_yt()
        cur = yt.call(
            yt.data.playlists().list(part="snippet,status", id=playlist_id), op="list"
        )["items"][0]
        snippet = cur["snippet"]
        status = cur.get("status", {})
        changes = {}
        if title is not None:
            snippet["title"] = title
            changes["title"] = title
        if description is not None:
            snippet["description"] = description
            changes["description"] = f"{len(description)} chars"
        if privacy is not None:
            status["privacyStatus"] = privacy
            changes["privacy"] = privacy
        if dry_run:
            return preview("update_playlist", {"playlist_id": playlist_id, **changes})
        yt.call(
            yt.data.playlists().update(
                part="snippet,status",
                body={"id": playlist_id, "snippet": snippet, "status": status},
            ),
            op="update",
        )
        return {"updated": playlist_id, "changes": changes}

    @mcp.tool()
    def delete_playlist(playlist_id: str, dry_run: bool = False) -> dict:
        """Delete a playlist (videos are not deleted)."""
        if dry_run:
            return preview("delete_playlist", {"playlist_id": playlist_id})
        yt = get_yt()
        yt.call(yt.data.playlists().delete(id=playlist_id), op="delete")
        return {"deleted": playlist_id}

    @mcp.tool()
    def add_to_playlist(
        playlist_id: str, video_id: str, position: int | None = None, dry_run: bool = False
    ) -> dict:
        """Add a video to a playlist, optionally at a specific position."""
        if dry_run:
            return preview(
                "add_to_playlist",
                {"playlist_id": playlist_id, "video_id": video_id, "position": position},
            )
        yt = get_yt()
        snippet: dict = {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        }
        if position is not None:
            snippet["position"] = position
        res = yt.call(
            yt.data.playlistItems().insert(part="snippet", body={"snippet": snippet}),
            op="insert",
        )
        return {"added": res["id"], "video_id": video_id, "playlist_id": playlist_id}

    @mcp.tool()
    def remove_from_playlist(playlist_item_id: str, dry_run: bool = False) -> dict:
        """Remove an item from a playlist by playlist-item id (from list results)."""
        if dry_run:
            return preview("remove_from_playlist", {"playlist_item_id": playlist_item_id})
        yt = get_yt()
        yt.call(yt.data.playlistItems().delete(id=playlist_item_id), op="delete")
        return {"removed": playlist_item_id}

    @mcp.tool()
    def list_playlist_items(playlist_id: str, limit: int = 50) -> list[dict]:
        """List videos in a playlist."""
        yt = get_yt()
        items = yt.paginate(
            yt.data.playlistItems(), "list", limit=limit, part="snippet", playlistId=playlist_id
        )
        return [
            {
                "item_id": i["id"],
                "video_id": i["snippet"]["resourceId"]["videoId"],
                "title": i["snippet"]["title"],
                "position": i["snippet"].get("position"),
            }
            for i in items
        ]
