"""FactorRanker must not read financial statements from the future.

`get_income_statement(sym, "annual", as_of, ...)` passes as_of into the THIRD
positional parameter, which is `end_date` -- not `as_of`. That left as_of=None,
which skips the provider's filing-date pre-pass; and because `lookback_periods`
short-circuits the end_date slicing too, the call returned the most recent annual
statement at EVERY historical bar. A 2023 backtest was scoring on FY2025
financials. Live was unaffected (there, as_of is genuinely "now").
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ba2_experts.FactorRanker import data as FR


class _RecordingDetails:
    """Captures how the statement methods are actually called."""

    def __init__(self):
        self.calls = []

    def _record(self, name, symbol, frequency, end_date, **kw):
        self.calls.append({"method": name, "symbol": symbol,
                           "end_date": end_date, "as_of": kw.get("as_of"),
                           "lookback_periods": kw.get("lookback_periods")})
        return {"statements": [{"fiscal_date_ending": "2025-09-27",
                                "net_income": 1.0, "total_revenue": 10.0,
                                "gross_profit": 4.0, "operating_income": 3.0,
                                "total_assets": 100.0,
                                "total_shareholder_equity": 50.0,
                                "total_liabilities": 50.0,
                                "operating_cash_flow": 5.0}]}

    def get_income_statement(self, symbol, frequency, end_date, **kw):
        return self._record("income", symbol, frequency, end_date, **kw)

    def get_balance_sheet(self, symbol, frequency, end_date, **kw):
        return self._record("balance", symbol, frequency, end_date, **kw)

    def get_cashflow_statement(self, symbol, frequency, end_date, **kw):
        return self._record("cashflow", symbol, frequency, end_date, **kw)


# `...details.FMPCompanyDetailsProvider` as an attribute path resolves to the
# CLASS (the package re-exports it), so the module has to be fetched explicitly.
_PROVIDER_MODULE = __import__(
    "ba2_providers.fundamentals.details.FMPCompanyDetailsProvider",
    fromlist=["FMPCompanyDetailsProvider"])


@pytest.fixture
def recording(monkeypatch):
    rec = _RecordingDetails()
    monkeypatch.setattr(_PROVIDER_MODULE, "FMPCompanyDetailsProvider",
                        lambda *a, **k: rec)
    return rec


AS_OF = datetime(2023, 6, 15, tzinfo=timezone.utc)


def test_value_inputs_pass_as_of_by_keyword(recording, monkeypatch):
    monkeypatch.setattr(FR, "_as_of_close", lambda *a, **k: 100.0)
    FR.fetch_value_inputs(["AAPL"], as_of=AS_OF, price_as_of={"AAPL": 100.0})
    assert recording.calls, "no statement call recorded"
    for call in recording.calls:
        assert call["as_of"] == AS_OF, (
            f"{call['method']}: as_of not forwarded -> the filing-date filter is "
            "skipped and the LATEST statement leaks into a historical bar")


def test_quality_inputs_pass_as_of_by_keyword(recording):
    FR.fetch_quality_inputs(["AAPL"], as_of=AS_OF)
    assert recording.calls, "no statement call recorded"
    for call in recording.calls:
        assert call["as_of"] == AS_OF, (
            f"{call['method']}: as_of not forwarded (lookahead)")


def test_statement_cache_depth_is_caller_independent():
    """The disk cache is keyed by (namespace, symbol) with NO depth component, so
    the fetch depth must not come from the caller's lookback_periods -- otherwise
    whoever warms it first (FactorRanker asks for 1) pins the history for every
    other expert."""
    import inspect

    M = _PROVIDER_MODULE
    # Strip comments so the explanatory prose about the old behaviour does not
    # count as the old behaviour.
    code = "\n".join(l.split("#")[0] for l in inspect.getsource(M).splitlines())
    assert code.count("limit=STATEMENT_HISTORY_DEPTH") == 3, \
        "income/balance/cashflow must all fetch at the fixed cache depth"
    assert "limit=lookback_periods" not in code, (
        "caller-dependent fetch depth poisons a depth-agnostic cache key")
    assert M.STATEMENT_HISTORY_DEPTH >= 6, (
        "Piotroski needs 2 fiscal years and growth acceleration needs 3+")
