"""Google API client wrapper: quota accounting, error mapping, logging."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from googleapiclient.errors import HttpError

logger = logging.getLogger("yt_studio_mcp")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Data API v3 quota costs (units) by operation family.
COSTS = {
    "list": 1,
    "insert": 50,
    "update": 50,
    "delete": 50,
    "setModerationStatus": 50,
    "markAsSpam": 50,
    "thumbnails.set": 50,
    "videos.insert": 1600,
    "captions.insert": 400,
    "captions.update": 450,
    "captions.download": 200,
    "analytics.query": 0,
}

HINTS = {
    "quotaExceeded": "Daily quota exhausted; resets midnight Pacific. Check quota_status().",
    "forbidden": "Check the authorized channel and OAuth scopes; re-run `yt-studio-mcp auth`.",
    "notFound": "The referenced resource id does not exist or is not visible to this channel.",
    "authError": "Re-run `yt-studio-mcp auth`.",
    "commentsDisabled": "Comments are disabled on that video.",
}


class ApiError(RuntimeError):
    def __init__(self, reason: str, status: int, detail: str):
        self.reason = reason
        self.status = status
        self.hint = HINTS.get(reason, "")
        super().__init__(f"{reason} (HTTP {status}): {detail} {self.hint}".strip())


class QuotaTracker:
    def __init__(self) -> None:
        self.spent = 0
        self.calls = 0

    def add(self, cost: int) -> None:
        self.spent += cost
        self.calls += 1


class YT:
    """Lazy holder for the Data v3 and Analytics v2 services."""

    def __init__(self, data_service: Any = None, analytics_service: Any = None):
        self._data = data_service
        self._analytics = analytics_service
        self.quota = QuotaTracker()

    @property
    def data(self) -> Any:
        if self._data is None:
            from googleapiclient.discovery import build

            from .auth import get_credentials

            self._data = build(
                "youtube", "v3", credentials=get_credentials(), cache_discovery=False
            )
        return self._data

    @property
    def analytics(self) -> Any:
        if self._analytics is None:
            from googleapiclient.discovery import build

            from .auth import get_credentials

            self._analytics = build(
                "youtubeAnalytics", "v2", credentials=get_credentials(), cache_discovery=False
            )
        return self._analytics

    def call(self, request: Any, op: str = "list") -> dict:
        """Execute a prepared API request with quota accounting and error mapping."""
        cost = COSTS.get(op, COSTS.get(op.split(".")[-1], 1))
        started = time.monotonic()
        try:
            result = request.execute()
            self.quota.add(cost)
            logger.info("api op=%s cost=%s ms=%d", op, cost, (time.monotonic() - started) * 1000)
            return result if result is not None else {}
        except HttpError as exc:
            self.quota.add(cost)
            reason = "unknown"
            detail = str(exc)
            try:
                body = json.loads(exc.content.decode())
                err = body.get("error", {})
                detail = err.get("message", detail)
                errors = err.get("errors") or [{}]
                reason = errors[0].get("reason", err.get("status", "unknown"))
            except Exception:  # noqa: BLE001 - best-effort body parse
                pass
            logger.error("api op=%s failed reason=%s detail=%s", op, reason, detail)
            raise ApiError(reason, exc.resp.status if exc.resp else 0, detail) from exc

    def paginate(self, resource: Any, op: str, limit: int, **params) -> list[dict]:
        """Collect up to `limit` items across pages of a .list endpoint."""
        items: list[dict] = []
        token = None
        while len(items) < limit:
            page = self.call(
                resource.list(**params, maxResults=min(50, limit - len(items)), pageToken=token),
                op=op,
            )
            items.extend(page.get("items", []))
            token = page.get("nextPageToken")
            if not token:
                break
        return items[:limit]


def preview(action: str, would: dict) -> dict:
    return {"preview": True, "action": action, "would": would}


_yt: YT | None = None


def get_yt() -> YT:
    global _yt
    if _yt is None:
        _yt = YT()
    return _yt
