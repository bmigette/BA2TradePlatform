"""DeterministicScorer.export_symbol_data: metric rows + signal tags.

Fixture note (adjusted from the plan sketch after reading the real
data.py/fundamental.py): the fundamentals_details provider methods are
``get_income_statement`` / ``get_balance_sheet`` / ``get_cashflow_statement``
(not ``get_income_statement``/``get_balance_sheet_statement``/
``get_cash_flow_statement`` as the sketch guessed), and each must return a
dict shaped ``{"statements": [...]}`` -- ``data.fetch_statements`` does
``stmts.get("statements", []) if isinstance(stmts, dict) else []``, so a bare
list return (the sketch's shape) would silently degrade to an empty section
rather than exercising the real code path. Returning empty statement lists
here is deliberate and still realistic: it drives the FUNDAMENTAL section to
its documented "no inputs -> score=None" branch (see
test_deterministic_scorer_regressions.py::test_missing_fundamentals_renormalize_instead_of_diluting),
which is a legitimate, common real-world case (a symbol with thin/absent FMP
fundamentals) and keeps this test focused on the export wiring rather than
re-deriving the fundamental calculators' own already-tested math.
"""
import pandas as pd

from ba2_experts.DeterministicScorer import DeterministicScorer
from ba2_experts.DeterministicScorer import data as _data

# Build a df with a clear-cut uptrend so technical score is unambiguously positive.
_CLOSES = [100.0 + i * 0.5 for i in range(280)]
_DF = pd.DataFrame({
    "Date": pd.date_range("2025-01-01", periods=280, freq="D"),
    "Open": _CLOSES, "High": [c + 1 for c in _CLOSES],
    "Low": [c - 1 for c in _CLOSES], "Close": _CLOSES,
    "Volume": [1_000_000] * 280,
})


class _FakeOHLCV:
    def get_ohlcv_data(self, symbol=None, start_date=None, end_date=None, interval="1d"):
        return _DF


class _FakeDetails:
    """Matches the REAL CompanyFundamentalsDetailsInterface call shape used by
    data.fetch_statements: get_income_statement/get_balance_sheet/
    get_cashflow_statement, each returning {"statements": [...]}."""
    def get_income_statement(self, **kwargs):
        return {"statements": []}

    def get_balance_sheet(self, **kwargs):
        return {"statements": []}

    def get_cashflow_statement(self, **kwargs):
        return {"statements": []}


def _resolver(cat, name, **kw):
    return {"ohlcv": _FakeOHLCV(), "fundamentals_details": _FakeDetails()}.get(cat)


def test_export_symbol_data_returns_technical_and_fundamental_rows():
    # data.py caches OHLCV/statements process-wide, keyed by symbol only (not by
    # test) -- reset so this test's fixture is what actually gets fetched.
    _data.reset_caches()
    result = DeterministicScorer.export_symbol_data("AAPL", providers_resolver=_resolver)
    assert result.error is None, result.error
    labels = {m.label for m in result.metrics}
    assert "Technical section" in labels
    assert "Fundamental section" in labels
    assert "Macro regime" in labels
    tech = next(m for m in result.metrics if m.label == "Technical section")
    assert tech.signal == "buy"   # clean uptrend fixture


def test_export_symbol_data_skip_surfaces_as_a_single_skipped_row():
    """min_history_days default is 260; a too-short OHLCV history makes
    _process return skip=True -- the override must defer to the base
    'Skipped' row rather than building a garbage technical/fundamental
    breakdown from an unprocessed bundle."""
    class _ShortOHLCV:
        def get_ohlcv_data(self, symbol=None, start_date=None, end_date=None, interval="1d"):
            return _DF.iloc[:50].reset_index(drop=True)

    def _short_resolver(cat, name, **kw):
        return {"ohlcv": _ShortOHLCV(), "fundamentals_details": _FakeDetails()}.get(cat)

    _data.reset_caches()
    result = DeterministicScorer.export_symbol_data("AAPL", providers_resolver=_short_resolver)
    assert result.error is None, result.error
    assert result.skipped is True
    assert len(result.metrics) == 1
    assert result.metrics[0].label == "Skipped"
    assert result.metrics[0].detail == "insufficient_history"


def test_export_symbol_data_a_single_statement_period_does_not_crash_on_none_fscore_or_z():
    """With only ONE annual statement, _build_fundamental still sets
    snapshot['fscore']/['z'] -- to None (Piotroski needs a prior period;
    Altman Z here needs totalAssets, which this fixture omits). Those keys
    being PRESENT-but-None must not crash `snap["fscore"] >= 7` /
    f"{snap['z']:.2f}" -- only a real (non-None) value should render a row."""
    class _OnePeriodDetails:
        def get_income_statement(self, **kwargs):
            return {"statements": [{"net_income": 25_000_000_000.0,
                                    "weighted_average_shares_outstanding": 16_000_000_000.0}]}

        def get_balance_sheet(self, **kwargs):
            # No totalAssets/total_assets -> altman_z_and_variant returns (None, None).
            return {"statements": [{"total_shareholder_equity": 60_000_000_000.0}]}

        def get_cashflow_statement(self, **kwargs):
            return {"statements": [{}]}

    def _resolver_one_period(cat, name, **kw):
        return {"ohlcv": _FakeOHLCV(), "fundamentals_details": _OnePeriodDetails()}.get(cat)

    _data.reset_caches()
    result = DeterministicScorer.export_symbol_data("AAPL", providers_resolver=_resolver_one_period)
    assert result.error is None, result.error
    labels = {m.label for m in result.metrics}
    assert "Piotroski F-Score" not in labels, "fscore is None -- must not render (or crash)"
    assert "Altman Z" not in labels, "z is None -- must not render (or crash)"
    assert "Quality (ROE)" in labels, "net_income/equity are present and should still surface"
