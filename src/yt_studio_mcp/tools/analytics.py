"""YouTube Analytics tools."""

from __future__ import annotations

from ..client import get_yt


def _rows_to_dicts(res: dict) -> list[dict]:
    headers = [h["name"] for h in res.get("columnHeaders", [])]
    return [dict(zip(headers, row, strict=False)) for row in res.get("rows", [])]


def register(mcp) -> None:
    @mcp.tool()
    def channel_report(
        start: str,
        end: str,
        metrics: str = "views,estimatedMinutesWatched,subscribersGained,subscribersLost",
        dimensions: str = "day",
    ) -> list[dict]:
        """Channel analytics between start/end (YYYY-MM-DD)."""
        yt = get_yt()
        res = yt.call(
            yt.analytics.reports().query(
                ids="channel==MINE",
                startDate=start,
                endDate=end,
                metrics=metrics,
                dimensions=dimensions,
                sort=dimensions.split(",")[0],
            ),
            op="analytics.query",
        )
        return _rows_to_dicts(res)

    @mcp.tool()
    def video_report(
        video_id: str,
        start: str,
        end: str,
        metrics: str = "views,estimatedMinutesWatched,averageViewDuration,likes,comments",
    ) -> list[dict]:
        """Per-video analytics between start/end (YYYY-MM-DD)."""
        yt = get_yt()
        res = yt.call(
            yt.analytics.reports().query(
                ids="channel==MINE",
                startDate=start,
                endDate=end,
                metrics=metrics,
                dimensions="day",
                filters=f"video=={video_id}",
                sort="day",
            ),
            op="analytics.query",
        )
        return _rows_to_dicts(res)

    @mcp.tool()
    def top_videos(start: str, end: str, limit: int = 10) -> list[dict]:
        """Top videos by views between start/end (YYYY-MM-DD)."""
        yt = get_yt()
        res = yt.call(
            yt.analytics.reports().query(
                ids="channel==MINE",
                startDate=start,
                endDate=end,
                metrics="views,estimatedMinutesWatched",
                dimensions="video",
                sort="-views",
                maxResults=limit,
            ),
            op="analytics.query",
        )
        return _rows_to_dicts(res)
