"""Auditable comment-entry giveaway suite.

Entry model: one entry per distinct video per commenter inside the window,
capped at max_entries_per_user (chronological first-N). Only top-level,
published comments count (held/spam-filtered comments are not visible to the
API and therefore not entries).

Drawing (algo v2, the default) is language-independent and binds the seed to
the frozen entry set: every entry gets the rank

    sha256("v2|" + seed + "|" + entry_hash + "|" + channel_id + "|" + video_id
           + "|" + comment_id)

and the n lowest-ranked DISTINCT channels win. Anyone with the snapshot and
the seed can recompute it with any sha256 tool. v1 (Python's Mersenne-Twister
shuffle) is kept only so draws recorded before v2 stay reproducible.

Integrity:
  * the snapshot stores entry_hash (entries) and meta_hash (window, caps,
    exclusions, completeness); both are re-checked before every draw;
  * every draw -- including re-runs and redraws -- is appended to a
    hash-chained <snapshot>-draws.jsonl log, and a second draw with a
    DIFFERENT seed is refused unless a redraw_reason is recorded, so "draw
    until the result is liked" leaves a trail;
  * audit records are re-derived from the snapshot before they are trusted
    (verification codes, announcements), so editing the winners in an audit
    file is detected rather than believed;
  * a snapshot whose collection hit a page limit is marked incomplete and
    cannot be drawn without allow_incomplete=True.

For the draw to be verifiable by viewers, publish the entry_hash BEFORE the
seed exists, and use a seed nobody controls (e.g. a public randomness beacon
round announced in advance).
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import secrets as pysecrets
from datetime import UTC, datetime
from pathlib import Path
from random import Random

from ..client import get_yt, preview
from ..paths import giveaway_dir
from ..validate import parse_rfc3339


def __getattr__(name: str):
    if name == "SNAPSHOT_DIR":  # back-compat for the former constant
        return giveaway_dir()
    raise AttributeError(name)


ALGO_VERSION = "v2"  # default draw algorithm
DRAW_ALGOS = ("v1", "v2")
UPLOADS_LIMIT = 5000
COMMENTS_PER_VIDEO_DEFAULT = 20000


def resolve_giveaway_path(path: str) -> Path:
    """Resolve a snapshot/audit/CSV path, refusing anything outside the giveaway dir.

    Guards against path traversal / arbitrary file access if a hostile prompt
    (e.g. injected via comment text) steers tool arguments.
    """
    rp = Path(path).expanduser().resolve()
    base = giveaway_dir().resolve()
    if not rp.is_relative_to(base):
        raise ValueError(f"path must be inside {base}")
    return rp


def _parse_ts(ts: str) -> datetime:
    return parse_rfc3339(ts)


def _entry_key(e: dict) -> tuple:
    return (e["channel_id"], e["video_id"], e["comment_id"])


def build_entries(
    comments: list[dict],
    start: str,
    end: str,
    max_entries_per_user: int,
    exclude_channel_ids: list[str],
) -> list[dict]:
    """Pure entry-derivation logic (unit-tested independently of the API)."""
    lo, hi = _parse_ts(start), _parse_ts(end)
    if lo >= hi:
        raise ValueError("window start must be before window end")
    if max_entries_per_user < 1:
        raise ValueError("max_entries_per_user must be >= 1")
    excluded = set(exclude_channel_ids)
    per_user_videos: dict[str, set[str]] = {}
    entries: list[dict] = []
    # Ties on the timestamp are broken by ids so the result never depends on
    # the order the API happened to return comments in.
    ordered = sorted(
        comments,
        key=lambda c: (_parse_ts(c["published"]), c["video_id"], c["comment_id"]),
    )
    for c in ordered:
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
        sorted(entries, key=_entry_key),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode()).hexdigest()


META_FIELDS = (
    "algo", "window", "max_entries_per_user", "excluded", "videos_scanned",
    "comments_seen", "complete", "truncated_videos", "uploads_truncated",
)


def meta_hash(snap: dict) -> str:
    meta = {k: snap.get(k) for k in META_FIELDS}
    meta["entry_hash"] = snap.get("entry_hash")
    return hashlib.sha256(
        json.dumps(meta, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _draw_v1(entries: list[dict], seed: str) -> list[dict]:
    ordered = sorted(entries, key=_entry_key)
    Random(seed).shuffle(ordered)
    return ordered


def draw_rank(seed: str, entry_hash: str, e: dict) -> str:
    material = "|".join(("v2", seed, entry_hash, e["channel_id"], e["video_id"],
                         e["comment_id"]))
    return hashlib.sha256(material.encode()).hexdigest()


def _draw_v2(entries: list[dict], seed: str) -> list[dict]:
    digest = canonical_hash(entries)
    return sorted(entries, key=lambda e: (draw_rank(seed, digest, e), _entry_key(e)))


def deterministic_draw(entries: list[dict], n: int, seed: str, algo: str = "v1") -> list[dict]:
    """First n distinct channels of the seeded ordering.

    algo="v1" is the legacy Python-Random shuffle (default here only so old
    callers and recorded draws keep reproducing); draw_winners uses v2.
    """
    if algo not in DRAW_ALGOS:
        raise ValueError(f"algo must be one of {DRAW_ALGOS}")
    ordered = _draw_v1(entries, seed) if algo == "v1" else _draw_v2(entries, seed)
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


def _write_exclusive(path: Path, text: str) -> None:
    """Create a file that must not already exist (never silently overwrite)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{pysecrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def collect_raw(yt, comment_limit_per_video: int = COMMENTS_PER_VIDEO_DEFAULT) -> dict:
    """Fetch every upload's top-level comments, recording anything cut off."""
    me = yt.call(yt.data.channels().list(part="id,contentDetails", mine=True), op="list")[
        "items"
    ][0]
    own_id = me["id"]
    uploads = me["contentDetails"]["relatedPlaylists"]["uploads"]
    video_items, uploads_truncated = yt.paginate_more(
        yt.data.playlistItems(), "list", limit=UPLOADS_LIMIT, part="snippet",
        playlistId=uploads,
    )
    video_ids = [i["snippet"]["resourceId"]["videoId"] for i in video_items]
    raw: list[dict] = []
    truncated: list[str] = []
    for vid in video_ids:
        threads, more = yt.paginate_more(
            yt.data.commentThreads(),
            "list",
            limit=comment_limit_per_video,
            page_size=100,
            part="snippet",
            videoId=vid,
            order="time",
            textFormat="plainText",
        )
        if more:
            truncated.append(vid)
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
    return {"own_id": own_id, "video_ids": video_ids, "raw": raw,
            "truncated_videos": truncated, "uploads_truncated": uploads_truncated}


def freeze_snapshot(
    collected: dict, start: str, end: str, max_entries_per_user: int,
    exclude_channel_ids: list[str],
) -> dict:
    """Derive entries and write the snapshot file. Shared by the tool and CLI."""
    excluded = [collected["own_id"], *exclude_channel_ids]
    entries = build_entries(collected["raw"], start, end, max_entries_per_user, excluded)
    digest = canonical_hash(entries)
    snap = {
        "algo": ALGO_VERSION,
        "window": {"start": start, "end": end},
        "max_entries_per_user": max_entries_per_user,
        "excluded": excluded,
        "videos_scanned": len(collected["video_ids"]),
        "comments_seen": len(collected["raw"]),
        "complete": not (collected["truncated_videos"] or collected["uploads_truncated"]),
        "truncated_videos": collected["truncated_videos"],
        "uploads_truncated": collected["uploads_truncated"],
        "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "entry_hash": digest,
        "entries": entries,
    }
    snap["meta_hash"] = meta_hash(snap)
    giveaway_dir().mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    body = json.dumps(snap, indent=2)
    for attempt in range(100):
        suffix = f"-{attempt}" if attempt else ""
        path = giveaway_dir() / f"entries-{stamp}-{digest[:8]}{suffix}.json"
        try:
            _write_exclusive(path, body)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("could not allocate a unique snapshot filename")
    return {
        "snapshot_path": str(path),
        "entry_hash": digest,
        "entries": len(entries),
        "participants": len({e["channel_id"] for e in entries}),
        "videos_scanned": len(collected["video_ids"]),
        "complete": snap["complete"],
        "truncated_videos": collected["truncated_videos"],
        "uploads_truncated": collected["uploads_truncated"],
    }


def load_snapshot(path: Path) -> dict:
    """Load a snapshot and verify both hashes; raises ValueError on tamper."""
    snap = json.loads(path.read_text())
    if canonical_hash(snap["entries"]) != snap["entry_hash"]:
        raise ValueError("snapshot hash mismatch — entries were modified after collection")
    if "meta_hash" in snap and meta_hash(snap) != snap["meta_hash"]:
        raise ValueError("snapshot meta mismatch — window/caps/exclusions were modified")
    return snap


def _draw_log_path(snapshot: Path) -> Path:
    return snapshot.with_name(snapshot.stem + "-draws.jsonl")


def read_draw_log(snapshot: Path) -> list[dict]:
    """Read and chain-verify the append-only draw log."""
    p = _draw_log_path(snapshot)
    if not p.exists():
        return []
    records, prev = [], "0" * 64
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("prev") != prev:
            raise ValueError(f"draw log {p.name} chain broken — a record was edited or removed")
        records.append(rec)
        prev = hashlib.sha256(line.encode()).hexdigest()
    return records


def _append_draw_log(snapshot: Path, record: dict) -> None:
    records = read_draw_log(snapshot)  # verifies the chain before extending it
    p = _draw_log_path(snapshot)
    prev = "0" * 64
    if records:
        last = [ln for ln in p.read_text().splitlines() if ln.strip()][-1]
        prev = hashlib.sha256(last.encode()).hexdigest()
    line = json.dumps({**record, "prev": prev}, sort_keys=True, separators=(",", ":"))
    with p.open("a") as fh:
        fh.write(line + "\n")


def _seed_tag(seed: str) -> str:
    # The seed is free text; it never goes into a filename verbatim.
    return hashlib.sha256(seed.encode()).hexdigest()[:12]


def load_verified_audit(audit_path: Path) -> dict:
    """Load an audit record and re-derive its winners from the snapshot.

    An audit file is just JSON on disk; editing "winners" must not let a
    different channel receive a verification code or an announcement.
    """
    audit = json.loads(audit_path.read_text())
    snap_path = audit_path.with_name(audit["snapshot"])
    snap = load_snapshot(snap_path)
    if snap["entry_hash"] != audit.get("entry_hash"):
        raise ValueError("audit entry_hash does not match its snapshot")
    algo = audit.get("draw_algo", audit.get("algo", "v1"))
    expected = deterministic_draw(snap["entries"], audit["n"], audit["seed"], algo=algo)
    if expected != audit.get("winners"):
        raise ValueError("audit winners do not match a re-draw from the snapshot — "
                         "the audit file was modified")
    return audit


def csv_safe(value: str) -> str:
    """Neutralise spreadsheet formula injection from viewer-controlled names."""
    value = "" if value is None else str(value)
    if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def register(mcp) -> None:
    @mcp.tool()
    def collect_entries(
        start: str,
        end: str,
        max_entries_per_user: int = 5,
        exclude_channel_ids: list[str] | None = None,
        comment_limit_per_video: int = COMMENTS_PER_VIDEO_DEFAULT,
    ) -> dict:
        """Collect giveaway entries: walk every upload's comments in the window
        (RFC3339 timestamps WITH an offset), one entry per distinct video per
        commenter, capped per user. Writes an auditable snapshot file (never
        overwrites one) and returns its hash. If any video had more comments
        than comment_limit_per_video, the snapshot is marked incomplete and
        lists those videos -- raise the limit and collect again before drawing."""
        try:
            lo, hi = _parse_ts(start), _parse_ts(end)
        except ValueError as exc:
            return {"error": str(exc)}
        if lo >= hi:
            return {"error": "window start must be before window end"}
        if max_entries_per_user < 1:
            return {"error": "max_entries_per_user must be >= 1"}
        yt = get_yt()
        collected = collect_raw(yt, comment_limit_per_video)
        out = freeze_snapshot(
            collected, start, end, max_entries_per_user, exclude_channel_ids or []
        )
        out["quota_spent"] = yt.quota.spent
        if not out["complete"]:
            out["warning"] = (
                "collection hit a page limit; entrants may be missing. Re-collect "
                "with a higher comment_limit_per_video before drawing."
            )
        return out

    @mcp.tool()
    def draw_winners(
        snapshot_path: str,
        n: int = 5,
        seed: str = "",
        algo: str = ALGO_VERSION,
        redraw_reason: str = "",
        allow_incomplete: bool = False,
    ) -> dict:
        """Deterministically draw n winners (distinct channels) from a snapshot.

        Same snapshot + seed + algo always reproduces the same winners; the
        result includes the exact recipe. Every call is appended to a
        tamper-evident draw log next to the snapshot. Drawing again with a
        DIFFERENT seed is refused unless redraw_reason explains why (it is
        recorded). Re-running the same draw returns the existing audit record
        unchanged, so issued verification codes are never wiped."""
        if not seed:
            return {"error": "provide a seed (announce it publicly for verifiability)"}
        if algo not in DRAW_ALGOS:
            return {"error": f"algo must be one of {DRAW_ALGOS}"}
        if n < 1:
            return {"error": "n must be >= 1"}
        try:
            path = resolve_giveaway_path(snapshot_path)
            snap = load_snapshot(path)
            prior = read_draw_log(path)
        except (ValueError, OSError, KeyError) as exc:
            return {"error": str(exc)}
        if snap.get("complete") is False and not allow_incomplete:
            return {
                "error": "snapshot is INCOMPLETE (collection hit a page limit on "
                f"{len(snap.get('truncated_videos', []))} video(s)); re-collect with a "
                "higher limit, or pass allow_incomplete=True to draw anyway",
            }
        entries = snap["entries"]
        digest = snap["entry_hash"]
        other_seeds = sorted({r["seed_sha256"] for r in prior} - {
            hashlib.sha256(seed.encode()).hexdigest()})
        if other_seeds and not redraw_reason.strip():
            return {
                "error": f"this snapshot was already drawn with {len(other_seeds)} other "
                "seed(s). A redraw must be justified: pass redraw_reason (it is "
                "recorded in the draw log).",
                "prior_draws": prior,
            }
        winners = deterministic_draw(entries, n, seed, algo=algo)
        audit_path = path.with_name(f"{path.stem}-draw-{algo}-{_seed_tag(seed)}-n{n}.json")
        audit = {
            "algo": algo,  # kept for v1-era readers
            "draw_algo": algo,
            "snapshot": path.name,
            "entry_hash": digest,
            "seed": seed,
            "n": n,
            "winners": winners,
            "participants": len({e["channel_id"] for e in entries}),
            "verification_codes": {},
        }
        reused = False
        if audit_path.exists():
            existing = json.loads(audit_path.read_text())
            if existing.get("winners") == winners and existing.get("seed") == seed:
                audit, reused = existing, True
            else:
                return {"error": f"{audit_path.name} exists with different content; "
                        "refusing to overwrite an audit record"}
        else:
            _write_exclusive(audit_path, json.dumps(audit, indent=2))
        _append_draw_log(path, {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "algo": algo,
            "seed": seed,
            "seed_sha256": hashlib.sha256(seed.encode()).hexdigest(),
            "n": n,
            "entry_hash": digest,
            "winners": [w["channel_id"] for w in winners],
            "audit": audit_path.name,
            "rerun_of_existing": reused,
            "redraw_reason": redraw_reason.strip() or None,
        })
        return {
            "winners": winners,
            "audit_path": str(audit_path),
            "entry_hash": digest,
            "algo": algo,
            "prior_draws": len(prior),
            "reused_existing_audit": reused,
            "reproduce": f"draw_winners(snapshot_path, n={n}, seed={seed!r}, algo={algo!r})",
        }

    @mcp.tool()
    def make_verification_code(
        audit_path: str, winner_channel_id: str, rotate: bool = False
    ) -> dict:
        """Generate a short verification code for a winner and record it in the
        audit file. Returns the existing code if one was already issued (pass
        rotate=True to replace it -- the old code stops verifying)."""
        try:
            path = resolve_giveaway_path(audit_path)
            audit = load_verified_audit(path)
        except (ValueError, OSError, KeyError) as exc:
            return {"error": str(exc)}
        if winner_channel_id not in {w["channel_id"] for w in audit["winners"]}:
            return {"error": f"{winner_channel_id} is not a winner in this audit record"}
        codes = audit.setdefault("verification_codes", {})
        if winner_channel_id in codes and not rotate:
            return {"channel_id": winner_channel_id, "code": codes[winner_channel_id],
                    "existing": True}
        code = pysecrets.token_hex(4)
        codes[winner_channel_id] = code
        _write_atomic(path, json.dumps(audit, indent=2))
        return {"channel_id": winner_channel_id, "code": code}

    @mcp.tool()
    def check_verification_reply(audit_path: str, comment_id: str, winner_channel_id: str) -> dict:
        """Verify a winner's reply: fetches the comment and checks it was authored
        by the winning channel and contains their verification code."""
        try:
            audit = load_verified_audit(resolve_giveaway_path(audit_path))
        except (ValueError, OSError, KeyError) as exc:
            return {"error": str(exc)}
        if winner_channel_id not in {w["channel_id"] for w in audit["winners"]}:
            return {"error": f"{winner_channel_id} is not a winner in this audit record"}
        code = audit.get("verification_codes", {}).get(winner_channel_id)
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
        comment_id, published) for spreadsheets or records. Display names are
        viewer-controlled, so cells that would start a spreadsheet formula are
        prefixed with an apostrophe."""
        try:
            path = resolve_giveaway_path(snapshot_path)
            out_path = (
                resolve_giveaway_path(csv_path) if csv_path else path.with_suffix(".csv")
            )
            snap = load_snapshot(path)
        except (ValueError, OSError, KeyError) as exc:
            return {"error": str(exc)}
        if out_path.suffix.lower() != ".csv":
            return {"error": "csv_path must end in .csv"}
        fields = ["channel_id", "author", "video_id", "comment_id", "published"]
        with out_path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(
                {k: csv_safe(e.get(k, "")) for k in fields} for e in snap["entries"]
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
            audit = load_verified_audit(resolve_giveaway_path(audit_path))
        except (ValueError, OSError, KeyError) as exc:
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
