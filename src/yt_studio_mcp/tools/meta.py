"""Server meta tools: quota and health."""

from __future__ import annotations

from mcp.types import ToolAnnotations

from ..client import get_yt

DAILY_QUOTA_DEFAULT = 10_000
WARN_FRACTION = 0.8


def register(mcp) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def quota_status() -> dict:
        """Quota units spent this session and today (Pacific day, persisted
        across restarts; local estimate from the official cost table)."""
        from ..client import ledger_path

        yt = get_yt()
        today = yt.quota.today()
        return {
            "session_units_spent": yt.quota.spent,
            "session_calls": yt.quota.calls,
            "today_units_spent": today["units"],
            "quota_day": today["day"],
            "ledger": str(ledger_path()),
            "daily_allocation": DAILY_QUOTA_DEFAULT,
            "warning": today["units"] >= DAILY_QUOTA_DEFAULT * WARN_FRACTION,
            "note": "estimate counted by THIS ledger only; instances sharing a "
                    "Google Cloud project should share YT_MCP_QUOTA_LEDGER. "
                    "Authoritative usage is in the Google Cloud console.",
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
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
            out["channel_id"] = res["items"][0]["id"]
            out["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 - health check reports, never raises
            out["status"] = f"error: {exc}"
        return out
