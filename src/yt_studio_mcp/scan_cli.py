"""Headless scan for cron: wordlist tier + optional LLM tier, no MCP client needed.

    yt-studio-mcp scan --incremental --auto-action heldForReview --llm

Tier 1: banned-word match (deterministic) → auto_action applies.
Tier 2 (--llm, only if YT_MCP_LLM_URL/MODEL set): every remaining NEW comment
is classified by the LLM; spam verdicts get auto_action too, tagged with the
model's reason. Exit code: 0 = clean, 2 = matches found (cron-friendly).
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
    from .client import get_yt
    from .llm import classify_comment, llm_configured
    from .tools.banned_words import (
        SCAN_STATE_PATH,
        _load_words,
        match_comment,
    )

    yt = get_yt()
    words = _load_words()
    watermark = None
    if incremental and SCAN_STATE_PATH.exists():
        watermark = json.loads(SCAN_STATE_PATH.read_text()).get("last_scan")

    me = yt.call(yt.data.channels().list(part="id", mine=True), op="list")
    channel_id = me["items"][0]["id"]
    threads = yt.paginate(
        yt.data.commentThreads(),
        "list",
        limit=limit,
        part="snippet",
        allThreadsRelatedToChannelId=channel_id,
        order="time",
        textFormat="plainText",
    )

    matches: list[dict] = []
    newest = watermark
    checked = 0
    for t in threads:
        top = t["snippet"]["topLevelComment"]["snippet"]
        published = top.get("publishedAt", "")
        if newest is None or published > newest:
            newest = published
        if watermark and published <= watermark:
            continue
        checked += 1
        text = top.get("textOriginal", top.get("textDisplay", ""))
        author = top.get("authorDisplayName", "")
        entry = {
            "comment_id": t["snippet"]["topLevelComment"]["id"],
            "video_id": t["snippet"].get("videoId"),
            "author": author,
            "text": text[:200],
        }
        hits = match_comment(text, words) if words else []
        if hits:
            matches.append({**entry, "source": "wordlist", "matched": hits})
            continue
        if use_llm and llm_configured():
            verdict = classify_comment(author, text)
            if verdict.get("spam"):
                matches.append({**entry, "source": "llm", "reason": verdict["reason"]})

    if auto_action != "none":
        for m in matches:
            yt.call(
                yt.data.comments().setModerationStatus(
                    id=m["comment_id"], moderationStatus=auto_action
                ),
                op="setModerationStatus",
            )

    if incremental and newest:
        SCAN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCAN_STATE_PATH.write_text(json.dumps({"last_scan": newest}))

    report = {
        "checked_new": checked,
        "matches": matches,
        "action": auto_action,
        "quota_spent": yt.quota.spent,
    }
    _append_history(report)
    print(json.dumps(report, indent=2))
    notify(matches)
    return 2 if matches else 0


def _append_history(report: dict) -> None:
    """Append a timestamped run record to scan_history.jsonl (dashboard feed)."""
    from datetime import UTC, datetime
    from pathlib import Path

    path = Path.home() / ".config" / "yt-studio-mcp" / "scan_history.jsonl"
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
