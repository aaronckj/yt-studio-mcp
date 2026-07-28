from yt_studio_mcp.tools.banned_words import match_comment, normalize


def test_normalize_case_and_separators():
    assert normalize("S-p.a_m w o r d") == "spamword"
    assert normalize("SCAMSITE!!") == "scamsite"


def test_match_comment_word_boundaries():
    words = ["spamword", "scamsite", "acme"]
    hit = match_comment("Get your $500 ScamSite gift card now", words)
    assert hit == ["scamsite"]
    assert match_comment("I love this game!", words) == []
    # obfuscated
    assert match_comment("s p a m w o r d rewards for you", words) == ["spamword"]
    # substring of a legit word must NOT match ("acme" inside "xacmey")
    assert match_comment("xacmey", words) == []


def test_match_multiple():
    words = ["spamword", "scamsite", "acme"]
    hits = match_comment("ACME and ScamSite codes here", words)
    assert set(hits) == {"acme", "scamsite"}
