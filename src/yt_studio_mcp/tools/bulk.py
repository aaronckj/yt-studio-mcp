"""Bulk maintenance tools: batch descriptions, made-for-kids audit, digest."""

from __future__ import annotations

from mcp.types import ToolAnnotations

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


MODES = ("append", "footer", "replace")


def _upload_ids(yt) -> list[str]:
    me = yt.call(
        yt.data.channels().list(part="contentDetails", mine=True), op="list"
    )["items"][0]
    uploads = me["contentDetails"]["relatedPlaylists"]["uploads"]
    items = yt.paginate(
        yt.data.playlistItems(), "list", limit=5000, part="snippet", playlistId=uploads
    )
    return [i["snippet"]["resourceId"]["videoId"] for i in items]


def _snippets(yt, ids: list[str]) -> dict[str, dict]:
    """Fetch snippets 50 ids per call (1 unit) instead of one call per video."""
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i : i + 50]
        res = yt.call(
            yt.data.videos().list(part="snippet", id=",".join(chunk)), op="list"
        )
        for v in res.get("items", []):
            out[v["id"]] = v["snippet"]
    return out


def _batch_update(yt, plan: list[tuple[str, dict]], dry_run: bool) -> dict:
    """Apply snippet updates one by one; never lose track of what happened.

    A failure (deleted video, quota exhausted) used to raise out of the loop
    and the caller could not tell which videos had already been changed.
    """
    changed, failed = [], []
    for idx, (vid, snippet) in enumerate(plan):
        if dry_run:
            changed.append({"video_id": vid, "title": snippet.get("title")})
            continue
        try:
            yt.call(
                yt.data.videos().update(part="snippet", body={"id": vid, "snippet": snippet}),
                op="update",
            )
            changed.append({"video_id": vid, "title": snippet.get("title")})
        except Exception as exc:  # noqa: BLE001 - reported per video
            failed.append({"video_id": vid, "error": str(exc)})
            if getattr(exc, "reason", "") == "quotaExceeded":
                failed.extend({"video_id": v, "error": "not attempted: quota exhausted"}
                              for v, _ in plan[idx + 1:])
                break
    return {"changed": changed, "failed": failed}


def register(mcp) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
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
        Scope: video_ids, or every upload when omitted. Quota: ~50 units per
        changed video plus 1 per 50 videos read. Partial failures are listed
        in `failed`; nothing after a quota exhaustion is attempted."""
        if mode not in MODES:
            return {"error": f"mode must be one of {'|'.join(MODES)}"}
        if mode in ("append", "footer") and not text.strip():
            return {"error": f"refusing an empty {mode}: it would blank the "
                    "footer/append nothing across every video"}
        if mode == "replace" and not find:
            return {"error": "mode=replace needs find"}
        yt = get_yt()
        ids = video_ids if video_ids is not None else _upload_ids(yt)
        snippets = _snippets(yt, ids)
        plan, skipped = [], 0
        missing = [v for v in ids if v not in snippets]
        for vid in ids:
            snippet = snippets.get(vid)
            if snippet is None:
                continue
            new_desc = apply_description_change(
                snippet.get("description", ""), mode, text, find
            )
            if new_desc is None:
                skipped += 1
                continue
            snippet["description"] = new_desc
            plan.append((vid, snippet))
        result = {"mode": mode, **_batch_update(yt, plan, dry_run),
                  "skipped": skipped, "not_found": missing}
        if dry_run:
            return preview("batch_update_descriptions", result)
        return result

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
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
        Quota: ~50 units per changed video plus 1 per 50 videos read.
        """
        if not add and not remove:
            return {"error": "supply add and/or remove"}
        yt = get_yt()
        ids = video_ids if video_ids is not None else _upload_ids(yt)
        snippets = _snippets(yt, ids)
        add_l = [t for t in (add or []) if t.strip()]
        rm_l = {t.lower() for t in (remove or [])}
        plan, skipped = [], 0
        tags_by_vid = {}
        for vid in ids:
            sn = snippets.get(vid)
            if sn is None:
                continue
            cur = sn.get("tags", [])
            new = [t for t in cur if t.lower() not in rm_l]
            for t in add_l:
                if t.lower() not in {x.lower() for x in new}:
                    new.append(t)
            if new == cur:
                skipped += 1
                continue
            sn["tags"] = new
            tags_by_vid[vid] = new
            plan.append((vid, sn))
        res = _batch_update(yt, plan, dry_run)
        for c in res["changed"]:
            c["tags"] = tags_by_vid[c["video_id"]]
        out = {**res, "skipped": skipped,
               "not_found": [v for v in ids if v not in snippets]}
        return preview("batch_update_tags", out) if dry_run else out

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
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
