"""Live broadcast, stream, and live chat tools."""

from __future__ import annotations

from mcp.types import ToolAnnotations

from ..client import get_yt, preview
from ..validate import check_future_schedule, check_privacy

LATENCIES = ("normal", "low", "ultraLow")
# contentDetails fields liveBroadcasts.update accepts. The update REPLACES the part, so every
# one is carried over from the readback. Not sent: boundStreamId / boundStreamLastUpdateTimeMs
# (read-only), enableLowLatency (deprecated; it fights latencyPreference), embedHtml (read-only).
_CD_WRITABLE = (
    "monitorStream",
    "enableEmbed",
    "enableDvr",
    "enableContentEncryption",
    "startWithSlate",
    "recordFromStart",
    "enableClosedCaptions",
    "closedCaptionsType",
    "projection",
    "enableAutoStart",
    "enableAutoStop",
    "latencyPreference",
)
_FIXED_LIFECYCLES = ("testing", "live", "complete", "revoked", "liveStarting", "testStarting")


def content_details_with_latency(cd: dict, latency: str) -> tuple[dict, list[str]]:
    """(contentDetails body for liveBroadcasts.update with only latencyPreference changed, notes).

    ultraLow does not support closed captions (YouTube refuses the update otherwise), so captions
    are switched off and that is noted."""
    body = {k: cd[k] for k in _CD_WRITABLE if k in cd}
    ms = cd.get("monitorStream")
    if isinstance(ms, dict):
        body["monitorStream"] = {
            k: ms[k] for k in ("enableMonitorStream", "broadcastStreamDelayMs") if k in ms
        }
    body["latencyPreference"] = latency
    notes = []
    if latency == "ultraLow":
        if body.get("closedCaptionsType") not in (None, "closedCaptionsDisabled") or body.get(
            "enableClosedCaptions"
        ):
            notes.append("ultraLow does not support live captions: closed captions were turned off")
        if "closedCaptionsType" in body or "enableClosedCaptions" in body:
            body["closedCaptionsType"] = "closedCaptionsDisabled"
            body["enableClosedCaptions"] = False
        notes.append("ultraLow caps the live stream at 1080p (no 1440p/4K live)")
    return body, notes


