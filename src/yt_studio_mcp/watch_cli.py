"""Live-stream moderation loop (runs only while a broadcast is active).

    yt-studio-mcp watch [--interval 60] [--auto-action heldForReview] [--llm]

While the channel has an active/live broadcast, polls new comments AND live
chat on a tight cadence, screens both against the wordlist (+ optional LLM),
and moderates matches: comments -> setModerationStatus; live-chat -> delete.
Exits when the broadcast ends. Meant to run for the duration of a stream so
someone can be present instead of refereeing.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from urllib.request import Request, urlopen

logger = logging.getLogger("yt_studio_mcp")


def notify(msg: str) -> None:
    url = os.environ.get("YT_MCP_NTFY_URL")
    if not url:
        return
    try:
        with urlopen(Request(url, data=msg.encode(), headers={"title": "Live moderation"})):
            pass
    except Exception as exc:  # noqa: BLE001
        logger.error("ntfy failed: %s", exc)


def _active_broadcast(yt):
    res = yt.call(
        yt.data.liveBroadcasts().list(part="snippet,status", broadcastStatus="active",
                                      broadcastType="all", maxResults=1),
        op="list",
    )
    items = res.get("items", [])
    if not items:
        return None
    b = items[0]
    return {"id": b["id"], "chat_id": b["snippet"].get("liveChatId"),
            "title": b["snippet"]["title"]}


def run_watch(interval: int, auto_action: str, use_llm: bool) -> int:
    from .client import get_yt
    from .llm import classify_comment, llm_configured
    from .tools.banned_words import _load_words, match_comment

    yt = get_yt()
    words = _load_words()
    own = yt.call(yt.data.channels().list(part="id", mine=True), op="list")["items"][0]["id"]

    b = _active_broadcast(yt)
    if not b:
        print(json.dumps({"status": "no active broadcast; nothing to watch"}))
        return 0
    logger.info("watching broadcast %s (%s)", b["id"], b["title"])
    notify(f"🟢 Live moderation started for: {b['title']}")

    seen_comments: set[str] = set()
    chat_page = None
    held = 0

    def is_spam(author, text):
        if match_comment(text, words):
            return "wordlist"
        if use_llm and llm_configured() and classify_comment(author, text).get("spam"):
            return "llm"
        return None

    while True:
        b = _active_broadcast(yt)
        if not b:
            break
        # 1) new top-level comments on the live video
        try:
            threads = yt.paginate(
                yt.data.commentThreads(), "list", limit=100, part="snippet",
                videoId=b["id"], order="time", textFormat="plainText",
            )
            for t in threads:
                cid = t["snippet"]["topLevelComment"]["id"]
                if cid in seen_comments:
                    continue
                seen_comments.add(cid)
                top = t["snippet"]["topLevelComment"]["snippet"]
                if top.get("authorChannelId", {}).get("value") == own:
                    continue
                src = is_spam(top.get("authorDisplayName", ""),
                              top.get("textOriginal", top.get("textDisplay", "")))
                if src and auto_action != "none":
                    yt.call(yt.data.comments().setModerationStatus(
                        id=cid, moderationStatus=auto_action), op="setModerationStatus")
                    held += 1
                    notify(f"held comment ({src}): {top.get('authorDisplayName')}")
        except Exception as exc:  # noqa: BLE001 - comments may be disabled on live
            logger.error("comment poll: %s", exc)
        # 2) live chat
        if b.get("chat_id"):
            try:
                params = {"liveChatId": b["chat_id"], "part": "snippet,authorDetails",
                          "maxResults": 200}
                if chat_page:
                    params["pageToken"] = chat_page
                res = yt.call(yt.data.liveChatMessages().list(**params), op="list")
                chat_page = res.get("nextPageToken")
                for m in res.get("items", []):
                    ad = m["authorDetails"]
                    if ad.get("isChatOwner") or ad.get("isChatModerator"):
                        continue
                    text = m["snippet"].get("displayMessage", "")
                    if is_spam(ad["displayName"], text):
                        yt.call(yt.data.liveChatMessages().delete(id=m["id"]), op="delete")
                        held += 1
                        notify(f"deleted chat: {ad['displayName']}")
            except Exception as exc:  # noqa: BLE001
                logger.error("chat poll: %s", exc)
        logger.info("cycle done, held so far=%d, quota=%d", held, yt.quota.spent)
        time.sleep(max(15, interval))

    notify(f"🔴 Live moderation ended. Held {held} items this stream.")
    print(json.dumps({"status": "broadcast ended", "held": held,
                      "quota_spent": yt.quota.spent}))
    return 0


def main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="yt-studio-mcp watch")
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--auto-action", default="heldForReview",
                   choices=["none", "heldForReview", "rejected"])
    p.add_argument("--llm", action="store_true")
    a = p.parse_args(argv)
    try:
        return run_watch(a.interval, a.auto_action, a.llm)
    except Exception as exc:  # noqa: BLE001
        logger.error("watch failed: %s", exc)
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        notify(f"⚠️ Live moderation crashed: {exc}")
        return 1
