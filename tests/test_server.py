import asyncio

from yt_studio_mcp.server import build_app

EXPECTED = {
    # videos
    "channel_info", "list_subscribers", "list_videos", "get_video", "update_video", "set_thumbnail",
    "upload_video", "delete_video",
    # playlists
    "list_playlists", "create_playlist", "delete_playlist", "add_to_playlist",
    "remove_from_playlist", "list_playlist_items",
    # comments
    "list_comments", "post_video_question", "reply_to_comment", "moderate_comment",
    "mark_spam", "delete_comment",
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
    "batch_update_descriptions", "made_for_kids_audit", "post_stream_digest",
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
