"""Headless scan for cron: wordlist tier + optional LLM tier, no MCP client needed.

    yt-studio-mcp scan --incremental --auto-action heldForReview --llm

Tier 1: banned-word match (deterministic) → auto_action applies.
Tier 2 (--llm, only if YT_MCP_LLM_URL/MODEL set): every remaining NEW comment
is classified by the LLM; spam verdicts get auto_action too, tagged with the
model's reason. Exit codes: 0 = clean, 2 = matches found, 4 = incomplete
(moderation failed, an LLM call failed, or the limit cut the scan short --
the watermark is NOT advanced), 5 = another scan holds the lock, 1 = error
(including a missing/corrupt word list or watermark: the scan fails closed).
Optional ntfy push: set YT_MCP_NTFY_URL to get a phone ping on matches.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from urllib.request import Request, urlopen

logger = logging.getLogger("yt_studio_mcp")


def notify(matches: list[dict]) -> None:
    url = os.environ.get("YT_MCP_NTFY_URL")
    if not url or not matches:
        return
    lines = [f"{m.get('source', 'wordlist')}: {m['author']}: {m['text'][:80]}" for m in matches[:5]]
    body = f"{len(matches)} spam comment(s) held\n" + "\n".join(lines)
    try:
        with urlopen(
            Request(url, data=body.encode(), headers={"title": "YouTube spam scan"})
        ) as _:
            pass
    except Exception as exc:  # noqa: BLE001 - notification is best-effort
        logger.error("ntfy push failed: %s", exc)


def run_scan(incremental: bool, auto_action: str, use_llm: bool, limit: int) -> int:
    from .tools.banned_words import ScanLocked, scan_lock

    try:
        with scan_lock():
            return _run_scan_locked(incremental, auto_action, use_llm, limit)
    except ScanLocked as exc:
        logger.error("scan skipped: %s", exc)
        print(json.dumps({"skipped": str(exc)}), file=sys.stderr)
        return 5


def _run_scan_locked(incremental: bool, auto_action: str, use_llm: bool, limit: int) -> int:
    from .client import get_yt
    from .llm import llm_configured
    from .tools.banned_words import (
        apply_moderation,
        load_wordlist_strict,
        match_comment,
        read_watermark,
        truncation_gap,
        write_watermark,
    )

    # Fail closed: both raise StateError on missing/corrupt state (exit 1).
    words = load_wordlist_strict()
    watermark = read_watermark() if incremental else None
    if use_llm and not llm_configured():
        raise RuntimeError("--llm given but YT_MCP_LLM_URL/YT_MCP_LLM_MODEL are not set")

    yt = get_yt()
    me = yt.call(yt.data.channels().list(part="id", mine=True), op="list")
    channel_id = me["items"][0]["id"]  # own comments are exempt below
    threads, more = yt.paginate_more(
        yt.data.commentThreads(),
        "list",
        limit=limit,
        page_size=100,
        part="snippet",
        allThreadsRelatedToChannelId=channel_id,
        order="time",
        textFormat="plainText",
    )

    from . import llm as llm_mod

    matches: list[dict] = []
    llm_errors: list[dict] = []
    newest = watermark
    checked = 0
    for t in threads:
        top = t["snippet"]["topLevelComment"]["snippet"]
        published = top.get("publishedAt", "")
        if newest is None or published > newest:
            newest = published
        if watermark and published <= watermark:
            continue
        author_id = top.get("authorChannelId", {}).get("value", "")
        if author_id == channel_id:
            continue  # never scan/hold the channel's own comments
        checked += 1
        text = top.get("textOriginal", top.get("textDisplay", ""))
        author = top.get("authorDisplayName", "")
        entry = {
            "comment_id": t["snippet"]["topLevelComment"]["id"],
            "video_id": t["snippet"].get("videoId"),
            "author": author,
            "text": text[:200],
        }
        hits = match_comment(text, words)
        if hits:
            matches.append({**entry, "source": "wordlist", "matched": hits})
            continue
        if use_llm:
            verdict = llm_mod.classify_comment(author, text)
            if "error" in verdict:
                # An unclassified comment is NOT a clean comment.
                llm_errors.append({"comment_id": entry["comment_id"],
                                   "error": verdict["error"]})
            elif verdict.get("spam"):
                matches.append({**entry, "source": "llm", "reason": verdict["reason"]})

    failed: list[dict] = []
    if auto_action != "none" and matches:
        _, failed = apply_moderation(yt, matches, auto_action)

    gap = truncation_gap(threads, more, watermark)
    complete = not (failed or gap or llm_errors)
    # The watermark moves only when every new comment was read, classified
    # and acted on; otherwise the next run looks at the same range again.
    if incremental and newest and complete:
        write_watermark(newest)

    report = {
        "checked_new": checked,
        "matches": matches,
        "action": auto_action,
        "quota_spent": yt.quota.spent,
        "watermark_advanced": bool(incremental and newest and complete),
    }
    if failed:
        report["moderation_failed"] = failed
    if llm_errors:
        report["llm_errors"] = llm_errors
    if gap:
        report["truncated"] = gap
    _append_history(report)
    print(json.dumps(report, indent=2))
    notify(matches)
    if not complete:
        logger.error("scan incomplete: moderation_failed=%d llm_errors=%d truncated=%s",
                     len(failed), len(llm_errors), bool(gap))
        return 4
    return 2 if matches else 0


def _append_history(report: dict) -> None:
    """Append a timestamped run record to scan_history.jsonl (dashboard feed)."""
    from datetime import UTC, datetime

    from .paths import scan_history_path

    path = scan_history_path()
    record = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), **report}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:  # history is best-effort; the scan itself succeeded
        logger.error("history append failed: %s", exc)


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="yt-studio-mcp scan")
    parser.add_argument("--incremental", action="store_true")
    parser.add_argument(
        "--auto-action",
        default="none",
        choices=["none", "heldForReview", "rejected"],
    )
    parser.add_argument("--llm", action="store_true", help="classify new comments with the LLM")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args(argv)
    try:
        return run_scan(args.incremental, args.auto_action, args.llm, args.limit)
    except Exception as exc:  # noqa: BLE001 - cron needs a nonzero exit + message
        logger.error("scan failed: %s", exc)
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
