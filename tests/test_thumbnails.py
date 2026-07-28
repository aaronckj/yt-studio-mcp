import io

from PIL import Image

from yt_studio_mcp.tools.thumbnails import finish_to_720


def make_png(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 60, 200)).save(buf, "PNG")
    return buf.getvalue()


def test_finish_crops_wide_to_720(tmp_path):
    out = tmp_path / "t.jpg"
    finish_to_720(make_png(1536, 1024), out)
    img = Image.open(out)
    assert img.size == (1280, 720)


def test_finish_crops_tall_to_720(tmp_path):
    out = tmp_path / "t.jpg"
    finish_to_720(make_png(1024, 1536), out)
    assert Image.open(out).size == (1280, 720)
