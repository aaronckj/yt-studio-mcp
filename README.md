# yt-studio-mcp

MCP server for managing a YouTube channel through the official Google APIs:
videos, comments and moderation, playlists, live broadcasts, captions,
analytics — plus an auditable giveaway suite for comment-entry giveaways
(windowed entry collection, per-user caps, deterministic seeded drawing,
winner verification).

> Full docs land with v0.1.0. Quick start below.

## Install

```bash
uvx yt-studio-mcp            # run the server
```

## Setup

1. Create a Google Cloud project, enable **YouTube Data API v3** and
   **YouTube Analytics API**, create an **OAuth client (Desktop app)**, and
   download `client_secret.json`.
2. Authorize once (opens a browser — pick your channel, including Brand
   Accounts):

```bash
yt-studio-mcp auth --client-secret /path/to/client_secret.json
```

3. Add to your MCP client, e.g. Claude Code:

```bash
claude mcp add yt-studio -s user -- uvx yt-studio-mcp
```

## Secret storage

| Backend | Select with | Stores |
|---|---|---|
| `file` (default) | — | `~/.config/yt-studio-mcp/credentials.json` (0600) |
| `env` | `YT_MCP_SECRETS=env` | read from `YT_MCP_*` env vars |
| `vaultproxy` | `YT_MCP_SECRETS=vaultproxy` | Vaultwarden items via a local vaultproxy |

No secrets are ever stored in this repository or logged at runtime.

## License

MIT
