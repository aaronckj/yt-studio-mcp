from yt_studio_mcp.tools.questions import build_question_comment, studio_comments_url


def test_comment_contains_question_and_rules():
    text = build_question_comment("What's your favorite boss?", "https://example.com/rules")
    assert "What's your favorite boss?" in text
    assert "https://example.com/rules" in text
    assert "No purchase necessary" in text


def test_studio_url():
    assert studio_comments_url("abc123") == "https://studio.youtube.com/video/abc123/comments"
