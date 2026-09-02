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
       file and returns its SHA-256 entry hash. Announce the hash if you
       want third-party verifiability.

2. draw_winners(snapshot_path, n=5, seed="any-public-string")
     → deterministic: a seeded shuffle over the canonically sorted entries,
       first n distinct channels win. Anyone with the snapshot + seed can
       re-run the draw and get identical winners. Records an audit file.
       Screen-record this step for extra transparency.

3. make_verification_code(audit_path, winner_channel_id)
     → short code you send to the claimed winner over your announced contact
       channel.

4. check_verification_reply(audit_path, comment_id, winner_channel_id)
     → confirms the reply was authored by the winning channel and contains
       the code — proves account ownership before shipping a prize.

5. post_winner_reply(comment_id, "Congratulations! ...")
```

Entries and audit records live in `~/.config/yt-studio-mcp/giveaways/`.

*This tool automates entry collection and drawing; giveaway laws vary by
jurisdiction and platform policies apply (e.g. YouTube's contest policies).
You are responsible for your own official rules and compliance.*

## Secret storage

| Backend | Select with | Stores |
|---|---|---|
| `file` (default) | — | `~/.config/yt-studio-mcp/credentials.json`, chmod 0600 |
| `env` | `YT_MCP_SECRETS=env` | reads `YT_MCP_REFRESH_TOKEN`, `YT_MCP_CLIENT_ID`, `YT_MCP_CLIENT_SECRET` |
| `vaultproxy` | `YT_MCP_SECRETS=vaultproxy` (+ `VAULTPROXY_URL`) | items in a Vaultwarden vault via a local vaultproxy HTTP API |

## Quota

The Data API's default allocation is 10,000 units/day. Most reads cost 1
unit; writes cost ~50; a video upload costs 1,600. `quota_status()` shows a
session estimate and warns at 80%. `collect_entries` reports what it spent.

## Development

```bash
pip install -e '.[dev]'
ruff check src tests && pytest
```

Tests are fully offline (mocked Google API). PRs welcome.

## License

MIT
