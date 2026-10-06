"""OAuth: one-time interactive flow plus credential rebuild/refresh."""

from __future__ import annotations

import json
from pathlib import Path

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials

from .secrets import SecretStore, get_store

SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"


class AuthError(RuntimeError):
    pass


def _channel_of(creds: Credentials) -> tuple[str, str]:
    """(channel id, title) the freshly granted token acts as."""
    from googleapiclient.discovery import build

    svc = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = svc.channels().list(part="snippet", mine=True).execute().get("items") or []
    if not items:
        raise AuthError("the authorized Google account has no YouTube channel")
    return items[0]["id"], items[0]["snippet"]["title"]


def run_auth_flow(
    client_secret_path: str,
    store: SecretStore | None = None,
    *,
    port: int = 0,
    open_browser: bool = True,
    timeout_seconds: int | None = None,
    force: bool = False,
    flow_factory=None,
) -> str:
    """Interactive browser consent. Stores refresh token + client info + the
    channel it belongs to. Refuses to replace credentials of a DIFFERENT
    channel (a re-auth without the right YT_MCP_CONFIG_DIR) unless force."""
    from .paths import config_dir

    store = store or get_store()
    path = Path(client_secret_path).expanduser()
    if not path.exists():
        raise AuthError(f"client secret file not found: {path}")
    if flow_factory is None:
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow_factory = InstalledAppFlow.from_client_secrets_file
    flow = flow_factory(str(path), scopes=SCOPES)
    kwargs: dict = {"port": port, "prompt": "consent", "open_browser": open_browser}
    if timeout_seconds:
        kwargs["timeout_seconds"] = timeout_seconds
    creds = flow.run_local_server(**kwargs)
    if not creds.refresh_token:
        raise AuthError("no refresh token returned; remove prior grants and retry")
    channel_id, title = _channel_of(creds)
    previous = store.get("channel_id")
    if previous and previous != channel_id and not force:
        raise AuthError(
            f"{config_dir()} holds credentials for channel {previous}, but you just "
            f"authorized {title} ({channel_id}). Nothing was written. Point "
            "YT_MCP_CONFIG_DIR at that channel's directory, or pass --force to replace."
        )
    conf = json.loads(path.read_text())
    client = conf.get("installed") or conf.get("web") or {}
    store.set("refresh_token", creds.refresh_token)
    store.set("client_id", client.get("client_id", creds.client_id or ""))
    store.set("client_secret", client.get("client_secret", creds.client_secret or ""))
    store.set("channel_id", channel_id)
    store.set("channel_title", title)
    return (f"authorized as {title} ({channel_id}); credentials stored for "
            f"{config_dir()}. Set YT_MCP_EXPECTED_CHANNEL_ID={channel_id} on this "
            "instance to make it fail closed if the token ever changes channel.")


def _refresh(creds: Credentials) -> None:
    creds.refresh(GoogleRequest())


def get_credentials(store: SecretStore | None = None) -> Credentials:
    """Rebuild Credentials from the secret store and refresh the access token."""
    store = store or get_store()
    refresh_token = store.get("refresh_token")
    client_id = store.get("client_id")
    client_secret = store.get("client_secret")
    if not (refresh_token and client_id and client_secret):
        raise AuthError(
            "no stored credentials; run `yt-studio-mcp auth --client-secret <path>` first"
        )
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )
    try:
        _refresh(creds)
    except Exception as exc:
        raise AuthError(
            f"token refresh failed ({exc}); re-run `yt-studio-mcp auth`"
        ) from exc
    return creds
