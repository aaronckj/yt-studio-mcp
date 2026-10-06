# yt-studio-mcp

An MCP (Model Context Protocol) server for managing a YouTube channel through
the official Google APIs — videos, comments and moderation, playlists, live
broadcasts, captions, analytics — plus an **auditable giveaway suite** for
running comment-entry giveaways with deterministic, independently verifiable
winner drawing.

- Official APIs only (YouTube Data v3 + YouTube Analytics v2). No scraping.
- Every mutating tool accepts `dry_run=true` and returns a preview instead of writing.
- No secrets in this repo, in logs, or in your MCP client config.

## Quick start

### 1. Create Google credentials (~5 minutes, one time)

1. Go to [console.cloud.google.com](https://console.cloud.google.com), create a project.
2. **APIs & Services → Library**: enable **YouTube Data API v3** and **YouTube Analytics API**.
3. **APIs & Services → OAuth consent screen**: External, add yourself as a test user.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID → Desktop app**;
   download the JSON as `client_secret.json`.

### 2. Authorize your channel

```bash
uvx yt-studio-mcp auth --client-secret /path/to/client_secret.json
```

A browser opens — sign in and **pick the channel** (Brand Accounts appear as
separate choices). The refresh token is stored locally (see Secret storage).

### 3. Connect your MCP client

```bash
# Claude Code
claude mcp add yt-studio -s user -- uvx yt-studio-mcp
```

## Tools

| Area | Tools |
|---|---|
| Channel/videos | `channel_info`, `list_videos`, `get_video`, `update_video`, `set_thumbnail`, `upload_video`, `delete_video` |
| Playlists | `list_playlists`, `create_playlist`, `delete_playlist`, `add_to_playlist`, `remove_from_playlist`, `list_playlist_items` |
| Comments | `list_comments`, `post_video_question`, `reply_to_comment`, `moderate_comment`, `mark_spam`, `delete_comment` |
| Live | `list_broadcasts`, `create_broadcast`, `list_streams`, `bind_broadcast`, `live_chat_messages`, `post_chat_message`, `delete_chat_message`, `ban_chat_user` |
| Captions | `list_captions`, `upload_caption`, `download_caption` |
| Analytics | `channel_report`, `video_report`, `top_videos` |
| Giveaways | `collect_entries`, `draw_winners`, `make_verification_code`, `check_verification_reply`, `post_winner_reply` |
| Twitch | `twitch_status`, `twitch_get_channel`, `twitch_set_channel` |
| Meta | `quota_status`, `health_check` |

Known API limitations (documented, not bugs): comments cannot be pinned via
the API (`post_video_question` reminds you to pin manually in Studio), and
Community-tab posts have no public API.

### Multistreaming to Twitch

Multistream plugins (Aitum, Restream) mirror the **video** to Twitch but not the
**metadata** — Twitch keeps whatever title and category were set last, so a new
stream goes out under the previous stream's name. `create_broadcast` therefore
sets the Twitch title/category too (`twitch=True` by default, `twitch_game`
strongly recommended); pass `twitch=False` to skip it. A Twitch failure never
fails the YouTube broadcast — it is reported in the result.

One-time setup:

1. Create an application at <https://dev.twitch.tv/console/apps> with the OAuth
   redirect URL set to exactly `http://localhost:8271`.
2. Run, as the channel owner:

   ```bash
   yt-studio-mcp twitch-auth --client-id <id> --client-secret <secret>
   ```

   On a **headless** box the local listener cannot catch the redirect -- it goes
   to the *browser's* localhost, not the server's. Do this instead:

   ```bash
   yt-studio-mcp twitch-auth --client-id <id> --client-secret <secret> --print-url
   # open that URL anywhere, let the redirect fail, copy ?code=... from the bar
   yt-studio-mcp twitch-auth --client-id <id> --client-secret <secret> --code <code>
   ```

   Authorization codes expire in minutes -- paste promptly.

This stores `twitch_client_id`, `twitch_client_secret` and
`twitch_refresh_token` in the configured secret backend. The grant needs the
`channel:manage:broadcast` scope — an app (client-credentials) token cannot
modify a channel. Twitch rotates refresh tokens, and the rotated value is
persisted automatically (except on the read-only `env` backend).

## Running a giveaway

The giveaway suite implements a common official-rules pattern: *comment on
any video during the entry window; each distinct video counts as one entry,
capped per person; winners drawn at random.*

```text
1. collect_entries(start="2026-01-01T00:00:00Z", end="2026-01-31T23:59:59Z",
                   max_entries_per_user=5, exclude_channel_ids=[...])
     → walks every upload's comments, derives entries, writes a snapshot
       file and returns its SHA-256 entry hash. Announce the hash BEFORE the
       seed exists if you want third-party verifiability. Timestamps need an
       offset (Z or -05:00). If any video had more comments than
       comment_limit_per_video the snapshot is marked incomplete and cannot
       be drawn without allow_incomplete=True; videos with comments disabled
       are skipped and listed.

2. draw_winners(snapshot_path, n=5, seed="any-public-string")
     → deterministic (algo v2): each entry is ranked by
       sha256("v2|seed|entry_hash|channel_id|video_id|comment_id"); the n
       lowest-ranked distinct channels win. Anyone with the snapshot + seed
       can recompute it with any sha256 tool. Every draw is appended to a
       hash-chained <snapshot>-draws.jsonl; drawing again with a different
       seed requires a recorded redraw_reason. Use a seed nobody controls
       (e.g. a randomness-beacon round announced in advance).

3. make_verification_code(audit_path, winner_channel_id)
     → short code you send to the claimed winner over your announced contact
       channel. Audit files are re-derived from the snapshot before use, so
       an edited winner list is rejected.

4. check_verification_reply(audit_path, comment_id, winner_channel_id)
     → confirms the reply was authored by the winning channel and contains
       the code — proves account ownership before shipping a prize.

5. post_winner_reply(comment_id, "Congratulations! ...")
```

Entries and audit records live in `<config dir>/giveaways/` (default
`~/.config/yt-studio-mcp/giveaways/`; see *Multiple channels*).

*This tool automates entry collection and drawing; giveaway laws vary by
jurisdiction and platform policies apply (e.g. YouTube's contest policies).
You are responsible for your own official rules and compliance.*

## Multiple channels

One server instance manages one channel. To manage several, run **one instance
per channel**, each with its own state directory:

| Variable | Default | Holds |
|---|---|---|
| `YT_MCP_CONFIG_DIR` | `~/.config/yt-studio-mcp` | credentials (file backend), banned words + scan watermark, scan history, live status, giveaway snapshots, question-comment tracking, archived thumbnails |
| `YT_MCP_CREDENTIALS_FILE` | `<config dir>/credentials.json` | the file-backend credentials only |
| `YT_MCP_VAULTPROXY_PREFIX` | `yt-studio-mcp` | vault item namespace (vaultproxy backend only) |
| `YT_MCP_EXPECTED_CHANNEL_ID` | unset | when set, the first API use verifies the stored token belongs to this channel and every tool refuses to act otherwise (recommended whenever several instances share one OAuth client) |
| `YT_MCP_QUOTA_LEDGER` | `<config dir>/quota_ledger.json` | per-day quota ledger; point instances that share one Google Cloud project at the same file |

Nothing is shared between directories, so instances never see each other's
tokens, wordlists or giveaway files. Leaving the variables unset keeps the
original single-channel layout.

```bash
# authorize the second channel into its own directory
YT_MCP_CONFIG_DIR=~/.config/yt-studio-mcp-second \
  uvx yt-studio-mcp auth --client-secret ~/.config/yt-studio-mcp-second/client_secret.json

# register it alongside the first
claude mcp add yt-second -s user -e YT_MCP_CONFIG_DIR=$HOME/.config/yt-studio-mcp-second \
  -- uvx yt-studio-mcp
```

The same Google OAuth client can authorize any number of channels; in the
consent screen pick the channel (Brand Accounts are listed separately). `auth`
records which channel it authorized and refuses to replace another channel's
credentials in the same directory unless `--force` is given. For a headless box,
`auth --port 59067 --no-browser --timeout 1200` and an `ssh -L` tunnel to that port. The
headless `scan`, `collect` and `watch` commands honour the same variable.

## Headless spam scan

`yt-studio-mcp scan --incremental --auto-action heldForReview [--llm]` fails
closed: a missing, empty or corrupt word list or watermark is an error, and the
watermark advances only when every new comment was read, classified and
moderated. Exit codes: 0 clean, 2 matches, 4 incomplete (moderation or LLM
failure, or the limit cut the listing short; watermark held), 5 another scan
holds the lock, 1 error. `watch --auto-action none` observes only.

## Secret storage

| Backend | Select with | Stores |
|---|---|---|
| `file` (default) | — | `<config dir>/credentials.json` (default `~/.config/yt-studio-mcp/`), chmod 0600; `YT_MCP_CREDENTIALS_FILE` overrides |
| `env` | `YT_MCP_SECRETS=env` | reads `YT_MCP_REFRESH_TOKEN`, `YT_MCP_CLIENT_ID`, `YT_MCP_CLIENT_SECRET` |
| `vaultproxy` | `YT_MCP_SECRETS=vaultproxy` (+ `VAULTPROXY_URL`) | items `<prefix>/<key>` in a Vaultwarden vault via a local vaultproxy HTTP API; prefix `YT_MCP_VAULTPROXY_PREFIX` (default `yt-studio-mcp`) |

## Quota

The Data API's default allocation is 10,000 units/day. Most reads cost 1
unit; writes cost ~50; a video upload costs 1,600. `quota_status()` shows the
session spend and today's spend from a persistent ledger keyed on the Pacific
day (when the quota resets) and warns at 80%. `collect_entries` reports what it spent.

## Development

```bash
pip install -e '.[dev]'
ruff check src tests && pytest
```

Tests are fully offline (mocked Google API). PRs welcome.

## License

MIT
