"""
FXMacroData macro calendar connector.

This connector lets PyEventBT users fetch point-in-time macro release schedules
from FXMacroData and use them as event filters or features in event-driven
backtests. Set FXMD_API_KEY (or FXMACRODATA_API_KEY) for protected currencies
or endpoints. The key is sent in the X-API-Key header.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class FXMacroDataError(RuntimeError):
    """Raised when FXMacroData returns something the connector cannot use."""


class _NoRedirect(HTTPRedirectHandler):
    # urllib copies custom headers onto a redirected request, so following a
    # redirect would send X-API-Key to whatever host it names.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)


def _clean_api_key(api_key: str | None) -> str | None:
    if not api_key:
        return None
    api_key = api_key.strip()
    if not api_key or any(ch.isspace() or ord(ch) < 32 for ch in api_key):
        # http.client echoes an invalid header value in its error message.
        raise FXMacroDataError("FXMacroData API key is empty or contains whitespace or control characters")
    return api_key


class FXMacroDataCalendarConnector:
    base_url = "https://api.fxmacrodata.com/v1"

    def __init__(self, api_key: str | None = None, timeout: int = 20) -> None:
        if api_key is None:
            api_key = os.getenv("FXMD_API_KEY") or os.getenv("FXMACRODATA_API_KEY")
        self.api_key = _clean_api_key(api_key)
        self.timeout = timeout

    def fetch_calendar(
        self,
        currency: str = "usd",
        start_date: str | None = None,
        end_date: str | None = None,
        top_tier_only: bool = False,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        headers = {"Accept": "application/json", "User-Agent": "pyeventbt-fxmacrodata"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

        query = f"?{urlencode(params)}" if params else ""
        url = f"{self.base_url}/calendar/{currency.lower()}{query}"
        request = Request(url, headers=headers)
        with _OPENER.open(request, timeout=self.timeout) as response:
            try:
                payload = json.loads(response.read().decode("utf-8"))
            except ValueError:
                raise FXMacroDataError("FXMacroData returned a response that is not JSON") from None

        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            detail = payload.get("detail") if isinstance(payload, dict) else None
            message = "FXMacroData returned an unexpected response shape"
            if isinstance(detail, str):
                message += f": {detail}"
            raise FXMacroDataError(message)

        rows = [row for row in payload["data"] if isinstance(row, dict)]
        if top_tier_only:
            rows = [row for row in rows if row.get("top_tier_for_currency") or row.get("market_tier") == 1]
        return rows

    def has_event_between(
        self,
        start: datetime,
        end: datetime,
        currency: str = "usd",
        top_tier_only: bool = True,
    ) -> bool:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        rows = self.fetch_calendar(currency=currency, start_date=start.date().isoformat(), end_date=end.date().isoformat(), top_tier_only=top_tier_only)
        return any(start <= _parse_event_time(row) <= end for row in rows if _parse_event_time(row) is not None)

    def next_events(
        self,
        now: datetime | None = None,
        lookahead: timedelta = timedelta(days=7),
        currency: str = "usd",
        top_tier_only: bool = True,
    ) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        end = now + lookahead
        rows = self.fetch_calendar(currency=currency, start_date=now.date().isoformat(), end_date=end.date().isoformat(), top_tier_only=top_tier_only)
        return [row for row in rows if (event_time := _parse_event_time(row)) is not None and now <= event_time <= end]


def _parse_event_time(row: dict[str, Any]) -> datetime | None:
    value = row.get("announcement_datetime_utc") or row.get("announcement_datetime_local")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed

