"""Banned-word comment scanning.

YouTube's Studio blocked-words list has no public API, so this module keeps
its own wordlist and sweeps comments against it, with optional automatic
moderation (hold or reject) of matches. Matching is obfuscation-tolerant:
case-insensitive and separator-insensitive (``s.P-a m`` matches ``spam``)
while still requiring word boundaries so substrings of longer words don't
false-positive.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets as pysecrets
from contextlib import contextmanager
from pathlib import Path

from ..client import get_yt, preview
from ..paths import config_dir, scan_state_path, wordlist_path
from ..validate import parse_rfc3339


class StateError(RuntimeError):
    """Local scan state is missing, unreadable or corrupt -- never 'empty'."""


class ScanLocked(RuntimeError):
    """Another scan holds the lock (cron run overlapping a manual one)."""


def __getattr__(name: str):
    # Back-compat for the former module constants.
    if name == "WORDLIST_PATH":
        return wordlist_path()
    if name == "SCAN_STATE_PATH":
        return scan_state_path()
    raise AttributeError(name)


_SEP = re.compile(r"[\s\.\-_,!\$\*\|]+")


def normalize(text: str) -> str:
    return _SEP.sub("", text.lower())


def _load_words() -> list[str]:
    """Lenient read for DISPLAY only: a fresh install has no list yet."""
    if wordlist_path().exists():
        return json.loads(wordlist_path().read_text())
    return []


def load_wordlist_strict() -> list[str]:
    """The list a scan acts on. Missing, unreadable, malformed or empty is an
    error: scanning with no words would let every comment through and then
    advance the watermark past them."""
    p = wordlist_path()
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError as exc:
        raise StateError(f"banned word list missing at {p}; refusing to scan") from exc
    except (OSError, ValueError) as exc:
        raise StateError(f"banned word list at {p} is unreadable: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(w, str) and w.strip() for w in data):
        raise StateError(f"banned word list at {p} must be a JSON list of non-empty strings")
    if not data:
        raise StateError(f"banned word list at {p} is empty; refusing to scan")
    return data


def read_watermark() -> str | None:
    """None only when no incremental scan has ever run (file absent)."""
    p = scan_state_path()
    try:
        raw = p.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateError(f"scan watermark {p} unreadable: {exc}") from exc
    try:
        data = json.loads(raw)
        mark = data["last_scan"]
        if not isinstance(mark, str):
            raise TypeError("last_scan is not a string")
        parse_rfc3339(mark)
    except (ValueError, KeyError, TypeError) as exc:
        raise StateError(
            f"scan watermark {p} is corrupt ({exc}); fix or delete it deliberately"
        ) from exc
    return mark


def _atomic_write(path: Path, text: str) -> None:
    """Unique temp name + rename: concurrent writers never share a temp file
    and a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{pysecrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _save_words(words: list[str]) -> None:
    _atomic_write(wordlist_path(), json.dumps(sorted(set(words)), indent=2))


@contextmanager
def scan_lock():
    """Exclusive, non-blocking lock shared by the scan CLI and scan_comments."""
    config_dir().mkdir(parents=True, exist_ok=True)
    fh = open(config_dir() / ".scan.lock", "w")  # noqa: SIM115 - held for the block
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ScanLocked("another scan is running; try again shortly") from exc
        yield
    finally:
        fh.close()


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


def truncation_gap(threads: list[dict], more: bool, watermark: str | None) -> dict | None:
    """When the fetch limit cut the listing off, describe what was never read.

    order=time lists newest first, so a cut-off means the OLDEST new comments
    were skipped; with an incremental watermark they would be skipped forever.
    """
    if not more or not threads:
        return None
    oldest = min(
        t["snippet"]["topLevelComment"]["snippet"].get("publishedAt", "") for t in threads
    )
    if watermark and oldest <= watermark:
        return None  # the window back to the watermark was fully covered
    return {
        "warning": "comment limit reached before the previous scan point; older "
        "comments in this range were NOT scanned",
        "unscanned_after": watermark,
        "unscanned_before": oldest,
    }


def apply_moderation(yt, matches: list[dict], action: str) -> tuple[list[str], list[dict]]:
    """Moderate each match independently; one failure must not hide the rest."""
    applied, failed = [], []
    for m in matches:
        try:
            yt.call(
                yt.data.comments().setModerationStatus(
                    id=m["comment_id"], moderationStatus=action
                ),
                op="setModerationStatus",
            )
            applied.append(m["comment_id"])
        except Exception as exc:  # noqa: BLE001 - reported per comment
            failed.append({"comment_id": m["comment_id"], "error": str(exc)})
            if getattr(exc, "reason", "") == "quotaExceeded":
                failed.extend(
                    {"comment_id": r["comment_id"], "error": "skipped: quota exhausted"}
                    for r in matches[len(applied) + len(failed):]
                )
                break
    return applied, failed


def write_watermark(value: str) -> None:
    _atomic_write(scan_state_path(), json.dumps({"last_scan": value}))


def register(mcp) -> None:
    @mcp.tool()
    def banned_words_list() -> dict:
        """Show the managed banned-word list used by scan_comments."""
        return {"words": _load_words(), "path": str(wordlist_path())}

    @mcp.tool()
    def banned_words_add(words: list[str]) -> dict:
        """Add words/phrases to the banned list."""
        clean = [w for w in words if isinstance(w, str) and w.strip()]
        if not clean:
            return {"error": "no non-empty words given"}
        try:
            current = load_wordlist_strict() if wordlist_path().exists() else []
        except StateError as exc:
            return {"error": str(exc)}  # never overwrite a list we could not read
        _save_words(current + clean)
        return {"words": load_wordlist_strict()}

    @mcp.tool()
    def banned_words_remove(words: list[str]) -> dict:
        """Remove words from the banned list."""
        drop = {w.lower() for w in words}
        try:
            current = load_wordlist_strict()
        except StateError as exc:
            return {"error": str(exc)}
        kept = [w for w in current if w.lower() not in drop]
        if not kept:
            return {"error": "refusing to empty the banned word list (scans would stop)"}
        _save_words(kept)
        return {"words": load_wordlist_strict()}

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
        incremental=True (channel-wide only) examines comments newer than the
        previous incremental scan; the watermark advances only when the whole
        batch was read and moderated. A missing/corrupt word list or watermark
        is an error, never an empty list."""
        if auto_action not in ("none", "heldForReview", "rejected"):
            return {"error": "auto_action must be none|heldForReview|rejected"}
        if incremental and video_id:
            # The watermark is channel-wide; advancing it from one video's
            # comments would skip every other video's newer comments.
            return {"error": "incremental scans are channel-wide; omit video_id"}
        try:
            words = load_wordlist_strict() + (extra_words or [])
            watermark = read_watermark() if incremental else None
        except StateError as exc:
            return {"error": str(exc)}
        try:
            with scan_lock():
                return _scan(video_id, limit, words, auto_action, incremental,
                             watermark, dry_run)
        except ScanLocked as exc:
            return {"error": str(exc)}

    def _scan(video_id, limit, words, auto_action, incremental, watermark, dry_run):
        if incremental:
            try:  # re-read under the lock: another run may have moved it
                watermark = read_watermark()
            except StateError as exc:
                return {"error": str(exc)}
        yt = get_yt()
        params: dict = {"part": "snippet", "order": "time", "textFormat": "plainText"}
        me = yt.call(yt.data.channels().list(part="id", mine=True), op="list")
        own_channel_id = me["items"][0]["id"]
        if video_id:
            params["videoId"] = video_id
        else:
            params["allThreadsRelatedToChannelId"] = own_channel_id
        threads, more = yt.paginate_more(
            yt.data.commentThreads(), "list", limit=limit, page_size=100, **params
        )

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
        gap = truncation_gap(threads, more, watermark)
        if gap:
            result["truncated"] = gap
        if auto_action != "none" and matches:
            if dry_run:
                result["moderation"] = preview(
                    "scan_comments.auto_action",
                    {"action": auto_action, "comment_ids": [m["comment_id"] for m in matches]},
                )
            else:
                applied, failed = apply_moderation(yt, matches, auto_action)
                result["moderation"] = {
                    "action": auto_action,
                    "applied_to": len(applied),
                    "failed": failed,
                }
        if incremental:
            result["watermark_was"] = watermark
            failed_any = bool(result.get("moderation", {}).get("failed"))
            # Advance only after the WHOLE batch was read and acted on: not on a
            # moderation failure, not when the limit cut the listing short.
            if newest_seen and not dry_run and not failed_any and not gap:
                write_watermark(newest_seen)
                result["watermark_now"] = newest_seen
        return result
