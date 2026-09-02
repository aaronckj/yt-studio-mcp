"""Live broadcast, stream, and live chat tools."""

from __future__ import annotations

from ..client import get_yt, preview


def register(mcp) -> None:
    @mcp.tool()
    def list_broadcasts(status: str = "all", limit: int = 25) -> list[dict]:
        """List live broadcasts. status: all|active|upcoming|completed."""
        yt = get_yt()
        items = yt.paginate(
            yt.data.liveBroadcasts(),
            "list",
            limit=limit,
            part="snippet,status,contentDetails",
            broadcastStatus=status,
            broadcastType="all",
        )
        return [
            {
                "id": b["id"],
                "title": b["snippet"]["title"],
                "scheduled_start": b["snippet"].get("scheduledStartTime"),
                "status": b["status"]["lifeCycleStatus"],
                "privacy": b["status"]["privacyStatus"],
                "chat_id": b["snippet"].get("liveChatId"),
            }
            for b in items
        ]

    @mcp.tool()
    def create_broadcast(
        title: str,
        start_time: str,
        privacy: str = "public",
        description: str = "",
        twitch: bool = True,
        twitch_title: str = "",
        twitch_game: str = "",
        dry_run: bool = False,
    ) -> dict:
        """Schedule a live broadcast. start_time is RFC3339 (e.g. 2026-08-01T18:00:00Z).

        Multistream plugins (Aitum, Restream) copy the VIDEO to Twitch but NOT the
        metadata, so by default this also sets the Twitch title/category. Twitch
        reuses `title` unless twitch_title is given; twitch_game sets the category
        and is strongly recommended -- otherwise Twitch keeps the PREVIOUS stream's
        game. Pass twitch=False for a YouTube-only broadcast. If Twitch is not
        configured the YouTube broadcast still succeeds and the result says why.
        """
        if dry_run:
            return preview(
                "create_broadcast",
                {"title": title, "start_time": start_time, "privacy": privacy,
                 "twitch": twitch, "twitch_title": twitch_title or title,
                 "twitch_game": twitch_game},
            )
        yt = get_yt()
        res = yt.call(
            yt.data.liveBroadcasts().insert(
                part="snippet,status,contentDetails",
                body={
                    "snippet": {
                        "title": title,
                        "description": description,
                        "scheduledStartTime": start_time,
                    },
                    "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
                    "contentDetails": {"enableAutoStart": False, "enableAutoStop": True},
                },
            ),
            op="insert",
        )
        out = {"created": res["id"], "title": title, "scheduled_start": start_time}
        if twitch:
            # The YouTube broadcast is already created; a Twitch failure must be
            # reported, never raised, or a working broadcast looks like a failure.
            from ..twitch import TwitchNotConfigured, set_channel

            try:
                out["twitch"] = set_channel(twitch_title or title, twitch_game or None)
            except TwitchNotConfigured as exc:
                out["twitch"] = {"configured": False, "missing": exc.missing,
                                 "hint": str(exc)}
            except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
                out["twitch"] = {"error": str(exc)}
        return out

    @mcp.tool()
    def list_streams(limit: int = 10) -> list[dict]:
        """List reusable stream keys and their health."""
        yt = get_yt()
        items = yt.paginate(
            yt.data.liveStreams(), "list", limit=limit, part="snippet,cdn,status", mine=True
        )
        return [
            {
                "id": s["id"],
                "title": s["snippet"]["title"],
                "status": s["status"]["streamStatus"],
                "health": s["status"].get("healthStatus", {}).get("status"),
                "ingestion_address": s["cdn"]["ingestionInfo"]["ingestionAddress"],
            }
            for s in items
        ]

    @mcp.tool()
    def bind_broadcast(broadcast_id: str, stream_id: str, dry_run: bool = False) -> dict:
        """Bind a broadcast to a stream key."""
        if dry_run:
            return preview(
                "bind_broadcast", {"broadcast_id": broadcast_id, "stream_id": stream_id}
            )
        yt = get_yt()
        yt.call(
            yt.data.liveBroadcasts().bind(id=broadcast_id, part="id", streamId=stream_id),
            op="update",
        )
        return {"bound": broadcast_id, "stream": stream_id}

    @mcp.tool()
    def live_chat_messages(chat_id: str, limit: int = 200) -> list[dict]:
        """Read messages from a live chat (chat_id from list_broadcasts)."""
        yt = get_yt()
        res = yt.call(
            yt.data.liveChatMessages().list(
                liveChatId=chat_id, part="snippet,authorDetails", maxResults=min(limit, 2000)
            ),
            op="list",
        )
        return [
            {
                "id": m["id"],
                "author": m["authorDetails"]["displayName"],
                "author_channel_id": m["authorDetails"]["channelId"],
                "is_moderator": m["authorDetails"].get("isChatModerator", False),
                "text": m["snippet"].get("displayMessage", ""),
                "published": m["snippet"]["publishedAt"],
            }
            for m in res.get("items", [])
        ]

    @mcp.tool()
    def post_chat_message(chat_id: str, text: str, dry_run: bool = False) -> dict:
        """Post a message to a live chat as the channel."""
        if dry_run:
            return preview("post_chat_message", {"chat_id": chat_id, "text": text})
        yt = get_yt()
        res = yt.call(
            yt.data.liveChatMessages().insert(
                part="snippet",
                body={
                    "snippet": {
                        "liveChatId": chat_id,
                        "type": "textMessageEvent",
                        "textMessageDetails": {"messageText": text},
                    }
                },
            ),
            op="insert",
        )
        return {"posted": res["id"]}

    @mcp.tool()
    def delete_chat_message(message_id: str, dry_run: bool = False) -> dict:
        """Delete a live chat message."""
        if dry_run:
            return preview("delete_chat_message", {"message_id": message_id})
        yt = get_yt()
        yt.call(yt.data.liveChatMessages().delete(id=message_id), op="delete")
        return {"deleted": message_id}

    @mcp.tool()
    def ban_chat_user(
        chat_id: str, channel_id: str, duration_seconds: int | None = None, dry_run: bool = False
    ) -> dict:
        """Ban a user from live chat; temporary when duration_seconds given, else permanent."""
        if dry_run:
            return preview(
                "ban_chat_user",
                {"chat_id": chat_id, "channel_id": channel_id, "duration": duration_seconds},
            )
        yt = get_yt()
        snippet: dict = {
            "liveChatId": chat_id,
            "type": "temporary" if duration_seconds else "permanent",
            "bannedUserDetails": {"channelId": channel_id},
        }
        if duration_seconds:
            snippet["banDurationSeconds"] = duration_seconds
        res = yt.call(
            yt.data.liveChatBans().insert(part="snippet", body={"snippet": snippet}),
            op="insert",
        )
        return {"banned": channel_id, "ban_id": res["id"]}
