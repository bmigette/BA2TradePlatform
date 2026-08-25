"""FMPCompanyDetailsProvider.get_past_earnings error handling.

Regression coverage for the 2026-08-24 incident: FMP quota exhaustion (HTTP 429) made
FMPRating fail loud (as designed) but FMPEarningsDrift silently reported "no earnings
data" -> a false HOLD, because this function's outer ``except Exception`` swallowed the
FMPError raised by the retried fetch and returned an ``{"error": ...}`` dict instead of
raising. Callers that only check for the "earnings" key (FMPEarningsDrift._gather among
them) can't tell that apart from a symbol that genuinely has no earnings in the window.

Two behaviours are proven:
  - A genuine FMP failure (FMPError, e.g. rate-limit/quota) PROPAGATES -- the caller's job
    fails loud, exactly like every other FMP-backed fetch in this provider.
  - A non-FMP bug (a defect in this function's own row-processing) is still swallowed into
    the best-effort {"error": ...} dict, unchanged from before -- this function must not
    become fragile to its own bugs, only stop masking upstream FMP failures.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ba2_providers.fmp_common import FMPError
from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import (
    FMPCompanyDetailsProvider,
)


def _provider():
    p = FMPCompanyDetailsProvider.__new__(FMPCompanyDetailsProvider)
    p.api_key = "fake-key"
    return p


def test_fmp_error_propagates_instead_of_being_swallowed():
    """Rate-limit/quota exhaustion must fail the caller, not degrade to 'no data'."""
    with patch(
        "ba2_providers.fundamentals.details.FMPCompanyDetailsProvider.fmp_history_disk_cached",
        side_effect=FMPError("FMP historical_earning_calendar error for AAPL after 4 attempts"),
    ):
        with pytest.raises(FMPError):
            _provider().get_past_earnings(
                "AAPL", frequency="quarterly",
                end_date=datetime(2026, 6, 13, tzinfo=timezone.utc),
                lookback_periods=1, format_type="dict",
            )


def test_non_fmp_bug_still_returns_best_effort_error_dict():
    """A defect unrelated to FMP (e.g. a malformed row) keeps the old best-effort contract:
    swallowed, logged, and returned as an {"error": ...} dict rather than crashing the run."""
    with patch(
        "ba2_providers.fundamentals.details.FMPCompanyDetailsProvider.fmp_history_disk_cached",
        return_value=object(),  # not iterable -> TypeError inside the processing loop
    ):
        result = _provider().get_past_earnings(
            "AAPL", frequency="quarterly",
            end_date=datetime(2026, 6, 13, tzinfo=timezone.utc),
            lookback_periods=1, format_type="dict",
        )
    assert result["error"]
    assert result["symbol"] == "AAPL"
