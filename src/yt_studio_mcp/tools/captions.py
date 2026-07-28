"""Caption tools."""

from __future__ import annotations

from ..client import get_yt, preview


def register(mcp) -> None:
    @mcp.tool()
    def list_captions(video_id: str) -> list[dict]:
        """List caption tracks on a video."""
        yt = get_yt()
        res = yt.call(
            yt.data.captions().list(part="snippet", videoId=video_id), op="list"
        )
        return [
            {
                "id": c["id"],
                "language": c["snippet"]["language"],
                "name": c["snippet"].get("name", ""),
                "kind": c["snippet"].get("trackKind"),
                "auto": c["snippet"].get("trackKind") == "asr",
            }
            for c in res.get("items", [])
        ]

    @mcp.tool()
    def upload_caption(
        video_id: str, language: str, file_path: str, name: str = "", dry_run: bool = False
    ) -> dict:
        """Upload a caption track (SRT/VTT file)."""
        if dry_run:
            return preview(
                "upload_caption",
                {"video_id": video_id, "language": language, "file_path": file_path},
            )
        from googleapiclient.http import MediaFileUpload

        yt = get_yt()
        res = yt.call(
            yt.data.captions().insert(
                part="snippet",
                body={
                    "snippet": {"videoId": video_id, "language": language, "name": name}
                },
                media_body=MediaFileUpload(file_path),
            ),
            op="captions.insert",
        )
        return {"uploaded": res["id"], "video_id": video_id, "language": language}

    @mcp.tool()
    def download_caption(caption_id: str, fmt: str = "srt") -> dict:
        """Download a caption track's text. fmt: srt|vtt."""
        yt = get_yt()
        data = yt.call(yt.data.captions().download(id=caption_id, tfmt=fmt), op="captions.download")
        text = data.decode() if isinstance(data, bytes) else str(data)
        return {"caption_id": caption_id, "format": fmt, "text": text}
