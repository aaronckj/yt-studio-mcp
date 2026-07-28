"""Server meta tools: quota and health."""

from __future__ import annotations

from ..client import get_yt

DAILY_QUOTA_DEFAULT = 10_000
WARN_FRACTION = 0.8


def register(mcp) -> None:
    @mcp.tool()
    def quota_status() -> dict:
        """Quota units spent this session (local estimate from the official cost table)."""
        yt = get_yt()
        spent = yt.quota.spent
        return {
            "session_units_spent": spent,
            "session_calls": yt.quota.calls,
            "daily_allocation": DAILY_QUOTA_DEFAULT,
            "warning": spent >= DAILY_QUOTA_DEFAULT * WARN_FRACTION,
            "note": "estimate; authoritative usage is in the Google Cloud console",
        }

    @mcp.tool()
    def health_check() -> dict:
        """Verify stored credentials refresh and the channel is reachable."""
        from ..auth import AuthError, get_credentials
        from ..secrets import get_store

        out: dict = {"secrets_backend": type(get_store()).__name__}
        try:
            get_credentials()
            out["auth"] = "ok"
        except AuthError as exc:
            out["auth"] = f"error: {exc}"
            return out
        try:
            yt = get_yt()
            res = yt.call(yt.data.channels().list(part="snippet", mine=True), op="list")
            out["channel"] = res["items"][0]["snippet"]["title"]
            out["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 - health check reports, never raises
            out["status"] = f"error: {exc}"
        return out
