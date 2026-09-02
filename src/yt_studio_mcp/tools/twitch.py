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
    def twitch_authorize_url() -> dict:
        """Step 1 of connecting Twitch: the URL to approve as the channel owner.

        Open it anywhere, approve, then pass the ?code=... from the redirect to
        twitch_finish_auth. The redirect (http://localhost:8271) will fail to
        load -- that is expected and harmless; only the code matters.
        """
        import os
        import secrets as pysecrets

        from ..twitch_auth import authorize_url

        cid = os.environ.get("TWITCH_CLIENT_ID")
        if not cid:
            return {"error": "TWITCH_CLIENT_ID is not set in the server environment"}
        return {
            "url": authorize_url(cid, pysecrets.token_urlsafe(16)),
            "next": "approve as the channel owner, then call twitch_finish_auth(code=...)",
            "note": "authorization codes expire within minutes",
        }

    @mcp.tool()
    def twitch_finish_auth(code: str) -> dict:
        """Step 2: exchange the pasted ?code=... for a stored refresh token.

        Runs inside the server so the client secret never leaves its process --
        it is injected from the vault at launch and is never typed or logged.
        """
        import os

        from ..twitch_auth import exchange_code

        cid = os.environ.get("TWITCH_CLIENT_ID")
        sec = os.environ.get("TWITCH_CLIENT_SECRET")
        if not cid or not sec:
            return {"error": "TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET missing from the "
                             "server environment; check the Connecterr vault injection"}
        try:
            msg = exchange_code(cid, sec, code.strip())
        except Exception as exc:  # noqa: BLE001 - the reason is the whole point
            return {"ok": False, "error": str(exc),
                    "hint": "codes expire in minutes and are single-use; get a fresh "
                            "one from twitch_authorize_url"}
        return {"ok": True, "detail": msg}

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
