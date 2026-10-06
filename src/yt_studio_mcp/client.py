"""Google API client wrapper: quota accounting, error mapping, logging."""

from __future__ import annotations

import json
import logging
import os
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
    "invalidPublishAt": (
        "The schedule time is invalid or already past. To publish now pass "
        "privacy='public' (the stale schedule is cleared automatically); to "
        "unschedule pass publish_at=''."
    ),
}


EXPECTED_CHANNEL_ENV = "YT_MCP_EXPECTED_CHANNEL_ID"


class ChannelMismatch(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, reason: str, status: int, detail: str):
        self.reason = reason
        self.status = status
        self.hint = HINTS.get(reason, "")
        super().__init__(f"{reason} (HTTP {status}): {detail} {self.hint}".strip())


def op_cost(op: str) -> int:
    return COSTS.get(op, COSTS.get(op.split(".")[-1], 1))


def map_http_error(exc: HttpError, op: str) -> ApiError:
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
    return ApiError(reason, exc.resp.status if exc.resp else 0, detail)


QUOTA_LEDGER_ENV = "YT_MCP_QUOTA_LEDGER"


def quota_day() -> str:
    """YouTube quota resets at midnight Pacific; the ledger keys on that day."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def ledger_path():
    from pathlib import Path

    from .paths import config_dir

    value = os.environ.get(QUOTA_LEDGER_ENV, "").strip()
    return Path(value).expanduser() if value else config_dir() / "quota_ledger.json"


class QuotaTracker:
    """Per-process counter plus a persistent per-Pacific-day ledger.

    The session counter alone reset on every restart and was compared against
    the DAILY allocation. The ledger survives restarts; point several
    instances that share one Google Cloud project (and so one 10k/day quota)
    at the same file with YT_MCP_QUOTA_LEDGER to see their combined spend.
    """

    def __init__(self) -> None:
        self.spent = 0
        self.calls = 0

    def add(self, cost: int) -> None:
        self.spent += cost
        self.calls += 1
        self._record(cost)

    @staticmethod
    def _record(cost: int) -> None:
        try:
            import fcntl

            path = ledger_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a+") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                fh.seek(0)
                raw = fh.read()
                try:
                    data = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    data = {}
                day = quota_day()
                if data.get("day") != day:
                    data = {"day": day, "units": 0, "calls": 0}
                data["units"] = int(data.get("units", 0)) + cost
                data["calls"] = int(data.get("calls", 0)) + 1
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps(data))
        except Exception:  # noqa: BLE001 - accounting must never fail a call
            logger.exception("quota ledger update failed")

    @staticmethod
    def today() -> dict:
        try:
            data = json.loads(ledger_path().read_text())
        except Exception:  # noqa: BLE001
            return {"day": quota_day(), "units": 0, "calls": 0}
        if data.get("day") != quota_day():
            return {"day": quota_day(), "units": 0, "calls": 0}
        return data


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

            service = build(
                "youtube", "v3", credentials=get_credentials(), cache_discovery=False
            )
            self.check_channel(service)
            self._data = service
        return self._data

    def check_channel(self, service: Any) -> None:
        """Refuse to operate on the wrong channel.

        Several instances (one per channel) share one OAuth client, and a
        re-auth run without the right YT_MCP_CONFIG_DIR silently swaps which
        channel a config dir's token belongs to. With YT_MCP_EXPECTED_CHANNEL_ID
        set, the first API use verifies the token's channel (1 quota unit per
        process) and every tool fails closed on a mismatch.
        """
        expected = os.environ.get(EXPECTED_CHANNEL_ENV, "").strip()
        if not expected:
            return
        res = self.call(service.channels().list(part="id", mine=True), op="list")
        actual = (res.get("items") or [{}])[0].get("id")
        if actual != expected:
            raise ChannelMismatch(
                f"stored credentials belong to channel {actual!r}, but this instance "
                f"expects {expected!r} ({EXPECTED_CHANNEL_ENV}). Refusing to act. "
                "Re-run `yt-studio-mcp auth` with the right YT_MCP_CONFIG_DIR."
            )

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
        cost = op_cost(op)
        started = time.monotonic()
        try:
            result = request.execute()
            self.quota.add(cost)
            logger.info("api op=%s cost=%s ms=%d", op, cost, (time.monotonic() - started) * 1000)
            return result if result is not None else {}
        except HttpError as exc:
            # A rejected call still burns its quota cost.
            self.quota.add(cost)
            raise map_http_error(exc, op) from exc

    def upload(self, request: Any, op: str = "videos.insert") -> dict:
        """Drive a resumable upload to completion with the same accounting as call()."""
        cost = op_cost(op)
        try:
            response = None
            while response is None:
                _, response = request.next_chunk()
        except HttpError as exc:
            self.quota.add(cost)
            raise map_http_error(exc, op) from exc
        self.quota.add(cost)
        logger.info("api op=%s cost=%s (resumable)", op, cost)
        return response

    def paginate(
        self, resource: Any, op: str, limit: int, page_size: int = 50, **params
    ) -> list[dict]:
        """Collect up to `limit` items across pages of a .list endpoint."""
        return self.paginate_more(resource, op, limit, page_size=page_size, **params)[0]

    def paginate_more(
        self, resource: Any, op: str, limit: int, page_size: int = 50, **params
    ) -> tuple[list[dict], bool]:
        """Like paginate(), but also say whether items were LEFT BEHIND.

        Callers that must be complete (giveaway entries, moderation sweeps)
        need to know the difference between "that was everything" and "the
        limit cut it off" -- silently dropping the tail looks identical.
        """
        items: list[dict] = []
        token = None
        more = False
        while len(items) < limit:
            page = self.call(
                resource.list(
                    **params, maxResults=min(page_size, limit - len(items)), pageToken=token
                ),
                op=op,
            )
            items.extend(page.get("items", []))
            token = page.get("nextPageToken")
            if not token:
                break
        else:
            more = bool(token)
        if len(items) > limit:
            more = True
        return items[:limit], more


def preview(action: str, would: dict) -> dict:
    return {"preview": True, "action": action, "would": would}


_yt: YT | None = None


def get_yt() -> YT:
    global _yt
    if _yt is None:
        _yt = YT()
    return _yt
