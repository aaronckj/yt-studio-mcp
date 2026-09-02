"""Twitch channel tools -- the other half of going live when multistreaming."""

from __future__ import annotations

from ..client import preview
from ..twitch import Twitch, TwitchNotConfigured, set_channel


def register(mcp) -> None:
    @mcp.tool()
    def twitch_status() -> dict:
        """Whether Twitch is configured, and which channel it would write to."""
        try:
            tw = Twitch()
        except TwitchNotConfigured as exc:
            return {"configured": False, "missing": exc.missing, "hint": str(exc)}
        me = tw.me()
        ch = tw.get_channel()
        return {
            "configured": True,
            "login": me.get("login"),
            "broadcaster_id": me.get("id"),
            "title": ch.get("title"),
            "category": ch.get("game_name"),
        }

    @mcp.tool()
    def twitch_get_channel() -> dict:
        """Current Twitch title, category and tags."""
        ch = Twitch().get_channel()
        return {
            "title": ch.get("title"),
            "category": ch.get("game_name"),
            "category_id": ch.get("game_id"),
            "tags": ch.get("tags"),
            "language": ch.get("broadcaster_language"),
        }

    @mcp.tool()
    def twitch_set_channel(
        title: str = "",
        game_name: str = "",
        tags: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Set the Twitch stream title and/or category before going live.

        Multistream plugins carry the video, not the metadata -- without this the
        Twitch channel keeps the PREVIOUS stream's title. game_name is resolved
        to a Twitch category id ("Hollow Knight: Silksong" -> id).
        """
        if not title and not game_name and tags is None:
            return {"error": "nothing to set: pass title, game_name and/or tags"}
        if dry_run:
            return preview("twitch_set_channel",
                           {"title": title, "game_name": game_name, "tags": tags})
        return set_channel(title or None, game_name or None, tags)
