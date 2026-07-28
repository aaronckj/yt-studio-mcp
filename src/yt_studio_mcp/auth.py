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


def run_auth_flow(client_secret_path: str, store: SecretStore | None = None) -> str:
    """Interactive browser consent. Stores refresh token + client info. Returns channel hint."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    store = store or get_store()
    path = Path(client_secret_path)
    if not path.exists():
        raise AuthError(f"client secret file not found: {path}")
    flow = InstalledAppFlow.from_client_secrets_file(str(path), scopes=SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    if not creds.refresh_token:
        raise AuthError("no refresh token returned; remove prior grants and retry")
    conf = json.loads(path.read_text())
    client = conf.get("installed") or conf.get("web") or {}
    store.set("refresh_token", creds.refresh_token)
    store.set("client_id", client.get("client_id", creds.client_id or ""))
    store.set("client_secret", client.get("client_secret", creds.client_secret or ""))
    return "authorized; credentials stored"


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
