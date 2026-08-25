"""FactorRanker's thin fetchers (fetch_value_inputs / fetch_quality_inputs / fetch_pead_inputs)
must tell a genuine FMP outage apart from an expected per-symbol data gap.

Companion to test_fmp_past_earnings_error_propagation.py: this file was previously implicated
in the SAME failure mode as FMPEarningsDrift (a bare `except Exception` around every FMP fetch),
but its fix must NOT be DeterministicScorer's "propagate everything but OSError" policy --
FactorRanker's fetchers are explicitly designed to skip one bad symbol (thin small-cap missing
a statement, no analyst estimates yet) and keep scoring the rest of the universe. Blanket
propagation would turn one normal missing-data symbol into a dead universe.

The correct, narrower fix (applied in FactorRanker/data.py): only ``FMPError`` -- a genuine
rate-limit/quota failure, which hits every symbol identically -- propagates and aborts the
batch. Every other failure (empty statement, malformed shape, `_require_statement`'s ValueError)
keeps the existing per-symbol skip-and-continue behavior, unchanged.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ba2_experts.FactorRanker.data import (
    fetch_pead_inputs,
    fetch_quality_inputs,
    fetch_value_inputs,
)
from ba2_providers.fmp_common import FMPError

AS_OF = datetime(2026, 6, 13, tzinfo=timezone.utc)

_GOOD_INCOME = {"statements": [{"fiscal_date_ending": "2025-12-31", "eps": 5.0,
                                "net_income": 1e9, "gross_profit": 2e9,
                                "weighted_average_shares_outstanding": 1e8}]}
_GOOD_BALANCE = {"statements": [{"fiscal_date_ending": "2025-12-31",
                                "total_shareholder_equity": 4e9, "total_assets": 1e10,
                                "short_term_debt": 1e8, "long_term_debt": 2e8,
                                "cash_and_cash_equivalents": 5e8}]}
_GOOD_CASHFLOW = {"statements": [{"date": "2025-12-31", "operating_cash_flow": 8e8,
                                  "free_cash_flow": 6e8}]}
_GOOD_EARNINGS = {"earnings": [{"report_date": "2026-06-01", "reported_eps": 1.5,
                                "estimated_eps": 1.2}]}
_EMPTY = {"statements": []}


class FakeDetails:
    """Per-method behavior keyed on symbol: 'quota' raises FMPError, 'empty' returns a
    genuinely-empty (but well-shaped) result -- the normal "this symbol has no data" case."""

    def get_income_statement(self, symbol, frequency, end_date, lookback_periods=None,
                             as_of=None, format_type="dict"):
        if symbol == "QUOTA":
            raise FMPError("FMP income_statement error for QUOTA after 4 attempts (HTTP 429)")
        if symbol == "EMPTY":
            return _EMPTY
        return _GOOD_INCOME

    def get_balance_sheet(self, symbol, frequency, end_date, lookback_periods=None,
                          as_of=None, format_type="dict"):
        return _GOOD_BALANCE

    def get_cashflow_statement(self, symbol, frequency, end_date, lookback_periods=None,
                               as_of=None, format_type="dict"):
        return _GOOD_CASHFLOW

    def get_past_earnings(self, symbol, frequency, end_date, lookback_periods=1,
                          format_type="dict"):
        if symbol == "QUOTA":
            raise FMPError("FMP historical_earning_calendar error for QUOTA after 4 attempts")
        if symbol == "EMPTY":
            return {"earnings": []}
        return _GOOD_EARNINGS

    def get_earnings_estimates(self, symbol, frequency, end_date, lookback_periods=1,
                               format_type="dict"):
        return {"estimates": []}


def _patched(fn, *args, **kwargs):
    with patch(
        "ba2_providers.fundamentals.details.FMPCompanyDetailsProvider.FMPCompanyDetailsProvider",
        return_value=FakeDetails()):
        return fn(*args, **kwargs)


def test_fetch_value_inputs_propagates_fmp_error():
    with pytest.raises(FMPError):
        _patched(fetch_value_inputs, ["QUOTA"], as_of=AS_OF, price_as_of={"QUOTA": 100.0})


def test_fetch_value_inputs_skips_empty_symbol_and_keeps_the_batch():
    out = _patched(fetch_value_inputs, ["EMPTY", "AAPL"], as_of=AS_OF,
                   price_as_of={"EMPTY": 100.0, "AAPL": 200.0})
    assert "EMPTY" not in out
    assert "AAPL" in out


def test_fetch_quality_inputs_propagates_fmp_error():
    with pytest.raises(FMPError):
        _patched(fetch_quality_inputs, ["QUOTA"], as_of=AS_OF)


def test_fetch_quality_inputs_skips_empty_symbol_and_keeps_the_batch():
    out = _patched(fetch_quality_inputs, ["EMPTY", "AAPL"], as_of=AS_OF)
    assert "EMPTY" not in out
    assert "AAPL" in out


def test_fetch_pead_inputs_propagates_fmp_error():
    with pytest.raises(FMPError):
        _patched(fetch_pead_inputs, ["QUOTA"], as_of=AS_OF)


def test_fetch_pead_inputs_skips_empty_symbol_and_keeps_the_batch():
    out = _patched(fetch_pead_inputs, ["EMPTY", "AAPL"], as_of=AS_OF)
    assert "EMPTY" not in out
    assert "AAPL" in out
