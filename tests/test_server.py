import asyncio

from yt_studio_mcp.server import build_app

EXPECTED = {
    # videos
    "channel_info", "list_videos", "get_video", "update_video", "set_thumbnail",
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
    "check_verification_reply", "post_winner_reply",
    # meta
    "quota_status", "health_check",
}


def test_all_tools_registered():
    app = build_app()
    tools = asyncio.run(app.list_tools())
    names = {t.name for t in tools}
    assert names == EXPECTED
