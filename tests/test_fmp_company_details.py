"""FMPCompanyDetailsProvider: timezone-safe earnings date handling.

Regression for the live error 'can't compare offset-naive and offset-aware
datetimes' — FactorRanker's PEAD fetch passes a tz-aware as_of, while the provider
parses FMP dates as naive.
"""
from datetime import datetime, timezone
from unittest.mock import patch

from ba2_trade_platform.modules.dataproviders.fundamentals.details.FMPCompanyDetailsProvider import (
    FMPCompanyDetailsProvider,
)

MOD = "ba2_trade_platform.modules.dataproviders.fundamentals.details.FMPCompanyDetailsProvider"


def _provider():
    # Bypass __init__ (it requires FMP_API_KEY in app settings).
    p = FMPCompanyDetailsProvider.__new__(FMPCompanyDetailsProvider)
    p.api_key = "test"
    return p


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_get_past_earnings_accepts_aware_end_date():
    sample = [
        {"date": "2026-01-15", "eps": 1.2, "epsEstimated": 1.0},
        {"date": "2025-10-15", "eps": 0.9, "epsEstimated": 1.0},
        {"date": "2099-01-01", "eps": 5.0, "epsEstimated": 4.0},  # future -> filtered out
    ]
    aware = datetime(2026, 6, 2, tzinfo=timezone.utc)
    with patch(f"{MOD}.fmpsdk.historical_earning_calendar", return_value=sample):
        out = _provider().get_past_earnings("PEP", "quarterly", aware, lookback_periods=2, format_type="dict")
    assert "error" not in out, out
    dates = [e["report_date"] for e in out["earnings"]]
    assert "2099-01-01" not in dates           # future earnings excluded
    assert dates == ["2026-01-15", "2025-10-15"]  # most-recent-first, within window


def test_get_earnings_estimates_accepts_aware_as_of():
    sample = [
        {"date": "2026-09-30", "estimatedEpsAvg": 1.5, "estimatedEpsHigh": 1.8,
         "estimatedEpsLow": 1.2, "numberAnalystEstimatedEps": 10},
        {"date": "2025-01-01", "estimatedEpsAvg": 1.0, "estimatedEpsHigh": 1.1,
         "estimatedEpsLow": 0.9, "numberAnalystEstimatedEps": 8},  # past -> filtered out
    ]
    aware = datetime(2026, 6, 2, tzinfo=timezone.utc)
    with patch(f"{MOD}.requests.get", return_value=_Resp(sample)):
        out = _provider().get_earnings_estimates("PEP", "quarterly", aware, lookback_periods=4, format_type="dict")
    assert "error" not in out, out
    dates = [e["fiscal_date_ending"] for e in out["estimates"]]
    assert dates == ["2026-09-30"]  # only the future estimate
