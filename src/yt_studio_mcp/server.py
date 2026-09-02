"""FastMCP app assembly."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .tools import (
    analytics,
    banned_words,
    bulk,
    captions,
    comments,
    giveaway,
    live,
    meta,
    playlists,
    questions,
    thumbnails,
    twitch,
    videos,
)


def build_app() -> FastMCP:
    mcp = FastMCP(
        "yt-studio",
        instructions=(
            "Manage the authorized YouTube channel: videos, comments and "
            "moderation, playlists, live broadcasts, captions, analytics, and "
            "auditable comment-entry giveaways, and the Twitch channel title/"
            "category that multistream plugins do NOT carry over. Every mutating "
            "tool accepts dry_run=True to preview without writing."
        ),
    )
    for module in (
        videos, playlists, comments, live, captions, analytics, giveaway,
        banned_words, bulk, questions, thumbnails, meta, twitch,
    ):
        module.register(mcp)
    return mcp
