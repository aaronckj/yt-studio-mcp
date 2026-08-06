"""Bulk maintenance tools: batch descriptions, made-for-kids audit, digest."""

from __future__ import annotations

from ..client import get_yt, preview

FOOTER_MARKER = "\n\n---\n"


def apply_description_change(
    current: str, mode: str, text: str, find: str | None = None
) -> str | None:
    """Pure description-transform logic. Returns new description or None if unchanged."""
    if mode == "append":
        if text in current:
            return None
        return (current + "\n\n" + text).strip()
    if mode == "footer":
        idx = current.rfind(FOOTER_MARKER)
        body = current[:idx] if idx != -1 else current
        new = body.rstrip() + FOOTER_MARKER + text
        return None if new == current else new
    if mode == "replace":
        if not find or find not in current:
            return None
        return current.replace(find, text)
    raise ValueError("mode must be append|footer|replace")


def register(mcp) -> None:
    @mcp.tool()
    def batch_update_descriptions(
        mode: str,
        text: str,
        find: str | None = None,
        video_ids: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Update descriptions across many videos in one sweep.

        mode=append: add text at the end (skips videos that already contain it).
        mode=footer: everything after the last "---" marker line is replaced
          with text (marker added if missing) — ideal for a links/giveaway
          footer kept in sync across all videos.
        mode=replace: replace occurrences of `find` with text.
        Scope: video_ids, or every upload when omitted. Quota: ~51 units per
        changed video."""
        yt = get_yt()
        if video_ids is None:
            me = yt.call(
                yt.data.channels().list(part="contentDetails", mine=True), op="list"
            )["items"][0]
            uploads = me["contentDetails"]["relatedPlaylists"]["uploads"]
            items = yt.paginate(
                yt.data.playlistItems(), "list", limit=500, part="snippet", playlistId=uploads
            )
            video_ids = [i["snippet"]["resourceId"]["videoId"] for i in items]

        changed, skipped = [], []
        for vid in video_ids:
            v = yt.call(
                yt.data.videos().list(part="snippet", id=vid), op="list"
            )["items"][0]
            snippet = v["snippet"]
            new_desc = apply_description_change(
                snippet.get("description", ""), mode, text, find
            )
            if new_desc is None:
                skipped.append(vid)
                continue
            if dry_run:
                changed.append({"video_id": vid, "title": snippet["title"]})
                continue
            snippet["description"] = new_desc
            yt.call(
                yt.data.videos().update(
                    part="snippet", body={"id": vid, "snippet": snippet}
                ),
                op="update",
            )
            changed.append({"video_id": vid, "title": snippet["title"]})
        result = {"mode": mode, "changed": changed, "skipped": len(skipped)}
        if dry_run:
            return preview("batch_update_descriptions", result)
        return result

    @mcp.tool()
    def batch_update_tags(
        add: list[str] | None = None,
        remove: list[str] | None = None,
        video_ids: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Add and/or remove tags across many videos at once.

        Tags drift as a catalogue grows — a series tag added halfway through is
        missing from everything before it. Existing tags are preserved; only
        the named ones change. Scope: video_ids, or every upload when omitted.
        Quota: ~51 units per changed video.
        """
        if not add and not remove:
            return {"error": "supply add and/or remove"}
        yt = get_yt()
        if video_ids is None:
            me = yt.call(
                yt.data.channels().list(part="contentDetails", mine=True), op="list"
            )["items"][0]
            uploads = me["contentDetails"]["relatedPlaylists"]["uploads"]
            items = yt.paginate(
                yt.data.playlistItems(), "list", limit=500, part="snippet",
                playlistId=uploads,
            )
            video_ids = [i["snippet"]["resourceId"]["videoId"] for i in items]
        ids = video_ids
        add_l = [t for t in (add or [])]
        rm_l = {t.lower() for t in (remove or [])}
        changed, skipped = [], 0
        for vid in ids:
            items = yt.call(
                yt.data.videos().list(part="snippet", id=vid), op="list"
            ).get("items", [])
            if not items:
                continue
            sn = items[0]["snippet"]
            cur = sn.get("tags", [])
            new = [t for t in cur if t.lower() not in rm_l]
            for t in add_l:
                if t.lower() not in {x.lower() for x in new}:
                    new.append(t)
            if new == cur:
                skipped += 1
                continue
            if not dry_run:
                sn["tags"] = new
                yt.call(
                    yt.data.videos().update(
                        part="snippet", body={"id": vid, "snippet": sn}
                    ),
                    op="update",
                )
            changed.append({"video_id": vid, "title": sn.get("title"), "tags": new})
        out = {"changed": changed, "skipped": skipped}
        return preview("batch_update_tags", out) if dry_run else out

    @mcp.tool()
    def made_for_kids_audit() -> dict:
        """Audit every upload's made-for-kids designation (COPPA). Reports both
        the platform-effective flag and the self-declared one per video."""
        yt = get_yt()
        me = yt.call(
            yt.data.channels().list(part="contentDetails", mine=True), op="list"
        )["items"][0]
        uploads = me["contentDetails"]["relatedPlaylists"]["uploads"]
        items = yt.paginate(
            yt.data.playlistItems(), "list", limit=500, part="snippet", playlistId=uploads
        )
        ids = [i["snippet"]["resourceId"]["videoId"] for i in items]
        videos = []
        for chunk_start in range(0, len(ids), 50):
            chunk = ids[chunk_start : chunk_start + 50]
            res = yt.call(
                yt.data.videos().list(part="snippet,status", id=",".join(chunk)),
                op="list",
            )
            for v in res.get("items", []):
                videos.append(
                    {
                        "video_id": v["id"],
                        "title": v["snippet"]["title"],
                        "made_for_kids": v["status"].get("madeForKids"),
                        "self_declared": v["status"].get("selfDeclaredMadeForKids"),
                        "privacy": v["status"].get("privacyStatus"),
                    }
                )
        flags = {str(v["made_for_kids"]) for v in videos}
        return {
            "videos": videos,
            "consistent": len(flags) <= 1,
            "note": "madeForKids=True disables comments on those videos",
        }

    @mcp.tool()
    def post_stream_digest(start: str, end: str) -> dict:
        """Channel performance digest for a date window (YYYY-MM-DD): totals,
        per-day trend, and top videos — run it the morning after a stream."""
        yt = get_yt()

        def q(**kw):
            return yt.call(
                yt.analytics.reports().query(ids="channel==MINE", **kw),
                op="analytics.query",
            )

        daily = q(
            startDate=start,
            endDate=end,
            metrics="views,estimatedMinutesWatched,subscribersGained,subscribersLost",
            dimensions="day",
            sort="day",
        )
        top = q(
            startDate=start,
            endDate=end,
            metrics="views,estimatedMinutesWatched",
            dimensions="video",
            sort="-views",
            maxResults=5,
        )
        headers = [h["name"] for h in daily.get("columnHeaders", [])]
        rows = [dict(zip(headers, r, strict=False)) for r in daily.get("rows", [])]
        totals = {
            "views": sum(r.get("views", 0) for r in rows),
            "minutes_watched": sum(r.get("estimatedMinutesWatched", 0) for r in rows),
            "subs_gained": sum(r.get("subscribersGained", 0) for r in rows),
            "subs_lost": sum(r.get("subscribersLost", 0) for r in rows),
        }
        top_headers = [h["name"] for h in top.get("columnHeaders", [])]
        return {
            "window": {"start": start, "end": end},
            "totals": totals,
            "daily": rows,
            "top_videos": [
                dict(zip(top_headers, r, strict=False)) for r in top.get("rows", [])
            ],
        }