def register(mcp) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def create_broadcast(
        title: str,
        start_time: str,
        privacy: str = "public",
        description: str = "",
        twitch: bool = True,
        twitch_title: str = "",
        twitch_game: str = "",
        latency: str = "ultraLow",
        dry_run: bool = False,
    ) -> dict:
        """Schedule a live broadcast. start_time is RFC3339 (e.g. 2026-08-01T18:00:00Z).

        latency (normal|low|ultraLow, default ultraLow) is YouTube's latencyPreference.
        ultraLow keeps viewers a few seconds behind so the hosts can answer chat, but caps live
        at 1080p and has no live captions; change it with set_broadcast_latency before go-live.

        Multistream plugins (Aitum, Restream) copy the VIDEO to Twitch but NOT the
        metadata, so by default this also sets the Twitch title/category. Twitch
        reuses `title` unless twitch_title is given; twitch_game sets the category
        and is strongly recommended -- otherwise Twitch keeps the PREVIOUS stream's
        game. Pass twitch=False for a YouTube-only broadcast. If Twitch is not
        configured the YouTube broadcast still succeeds and the result says why.
        NOTE: privacy defaults to public -- the scheduled stream is listed on the
        channel as soon as it is created -- and the Twitch title/category change
        immediately, not at start_time.
        """
        if err := check_privacy(privacy):
            return {"error": err}
        if err := check_future_schedule(start_time):
            return {"error": err.replace("publish_at", "start_time")}
        if latency not in LATENCIES:
            return {"error": f"latency must be one of {', '.join(LATENCIES)}"}
        if dry_run:
            return preview(
                "create_broadcast",
                {
                    "title": title,
                    "start_time": start_time,
                    "privacy": privacy,
                    "latency": latency,
                    "listed_publicly_now": privacy == "public",
                    "twitch": twitch,
                    "twitch_title": twitch_title or title,
                    "twitch_game": twitch_game,
                },
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
                    "contentDetails": {
                        "enableAutoStart": False,
                        "enableAutoStop": True,
                        "latencyPreference": latency,
                    },
                },
            ),
            op="insert",
        )
        out = {
            "created": res["id"],
            "title": title,
            "scheduled_start": start_time,
            "latency": latency,
        }
        if twitch:
            # The YouTube broadcast is already created; a Twitch failure must be
            # reported, never raised, or a working broadcast looks like a failure.
            from ..twitch import TwitchNotConfigured, set_channel

            try:
                out["twitch"] = set_channel(twitch_title or title, twitch_game or None)
            except TwitchNotConfigured as exc:
                out["twitch"] = {"configured": False, "missing": exc.missing, "hint": str(exc)}
            except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
                out["twitch"] = {"error": str(exc)}
        return out

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def set_broadcast_latency(
        broadcast_id: str, latency: str = "ultraLow", dry_run: bool = False
    ) -> dict:
        """Set a scheduled broadcast's latencyPreference (normal|low|ultraLow); reads it back.

        Only before it goes live (YouTube fixes it at go-live). Every other contentDetails field
        (auto start/stop, DVR, monitor stream, embed ...) is kept. ultraLow turns live captions off
        and caps the live stream at 1080p. Costs 1 unit to read + 50 to update (0 when already set).
        """
        if latency not in LATENCIES:
            return {"error": f"latency must be one of {', '.join(LATENCIES)}"}
        yt = get_yt()

        def read():
            r = yt.call(
                yt.data.liveBroadcasts().list(part="id,status,contentDetails", id=broadcast_id),
                op="list",
            )
            return (r.get("items") or [None])[0]

        b = read()
        if not b:
            return {"error": f"no broadcast {broadcast_id} on this channel"}
        life = (b.get("status") or {}).get("lifeCycleStatus")
        cd = b.get("contentDetails") or {}
        before = cd.get("latencyPreference")
        if before == latency:
            return {
                "broadcast": broadcast_id,
                "changed": False,
                "latency_before": before,
                "latency_after": before,
                "lifecycle": life,
                "notes": [],
            }
        if life in _FIXED_LIFECYCLES:
            return {
                "error": f"broadcast {broadcast_id} is {life}: "
                "its latency can only change before it goes live"
            }
        body, notes = content_details_with_latency(cd, latency)
        if dry_run:
            return preview(
                "set_broadcast_latency",
                {
                    "broadcast_id": broadcast_id,
                    "from": before,
                    "to": latency,
                    "contentDetails": body,
                    "notes": notes,
                },
            )
        yt.call(
            yt.data.liveBroadcasts().update(
                part="contentDetails", body={"id": broadcast_id, "contentDetails": body}
            ),
            op="update",
        )
        after = ((read() or {}).get("contentDetails") or {}).get("latencyPreference")
        return {
            "broadcast": broadcast_id,
            "changed": after == latency,
            "latency_before": before,
            "latency_after": after,
            "lifecycle": life,
            "notes": notes,
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def bind_broadcast(broadcast_id: str, stream_id: str, dry_run: bool = False) -> dict:
        """Bind a broadcast to a stream key."""
        if dry_run:
            return preview("bind_broadcast", {"broadcast_id": broadcast_id, "stream_id": stream_id})
        yt = get_yt()
        yt.call(
            yt.data.liveBroadcasts().bind(id=broadcast_id, part="id", streamId=stream_id),
            op="update",
        )
        return {"bound": broadcast_id, "stream": stream_id}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def delete_chat_message(message_id: str, dry_run: bool = False) -> dict:
        """Delete a live chat message."""
        if dry_run:
            return preview("delete_chat_message", {"message_id": message_id})
        yt = get_yt()
        yt.call(yt.data.liveChatMessages().delete(id=message_id), op="delete")
        return {"deleted": message_id}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
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
