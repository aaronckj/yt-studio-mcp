"""Banned-word comment scanning.

YouTube's Studio blocked-words list has no public API, so this module keeps
its own wordlist and sweeps comments against it, with optional automatic
moderation (hold or reject) of matches. Matching is obfuscation-tolerant:
case-insensitive and separator-insensitive (``s.P-a m`` matches ``spam``)
while still requiring word boundaries so substrings of longer words don't
false-positive.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..client import get_yt, preview

WORDLIST_PATH = Path.home() / ".config" / "yt-studio-mcp" / "banned_words.json"
SCAN_STATE_PATH = Path.home() / ".config" / "yt-studio-mcp" / "scan_state.json"
_SEP = re.compile(r"[\s\.\-_,!\$\*\|]+")


def normalize(text: str) -> str:
    return _SEP.sub("", text.lower())


def _load_words() -> list[str]:
    if WORDLIST_PATH.exists():
        return json.loads(WORDLIST_PATH.read_text())
    return []


def _save_words(words: list[str]) -> None:
    WORDLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    WORDLIST_PATH.write_text(json.dumps(sorted(set(words)), indent=2))


def match_comment(text: str, words: list[str]) -> list[str]:
    """Return the banned words present in text.

    A word hits when its separator-squashed form appears in the squashed text
    AND the characters adjacent to the matched span in the ORIGINAL text are
    non-alphanumeric (or the string edge). This catches obfuscations like
    ``s.P-a m`` while rejecting substrings of longer words (``acme`` inside
    ``xacmey``).
    """
    lower = text.lower()
    # squashed text plus a map from squashed index -> original index
    squashed_chars: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(lower):
        if not _SEP.match(ch):
            squashed_chars.append(ch)
            origin.append(i)
    squashed = "".join(squashed_chars)

    hits = []
    for w in words:
        target = normalize(w)
        if not target:
            continue
        start = 0
        while (idx := squashed.find(target, start)) != -1:
            raw_start = origin[idx]
            raw_end = origin[idx + len(target) - 1]
            before_ok = raw_start == 0 or not lower[raw_start - 1].isalnum()
            after_ok = raw_end == len(lower) - 1 or not lower[raw_end + 1].isalnum()
            if before_ok and after_ok:
                hits.append(w)
                break
            start = idx + 1
    return hits


def register(mcp) -> None:
    @mcp.tool()
    def banned_words_list() -> dict:
        """Show the managed banned-word list used by scan_comments."""
        return {"words": _load_words(), "path": str(WORDLIST_PATH)}

    @mcp.tool()
    def banned_words_add(words: list[str]) -> dict:
        """Add words/phrases to the banned list."""
        current = _load_words()
        _save_words(current + words)
        return {"words": _load_words()}

    @mcp.tool()
    def banned_words_remove(words: list[str]) -> dict:
        """Remove words from the banned list."""
        drop = {w.lower() for w in words}
        _save_words([w for w in _load_words() if w.lower() not in drop])
        return {"words": _load_words()}

    @mcp.tool()
    def scan_comments(
        video_id: str | None = None,
        limit: int = 500,
        extra_words: list[str] | None = None,
        auto_action: str = "none",
        incremental: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Sweep comments (one video, or channel-wide when video_id omitted) for
        banned words. auto_action: none | heldForReview | rejected — matches get
        that moderation status applied (dry_run previews instead).
        incremental=True only examines comments newer than the previous
        incremental scan (watermark stored locally) — cheap to run daily."""
        if auto_action not in ("none", "heldForReview", "rejected"):
            return {"error": "auto_action must be none|heldForReview|rejected"}
        watermark = None
        if incremental and SCAN_STATE_PATH.exists():
            watermark = json.loads(SCAN_STATE_PATH.read_text()).get("last_scan")
        words = _load_words() + (extra_words or [])
        if not words:
            return {"error": "banned word list is empty; add words with banned_words_add"}
        yt = get_yt()
        params: dict = {"part": "snippet", "order": "time", "textFormat": "plainText"}
        me = yt.call(yt.data.channels().list(part="id", mine=True), op="list")
        own_channel_id = me["items"][0]["id"]
        if video_id:
            params["videoId"] = video_id
        else:
            params["allThreadsRelatedToChannelId"] = own_channel_id
        threads = yt.paginate(yt.data.commentThreads(), "list", limit=limit, **params)

        matches = []
        newest_seen = watermark
        for t in threads:
            top = t["snippet"]["topLevelComment"]["snippet"]
            published = top.get("publishedAt", "")
            if newest_seen is None or published > newest_seen:
                newest_seen = published
            if watermark and published <= watermark:
                continue
            if top.get("authorChannelId", {}).get("value") == own_channel_id:
                continue  # channel's own comments are exempt
            text = top.get("textOriginal", top.get("textDisplay", ""))
            hits = match_comment(text, words)
            if hits:
                matches.append(
                    {
                        "comment_id": t["snippet"]["topLevelComment"]["id"],
                        "video_id": t["snippet"].get("videoId"),
                        "author": top.get("authorDisplayName"),
                        "author_channel_id": top.get("authorChannelId", {}).get("value"),
                        "text": text[:200],
                        "matched": hits,
                    }
                )

        result = {
            "scanned": len(threads),
            "matches": matches,
            "words_checked": len(words),
        }
        if incremental:
            result["watermark_was"] = watermark
            if newest_seen and not dry_run:
                SCAN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                SCAN_STATE_PATH.write_text(json.dumps({"last_scan": newest_seen}))
                result["watermark_now"] = newest_seen
        if auto_action != "none" and matches:
            if dry_run:
                result["moderation"] = preview(
                    "scan_comments.auto_action",
                    {"action": auto_action, "comment_ids": [m["comment_id"] for m in matches]},
                )
            else:
                for m in matches:
                    yt.call(
                        yt.data.comments().setModerationStatus(
                            id=m["comment_id"], moderationStatus=auto_action
                        ),
                        op="setModerationStatus",
                    )
                result["moderation"] = {
                    "action": auto_action,
                    "applied_to": len(matches),
                }
        return result
