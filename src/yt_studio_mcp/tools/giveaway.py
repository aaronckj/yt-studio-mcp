"""Auditable comment-entry giveaway suite.

Entry model: one entry per distinct video per commenter inside the window,
capped at max_entries_per_user (chronological first-N). Drawing is
deterministic: a seeded PRNG over the canonically sorted entry list, taking
the first n distinct channels — same snapshot + same seed always reproduces
the same winners, so a recorded draw is independently verifiable.
"""

from __future__ import annotations

import hashlib
import json
import secrets as pysecrets
from datetime import UTC, datetime
from pathlib import Path
from random import Random

from ..client import get_yt, preview

SNAPSHOT_DIR = Path.home() / ".config" / "yt-studio-mcp" / "giveaways"
ALGO_VERSION = "v1"


def resolve_giveaway_path(path: str) -> Path:
    """Resolve a snapshot/audit/CSV path, refusing anything outside SNAPSHOT_DIR.

    Guards against path traversal / arbitrary file access if a hostile prompt
    (e.g. injected via comment text) steers tool arguments.
    """
    rp = Path(path).expanduser().resolve()
    base = SNAPSHOT_DIR.resolve()
    if not rp.is_relative_to(base):
        raise ValueError(f"path must be inside {base}")
    return rp


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def build_entries(
    comments: list[dict],
    start: str,
    end: str,
    max_entries_per_user: int,
    exclude_channel_ids: list[str],
) -> list[dict]:
    """Pure entry-derivation logic (unit-tested independently of the API)."""
    lo, hi = _parse_ts(start), _parse_ts(end)
    excluded = set(exclude_channel_ids)
    per_user_videos: dict[str, set[str]] = {}
    entries: list[dict] = []
    for c in sorted(comments, key=lambda c: c["published"]):
        ch = c["author_channel_id"]
        if not ch or ch in excluded:
            continue
        ts = _parse_ts(c["published"])
        if ts < lo or ts > hi:
            continue
        seen = per_user_videos.setdefault(ch, set())
        if c["video_id"] in seen or len(seen) >= max_entries_per_user:
            continue
        seen.add(c["video_id"])
        entries.append(
            {
                "channel_id": ch,
                "author": c.get("author", ""),
                "video_id": c["video_id"],
                "comment_id": c["comment_id"],
                "published": c["published"],
            }
        )
    return entries


def canonical_hash(entries: list[dict]) -> str:
    canon = json.dumps(
        sorted(entries, key=lambda e: (e["channel_id"], e["video_id"], e["comment_id"])),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode()).hexdigest()


def deterministic_draw(entries: list[dict], n: int, seed: str) -> list[dict]:
    """Seeded shuffle of the canonically sorted entries; first n distinct channels win."""
    ordered = sorted(entries, key=lambda e: (e["channel_id"], e["video_id"], e["comment_id"]))
    rng = Random(seed)
    rng.shuffle(ordered)
    winners: list[dict] = []
    seen: set[str] = set()
    for e in ordered:
        if e["channel_id"] in seen:
            continue
        seen.add(e["channel_id"])
        winners.append(e)
        if len(winners) == n:
            break
    return winners


