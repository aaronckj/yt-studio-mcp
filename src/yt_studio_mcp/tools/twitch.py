"""Twitch channel tools -- the other half of going live when multistreaming -- and raids."""

from __future__ import annotations

from mcp.types import ToolAnnotations

from ..client import preview
from ..twitch import (
    Twitch,
    TwitchError,
    TwitchNotConfigured,
    TwitchRaidError,
    raid_cancel,
    raid_candidates,
    raid_start,
    set_channel,
    valid_login,
)


def register(mcp) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def twitch_status() -> dict:
        """Whether Twitch is configured, which channel it would write to, and its scopes.

        scopes.missing lists what the stored grant lacks (e.g. channel:manage:raids);
        scopes.hint says how to re-approve. Never returns the stream key or email.
        """
        try:
            tw = Twitch()
        except TwitchNotConfigured as exc:
            return {"configured": False, "missing": exc.missing, "hint": str(exc)}
        me = tw.me()
        ch = tw.get_channel()
        try:
            scopes = tw.scope_report()
        except TwitchError as exc:
            scopes = {"error": str(exc)[:200]}
        return {
            "configured": True,
            "login": me.get("login"),
            "broadcaster_id": me.get("id"),
            "title": ch.get("title"),
            "category": ch.get("game_name"),
            "scopes": scopes,
            "raids_enabled": bool(scopes.get("raids_enabled")),
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def twitch_set_channel(
        title: str = "",
        game_name: str = "",
        tags: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Set the Twitch stream title and/or category before going live.

        Multistream plugins carry the video, not the metadata -- without this the
        Twitch channel keeps the PREVIOUS stream's title. game_name is resolved
        to a Twitch category id ("Example Game" -> id).
        """
        if not title and not game_name and tags is None:
            return {"error": "nothing to set: pass title, game_name and/or tags"}
        if dry_run:
            return preview("twitch_set_channel",
                           {"title": title, "game_name": game_name, "tags": tags})
        return set_channel(title or None, game_name or None, tags)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def twitch_raid_candidates(logins: list[str]) -> dict:
        """Which of these Twitch channels are live right now: game, viewer count, title.

        Up to 100 logins per Helix call (batched). Invalid logins come back in
        `invalid` and are never sent to Twitch.
        """
        return raid_candidates(logins)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def twitch_raid(to_login: str, dry_run: bool = False) -> dict:
        """Raid another channel with our live Twitch audience.

        Twitch shows a ~90 s countdown, then sends the raid; ending our stream
        before that cancels it. Needs the channel:manage:raids scope (see
        twitch_status). Limit: 10 raid requests per 10 minutes.
        """
        login = valid_login(to_login)
        if not login:
            return TwitchRaidError("invalid_login", f"not a Twitch login: {to_login!r}",
                                   "Logins are 3-25 letters, digits or underscores.").as_dict()
        if dry_run:
            return preview("twitch_raid", {"to_login": login,
                                           "note": "Twitch sends the raid ~90 s after this"})
        try:
            return raid_start(login)
        except TwitchRaidError as exc:
            return exc.as_dict()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def twitch_raid_cancel(dry_run: bool = False) -> dict:
        """Cancel our pending raid (only during Twitch's countdown)."""
        if dry_run:
            return preview("twitch_raid_cancel", {})
        try:
            return raid_cancel()
        except TwitchRaidError as exc:
            return exc.as_dict()
