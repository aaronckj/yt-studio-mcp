import asyncio

from yt_studio_mcp.server import build_app

EXPECTED = {
    # videos
    "channel_info", "list_subscribers", "list_videos", "get_video", "update_video", "set_thumbnail",
    "update_channel",
    "upload_video", "delete_video",
    # playlists
    "list_playlists", "create_playlist", "delete_playlist", "add_to_playlist",
    "remove_from_playlist", "list_playlist_items", "update_playlist",
    # comments
    "list_comments", "post_video_question", "reply_to_comment", "moderate_comment",
    "mark_spam", "delete_comment", "list_held_comments",
    # live
    "list_broadcasts", "create_broadcast", "list_streams", "bind_broadcast",
    "live_chat_messages", "post_chat_message", "delete_chat_message", "ban_chat_user",
    # captions
    "list_captions", "upload_caption", "download_caption",
    # analytics
    "channel_report", "video_report", "top_videos",
    # giveaway
    "collect_entries", "draw_winners", "make_verification_code",
    "check_verification_reply", "post_winner_reply", "export_entries_csv",
    "winner_announcement",
    # banned words
    "banned_words_list", "banned_words_add", "banned_words_remove", "scan_comments",
    # questions
    "post_giveaway_question", "giveaway_question_status",
    # bulk
    "batch_update_descriptions", "batch_update_tags", "made_for_kids_audit", "post_stream_digest",
    # thumbnails
    "generate_thumbnail",
    # meta
    "quota_status", "health_check",
}


def test_all_tools_registered():
    app = build_app()
    tools = asyncio.run(app.list_tools())
    names = {t.name for t in tools}
    assert names == EXPECTED


def test_update_video_accepts_publish_at():
    """Scheduling used to be write-once: publish_at existed only on upload, so
    moving a scheduled video meant delete + re-upload or a manual Studio edit."""
    import inspect

    from yt_studio_mcp.tools import videos

    src = inspect.getsource(videos)
    assert "publish_at: str | None = None" in src
    assert 'status["publishAt"] = publish_at' in src


def test_scheduled_time_is_readable():
    """publishedAt is the UPLOAD time; without publishAt a schedule cannot be
    verified after the fact."""
    import inspect

    from yt_studio_mcp.tools import videos

    src = inspect.getsource(videos)
    assert 'out["scheduled"] = scheduled' in src


def test_publish_at_forces_private():
    """YouTube drops publishAt unless privacyStatus is private."""
    import inspect

    from yt_studio_mcp.tools import videos

    src = inspect.getsource(videos)
    assert 'status["privacyStatus"] = "private"' in src


def test_new_write_tools_exist():
    """Four things that were being done by hand in Studio, or not at all:
    renaming a playlist, editing the channel description, reviewing held
    comments, and keeping tags consistent across a growing catalogue."""
    import inspect

    from yt_studio_mcp.tools import bulk, comments, playlists, videos

    assert "def update_playlist(" in inspect.getsource(playlists)
    assert "def update_channel(" in inspect.getsource(videos)
    assert "def list_held_comments(" in inspect.getsource(comments)
    assert "def batch_update_tags(" in inspect.getsource(bulk)


def test_held_comments_filters_by_moderation_status():
    """Held comments are invisible to the public until approved; without the
    moderationStatus filter they cannot be found at all."""
    import inspect

    from yt_studio_mcp.tools import comments

    assert "moderationStatus=status" in inspect.getsource(comments)


def test_batch_tags_preserves_existing():
    """Only the named tags change — a blind overwrite would wipe per-video tags."""
    import inspect

    from yt_studio_mcp.tools import bulk

    src = inspect.getsource(bulk)
    assert "new = [t for t in cur if t.lower() not in rm_l]" in src
