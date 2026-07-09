from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.agent.models import TurnRequest
from app.services.agent.research_utils import temporal_context


def test_temporal_context_output_format():
    ctx = temporal_context()

    assert set(ctx) == {"current_date", "current_datetime_iso"}
    parts = ctx["current_date"].split(", ")
    assert len(parts) == 3  # "Weekday", "Month DD", "YYYY"
    assert parts[2].isdigit() and len(parts[2]) == 4
    # ISO string round-trips.
    datetime.fromisoformat(ctx["current_datetime_iso"])


def test_temporal_context_respects_explicit_timezone():
    ctx = temporal_context("Asia/Tokyo")
    expected_date = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%A, %B %d, %Y")

    assert ctx["current_date"] == expected_date


def test_temporal_context_falls_back_to_america_new_york_when_no_tz():
    ctx = temporal_context(None)
    expected_date = datetime.now(ZoneInfo("America/New_York")).strftime("%A, %B %d, %Y")

    assert ctx["current_date"] == expected_date


def test_temporal_context_falls_back_on_invalid_tz():
    ctx = temporal_context("Not/A_Real_Zone")
    expected_date = datetime.now(ZoneInfo("America/New_York")).strftime("%A, %B %d, %Y")

    assert ctx["current_date"] == expected_date


def test_turn_request_accepts_valid_iana_timezone():
    request = TurnRequest(message="hi", user_timezone="America/Los_Angeles")

    assert request.user_timezone == "America/Los_Angeles"


def test_turn_request_invalid_timezone_falls_back_to_none():
    request = TurnRequest(message="hi", user_timezone="Not/A_Real_Zone")

    assert request.user_timezone is None


def test_turn_request_empty_timezone_stays_none():
    request = TurnRequest(message="hi", user_timezone="")

    assert request.user_timezone is None


def test_turn_request_default_timezone_is_none():
    request = TurnRequest(message="hi")

    assert request.user_timezone is None