def register(mcp) -> None:
    @mcp.tool()
    def collect_entries(
        start: str,
        end: str,
        max_entries_per_user: int = 5,
        exclude_channel_ids: list[str] | None = None,
        comment_limit_per_video: int = 2000,
    ) -> dict:
        """Collect giveaway entries: walk every upload's comments in the window
        (RFC3339 timestamps), one entry per distinct video per commenter, capped
        per user. Writes an auditable snapshot file and returns its hash."""
        yt = get_yt()
        me = yt.call(yt.data.channels().list(part="id,contentDetails", mine=True), op="list")[
            "items"
        ][0]
        own_id = me["id"]
        uploads = me["contentDetails"]["relatedPlaylists"]["uploads"]
        video_items = yt.paginate(
            yt.data.playlistItems(), "list", limit=500, part="snippet", playlistId=uploads
        )
        video_ids = [i["snippet"]["resourceId"]["videoId"] for i in video_items]

        raw: list[dict] = []
        for vid in video_ids:
            threads = yt.paginate(
                yt.data.commentThreads(),
                "list",
                limit=comment_limit_per_video,
                part="snippet",
                videoId=vid,
                order="time",
                textFormat="plainText",
            )
            for t in threads:
                top = t["snippet"]["topLevelComment"]["snippet"]
                raw.append(
                    {
                        "author_channel_id": top.get("authorChannelId", {}).get("value", ""),
                        "author": top.get("authorDisplayName", ""),
                        "video_id": vid,
                        "comment_id": t["snippet"]["topLevelComment"]["id"],
                        "published": top["publishedAt"],
                    }
                )

        entries = build_entries(
            raw, start, end, max_entries_per_user, [own_id, *(exclude_channel_ids or [])]
        )
        digest = canonical_hash(entries)
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        snapshot_path = SNAPSHOT_DIR / f"entries-{stamp}.json"
        snapshot_path.write_text(
            json.dumps(
                {
                    "algo": ALGO_VERSION,
                    "window": {"start": start, "end": end},
                    "max_entries_per_user": max_entries_per_user,
                    "excluded": [own_id, *(exclude_channel_ids or [])],
                    "videos_scanned": len(video_ids),
                    "comments_seen": len(raw),
                    "entry_hash": digest,
                    "entries": entries,
                },
                indent=2,
            )
        )
        participants = len({e["channel_id"] for e in entries})
        return {
            "snapshot_path": str(snapshot_path),
            "entry_hash": digest,
            "entries": len(entries),
            "participants": participants,
            "videos_scanned": len(video_ids),
            "quota_spent": yt.quota.spent,
        }

    @mcp.tool()
    def draw_winners(snapshot_path: str, n: int = 5, seed: str = "") -> dict:
        """Deterministically draw n winners (distinct channels) from a snapshot.
        Same snapshot + seed always reproduces the same winners. Writes an audit
        record next to the snapshot."""
        if not seed:
            return {"error": "provide a seed (announce it publicly for verifiability)"}
        try:
            path = resolve_giveaway_path(snapshot_path)
        except ValueError as exc:
            return {"error": str(exc)}
        snap = json.loads(path.read_text())
        entries = snap["entries"]
        digest = canonical_hash(entries)
        if digest != snap["entry_hash"]:
            return {"error": "snapshot hash mismatch — file was modified after collection"}
        winners = deterministic_draw(entries, n, seed)
        audit = {
            "algo": ALGO_VERSION,
            "snapshot": path.name,
            "entry_hash": digest,
            "seed": seed,
            "n": n,
            "winners": winners,
            "verification_codes": {},
        }
        audit_path = path.with_name(path.stem + f"-draw-{seed}.json")
        audit_path.write_text(json.dumps(audit, indent=2))
        return {
            "winners": winners,
            "audit_path": str(audit_path),
            "entry_hash": digest,
            "reproduce": f"draw_winners(snapshot_path, n={n}, seed={seed!r})",
        }

    @mcp.tool()
    def make_verification_code(audit_path: str, winner_channel_id: str) -> dict:
        """Generate a short verification code for a winner and record it in the audit file."""
        try:
            path = resolve_giveaway_path(audit_path)
        except ValueError as exc:
            return {"error": str(exc)}
        audit = json.loads(path.read_text())
        if winner_channel_id not in {w["channel_id"] for w in audit["winners"]}:
            return {"error": f"{winner_channel_id} is not a winner in this audit record"}
        code = pysecrets.token_hex(4)
        audit["verification_codes"][winner_channel_id] = code
        path.write_text(json.dumps(audit, indent=2))
        return {"channel_id": winner_channel_id, "code": code}

    @mcp.tool()
    def check_verification_reply(audit_path: str, comment_id: str, winner_channel_id: str) -> dict:
        """Verify a winner's reply: fetches the comment and checks it was authored
        by the winning channel and contains their verification code."""
        try:
            audit = json.loads(resolve_giveaway_path(audit_path).read_text())
        except ValueError as exc:
            return {"error": str(exc)}
        code = audit["verification_codes"].get(winner_channel_id)
        if not code:
            return {"error": "no verification code issued for that channel"}
        yt = get_yt()
        res = yt.call(
            yt.data.comments().list(part="snippet", id=comment_id, textFormat="plainText"),
            op="list",
        )
        items = res.get("items", [])
        if not items:
            return {"verified": False, "reason": "comment not found"}
        sn = items[0]["snippet"]
        author = sn.get("authorChannelId", {}).get("value")
        text = sn.get("textOriginal", sn.get("textDisplay", ""))
        verified = author == winner_channel_id and code in text
        return {
            "verified": verified,
            "author_matches": author == winner_channel_id,
            "code_present": code in text,
        }

    @mcp.tool()
    def export_entries_csv(snapshot_path: str, csv_path: str | None = None) -> dict:
        """Export a snapshot's entries to CSV (channel_id, author, video_id,
        comment_id, published) for spreadsheets or records."""
        import csv

        try:
            path = resolve_giveaway_path(snapshot_path)
            out_path = (
                resolve_giveaway_path(csv_path) if csv_path else path.with_suffix(".csv")
            )
        except ValueError as exc:
            return {"error": str(exc)}
        snap = json.loads(path.read_text())
        fields = ["channel_id", "author", "video_id", "comment_id", "published"]
        with out_path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(
                {k: e.get(k, "") for k in fields} for e in snap["entries"]
            )
        return {
            "csv_path": str(out_path),
            "entries": len(snap["entries"]),
            "entry_hash": snap["entry_hash"],
        }

    @mcp.tool()
    def winner_announcement(audit_path: str, claim_days: int = 7, contact: str = "") -> dict:
        """Draft winner-announcement text (for a pinned comment / community post)
        from a draw's audit record. Review before posting."""
        try:
            audit = json.loads(resolve_giveaway_path(audit_path).read_text())
        except ValueError as exc:
            return {"error": str(exc)}
        names = [w.get("author") or w["channel_id"] for w in audit["winners"]]
        lines = [
            "🎉 GIVEAWAY WINNERS 🎉",
            "",
            "Congratulations to:",
            *[f"  • {n}" for n in names],
            "",
            f"Winners: check for a reply on your comment, then follow the claim "
            f"instructions within {claim_days} days.",
        ]
        if contact:
            lines.append(f"Claims: {contact}")
        lines += [
            "",
            f"Drawn from {audit['n']}-winner random draw, seed \"{audit['seed']}\", "
            f"entry hash {audit['entry_hash'][:12]}… (independently verifiable).",
            "We will NEVER ask for payment or card details.",
        ]
        return {"text": "\n".join(lines), "winners": names}

    @mcp.tool()
    def post_winner_reply(comment_id: str, text: str, dry_run: bool = False) -> dict:
        """Reply to a winning comment as the channel (winner announcement/notification)."""
        if dry_run:
            return preview("post_winner_reply", {"comment_id": comment_id, "text": text})
        yt = get_yt()
        res = yt.call(
            yt.data.comments().insert(
                part="snippet",
                body={"snippet": {"parentId": comment_id, "textOriginal": text}},
            ),
            op="insert",
        )
        return {"replied": res["id"], "parent": comment_id}
