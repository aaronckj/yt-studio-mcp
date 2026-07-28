from yt_studio_mcp.tools.bulk import FOOTER_MARKER, apply_description_change


def test_append_skips_when_present():
    assert apply_description_change("hello\n\nlink", "append", "link") is None
    assert apply_description_change("hello", "append", "link") == "hello\n\nlink"


def test_footer_adds_marker_when_missing():
    out = apply_description_change("body text", "footer", "footer v1")
    assert out == "body text" + FOOTER_MARKER + "footer v1"


def test_footer_replaces_existing_footer_only():
    current = "body text" + FOOTER_MARKER + "old footer"
    out = apply_description_change(current, "footer", "new footer")
    assert out == "body text" + FOOTER_MARKER + "new footer"
    # unchanged when footer already matches
    assert apply_description_change(out, "footer", "new footer") is None


def test_replace_mode():
    assert (
        apply_description_change("visit old.example", "replace", "new.example", find="old.example")
        == "visit new.example"
    )
    assert apply_description_change("nothing here", "replace", "x", find="absent") is None
