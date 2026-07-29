"""AI thumbnail generation via any OpenAI-compatible images endpoint.

Env:
  YT_MCP_IMAGE_API_KEY   bearer key (required to enable the tool)
  YT_MCP_IMAGE_API_URL   base URL, default https://api.openai.com/v1
  YT_MCP_IMAGE_MODEL     default gpt-image-1

Output is center-cropped and resized to YouTube's 1280x720. Every request
and failure is logged.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..client import preview

logger = logging.getLogger("yt_studio_mcp.thumbnails")

OUT_DIR = Path.home() / ".config" / "yt-studio-mcp" / "thumbnails"


def finish_to_720(png_bytes: bytes, out_path: Path) -> None:
    """Center-crop to 16:9 and resize to 1280x720."""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = img.size
    target = 16 / 9
    if w / h > target:
        new_w = int(h * target)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    img = img.resize((1280, 720), Image.LANCZOS)
    img.save(out_path, "JPEG", quality=90)


def generate_image(prompt: str, quality: str) -> bytes:
    api_url = os.environ.get("YT_MCP_IMAGE_API_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("YT_MCP_IMAGE_MODEL", "gpt-image-1")
    key = os.environ["YT_MCP_IMAGE_API_KEY"]
    body = {
        "model": model,
        "prompt": prompt,
        "size": "1536x1024",
        "quality": quality,
        "n": 1,
    }
    logger.info("image gen model=%s quality=%s prompt=%r", model, quality, prompt[:200])
    req = Request(
        f"{api_url}/images/generations",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
    )
    try:
        with urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read())
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        logger.error("image gen failed %s: %s", exc.code, detail)
        raise RuntimeError(f"image API {exc.code}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        logger.error("image gen unreachable: %s", exc)
        raise RuntimeError(f"image API unreachable: {exc}") from exc
    data = payload["data"][0]
    if "b64_json" in data:
        return base64.b64decode(data["b64_json"])
    with urlopen(data["url"], timeout=60) as img_resp:
        return img_resp.read()


def _resize_ref(path: str, max_px: int = 1024) -> bytes:
    """Downscale a reference image to keep the multipart request light."""
    import io

    from PIL import Image

    img = Image.open(path).convert("RGB")
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def generate_image_with_refs(prompt: str, quality: str, references: list[str]) -> bytes:
    """Generate via the images/edits endpoint using reference images to keep
    characters on-model (e.g. the Cipher/Echo character sheets)."""
    import secrets as _sec

    api_url = os.environ.get("YT_MCP_IMAGE_API_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("YT_MCP_IMAGE_MODEL", "gpt-image-1")
    key = os.environ["YT_MCP_IMAGE_API_KEY"]
    boundary = "----ytmcp" + _sec.token_hex(8)
    parts = []
    for field, val in (("model", model), ("prompt", prompt), ("size", "1536x1024"),
                       ("quality", quality), ("n", "1")):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"\r\n\r\n{val}\r\n'
        )
    head = "".join(parts).encode()
    body = bytearray(head)
    for i, ref in enumerate(references):
        img_bytes = _resize_ref(ref)
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image[]"; filename="ref{i}.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode()
        body += img_bytes + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    logger.info(
        "image edit model=%s q=%s refs=%d prompt=%r",
        model, quality, len(references), prompt[:150],
    )
    req = Request(f"{api_url}/images/edits", data=bytes(body),
                  headers={"content-type": f"multipart/form-data; boundary={boundary}",
                           "authorization": f"Bearer {key}"})
    try:
        with urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read())
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        logger.error("image edit failed %s: %s", exc.code, detail)
        raise RuntimeError(f"image edit API {exc.code}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"image edit API unreachable: {exc}") from exc
    d = payload["data"][0]
    if "b64_json" in d:
        return base64.b64decode(d["b64_json"])
    with urlopen(d["url"], timeout=60) as r:
        return r.read()


def register(mcp) -> None:
    @mcp.tool()
    def generate_thumbnail(
        prompt: str,
        video_id: str | None = None,
        quality: str = "medium",
        reference_images: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Generate a 1280x720 thumbnail from a prompt via the configured
        image API (YT_MCP_IMAGE_API_KEY required; quality: low|medium|high).
        reference_images: local image paths fed to the model to keep characters
        on-model (e.g. the Cipher/Echo character sheets) — uses the edits
        endpoint. If video_id is given, the thumbnail is also uploaded."""
        if not os.environ.get("YT_MCP_IMAGE_API_KEY"):
            return {"error": "set YT_MCP_IMAGE_API_KEY to enable thumbnail generation"}
        if quality not in ("low", "medium", "high"):
            return {"error": "quality must be low|medium|high"}
        if dry_run:
            return preview(
                "generate_thumbnail",
                {"prompt": prompt, "quality": quality, "video_id": video_id,
                 "references": reference_images or []},
            )
        if reference_images:
            png = generate_image_with_refs(prompt, quality, reference_images)
        else:
            png = generate_image(prompt, quality)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_path = OUT_DIR / f"thumb-{stamp}.jpg"
        finish_to_720(png, out_path)
        result: dict = {"thumbnail_path": str(out_path), "size": "1280x720"}
        if video_id:
            from googleapiclient.http import MediaFileUpload

            from ..client import get_yt

            yt = get_yt()
            yt.call(
                yt.data.thumbnails().set(
                    videoId=video_id, media_body=MediaFileUpload(str(out_path))
                ),
                op="thumbnails.set",
            )
            result["uploaded_to"] = video_id
        return result
