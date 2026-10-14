"""Offline tests for the FXMacroData calendar connector."""

import io
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from pyeventbt.data_provider.connectors import fxmacrodata_calendar_connector as fxmd
from pyeventbt.data_provider.connectors.fxmacrodata_calendar_connector import (
    FXMacroDataCalendarConnector,
    FXMacroDataError,
)

ROWS = [
    {"release": "non_farm_payrolls", "announcement_datetime_utc": "2026-10-09T12:30:00+00:00", "market_tier": 1},
    {"release": "trade_balance", "announcement_datetime_utc": "2026-10-06T12:30:00+00:00", "market_tier": 2},
    {"release": "bad_time", "announcement_datetime_utc": "not a date", "market_tier": 1},
    "junk",
]


class _FakeOpener:
    def __init__(self, body):
        self.body = body
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return io.BytesIO(raw)


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.delenv("FXMD_API_KEY", raising=False)
    monkeypatch.delenv("FXMACRODATA_API_KEY", raising=False)


def _install(monkeypatch, body):
    opener = _FakeOpener(body)
    monkeypatch.setattr(fxmd, "_OPENER", opener)
    return opener


def test_key_goes_in_header_not_url(monkeypatch):
    opener = _install(monkeypatch, {"data": ROWS})
    FXMacroDataCalendarConnector(api_key="test-key").fetch_calendar("USD", start_date="2026-10-01")

    request = opener.requests[0]
    assert request.get_header("X-api-key") == "test-key"
    assert "api_key" not in request.full_url
    assert request.full_url == "https://api.fxmacrodata.com/v1/calendar/usd?start_date=2026-10-01"


def test_env_fallback_and_strip(monkeypatch):
    monkeypatch.setenv("FXMACRODATA_API_KEY", "env-key\n")
    assert FXMacroDataCalendarConnector().api_key == "env-key"


@pytest.mark.parametrize("bad", [" secret key", "secret\nkey", "sec ret"])
def test_malformed_key_is_not_echoed(bad):
    with pytest.raises(FXMacroDataError) as exc:
        FXMacroDataCalendarConnector(api_key=bad)
    assert "secret" not in str(exc.value)


def test_top_tier_filter_and_bad_rows(monkeypatch):
    _install(monkeypatch, {"data": ROWS})
    connector = FXMacroDataCalendarConnector()
    rows = connector.fetch_calendar(top_tier_only=True)
    assert [row["release"] for row in rows] == ["non_farm_payrolls", "bad_time"]

    events = connector.next_events(now=datetime(2026, 10, 5, tzinfo=timezone.utc))
    assert [row["release"] for row in events] == ["non_farm_payrolls"]


def test_error_body_with_200(monkeypatch):
    _install(monkeypatch, {"detail": "Unsupported currency"})
    with pytest.raises(FXMacroDataError, match="Unsupported currency"):
        FXMacroDataCalendarConnector().fetch_calendar("xyz")


@pytest.mark.parametrize("body", [b"<html>", ["a", "list"], {"data": "nope"}])
def test_malformed_payload(monkeypatch, body):
    _install(monkeypatch, body)
    with pytest.raises(FXMacroDataError):
        FXMacroDataCalendarConnector().fetch_calendar()


def test_redirects_are_not_followed():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/start"
        with pytest.raises(HTTPError) as exc:
            fxmd._OPENER.open(Request(url, headers={"X-API-Key": "secret"}), timeout=5)
    finally:
        server.shutdown()

    assert exc.value.code == 302
    assert "secret" not in str(exc.value)
    assert hits == ["/start"]
