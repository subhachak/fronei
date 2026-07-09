from __future__ import annotations

from app.services.agent.research_evidence import compute_staleness


def test_compute_staleness_unknown_published_date_is_not_penalised():
    assert compute_staleness(None, "high", "2026-07-09") == "unknown"


def test_compute_staleness_unknown_freshness_risk_is_unknown():
    assert compute_staleness("2026-06-01", "unknown", "2026-07-09") == "unknown"


def test_compute_staleness_low_risk_is_always_current():
    # Even a very old source is "rarely stale regardless of age" for low-risk claims.
    assert compute_staleness("2015-01-01", "low", "2026-07-09") == "current"


def test_compute_staleness_high_risk_becomes_stale_after_three_months():
    assert compute_staleness("2026-01-01", "high", "2026-07-09") == "stale"


def test_compute_staleness_high_risk_aging_between_one_and_three_months():
    assert compute_staleness("2026-05-20", "high", "2026-07-09") == "aging"


def test_compute_staleness_high_risk_current_within_one_month():
    assert compute_staleness("2026-07-01", "high", "2026-07-09") == "current"


def test_compute_staleness_medium_risk_becomes_stale_after_twelve_months():
    assert compute_staleness("2025-01-01", "medium", "2026-07-09") == "stale"


def test_compute_staleness_medium_risk_aging_between_six_and_twelve_months():
    assert compute_staleness("2025-11-01", "medium", "2026-07-09") == "aging"


def test_compute_staleness_medium_risk_current_within_six_months():
    assert compute_staleness("2026-06-01", "medium", "2026-07-09") == "current"


def test_compute_staleness_unparseable_dates_return_unknown():
    assert compute_staleness("not-a-date", "high", "2026-07-09") == "unknown"
    assert compute_staleness("2026-01-01", "high", "not-a-date") == "unknown"
