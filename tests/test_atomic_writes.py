"""banned_words._atomic_write and giveaway._write_atomic use splendid-safe safe_io.atomic_write."""
import os
import stat

from yt_studio_mcp.tools import banned_words, giveaway


def test_atomic_writers_mode_content_and_no_temp(tmp_path):
    a = tmp_path / "sub" / "words.json"
    banned_words._atomic_write(a, '["x"]')              # creates the parent like before
    b = tmp_path / "audit.json"
    giveaway._write_atomic(b, "{}")
    for p, want in ((a, '["x"]'), (b, "{}")):
        assert p.read_text() == want and stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]


def test_atomic_writer_replaces_a_link_instead_of_writing_through_it(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    link = tmp_path / "words.json"
    link.symlink_to(victim)
    banned_words._atomic_write(link, "[]")
    assert victim.read_text() == "keep" and not link.is_symlink() and link.read_text() == "[]"
