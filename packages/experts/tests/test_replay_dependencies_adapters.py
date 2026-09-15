"""Per-expert replay-dependency adapters (spec step 4, section 5 table).

Each adapter answers "what does THIS configuration read", and each assertion here
pins a branch the expert's ``_gather`` actually has. The two failure modes both
cost a whole comparison run:

* declaring TOO LITTLE -- the warm plan looks complete, the historical replay then
  hits a cache miss and the run is scrapped (the ``expected_profit_mode='model'``
  namespaces the 2026-09-10 audit found missing are exactly this);
* declaring TOO MUCH -- a green coverage row for an input live never consumed.
"""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_common.core.replay import dependencies as dep
from ba2_experts import replay_dependencies  # noqa: F401 - registers the adapters on import

NOW = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
WINDOW = dep.Window(start=NOW - timedelta(days=365), end=NOW)

#: The risk-manager keys ``rule_requirements`` always reads. Off, so a test about an
#: expert's own reads is not polluted by the ATR declaration.
RM_OFF = {"use_atr_stop": False, "sizing_mode": "notional", "atr_period": 14}


def _resolve(expert, settings, universe=("AAPL",), rules=None):
    return dep.required_replay_inputs(expert, {**RM_OFF, **settings}, rules, list(universe),
                                      WINDOW)


def _namespaces(requirements, kind=None):
    return {r.namespace for r in requirements if kind is None or r.kind == kind}


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def test_the_four_recorded_experts_all_have_adapters():
    assert set(replay_dependencies.ADAPTED_EXPERTS) <= set(dep.registered_experts())
    assert set(replay_dependencies.ADAPTED_EXPERTS) == {
        "FMPRating", "FMPEarningsDrift", "FMPInsiderClusterBuy", "DeterministicScorer"}


@pytest.mark.parametrize("expert", ["FactorRanker", "FMPSenateTraderWeight", "FinnHubRating",
                                    "ETFTrend", "PennyMomentumTrader"])
def test_every_other_expert_is_unsupported_not_silently_covered(expert):
    out = _resolve(expert, {})

    assert [r.kind for r in out] == [dep.KIND_UNSUPPORTED]


# --------------------------------------------------------------------------- #
# FMPRating
# --------------------------------------------------------------------------- #
def test_fmprating_declares_targets_grades_and_the_price_series():
    out = _resolve("FMPRating", {"max_analyst_age_months": 0})

    assert _namespaces(out, dep.KIND_HISTORY) == {"price_target", "grades_historical"}
    series = [r for r in out if r.kind == dep.KIND_TIMESERIES]
    assert [(r.symbol, r.interval, r.provider) for r in series] == [("AAPL", "1d", "fmp")]


def test_fmprating_recency_filter_adds_the_dated_individual_grades():
    off = _resolve("FMPRating", {"max_analyst_age_months": 0})
    on = _resolve("FMPRating", {"max_analyst_age_months": 6})

    assert "analyst_grades" not in _namespaces(off)
    assert "analyst_grades" in _namespaces(on), (
        "max_analyst_age_months>0 makes _gather fetch the DATED grades endpoint")


def test_fmprating_without_its_recency_setting_is_a_loud_error():
    with pytest.raises(dep.MissingDependencySetting) as excinfo:
        _resolve("FMPRating", {})

    assert excinfo.value.key == "max_analyst_age_months"


# --------------------------------------------------------------------------- #
# FMPEarningsDrift
# --------------------------------------------------------------------------- #
def test_earnings_drift_static_mode_declares_only_the_quarterly_calendar():
    out = _resolve("FMPEarningsDrift", {"expected_profit_mode": "static"})

    assert _namespaces(out, dep.KIND_HISTORY) == {"past_earnings_quarterly"}


def test_earnings_drift_model_mode_adds_the_estimator_namespaces():
    out = _resolve("FMPEarningsDrift", {"expected_profit_mode": "model"})

    assert _namespaces(out, dep.KIND_HISTORY) == {
        "past_earnings_quarterly", "earnings_estimates_quarterly"}
    estimates = [r for r in out if r.namespace == "earnings_estimates_quarterly"]
    assert "revision" in estimates[0].reason, (
        "the estimates endpoint filters fiscal periods, not revisions -- the plan must carry "
        "that provenance limitation (spec section 5)")


# --------------------------------------------------------------------------- #
# FMPInsiderClusterBuy
# --------------------------------------------------------------------------- #
def test_insider_declares_the_insider_history_over_its_configured_lookback():
    out = _resolve("FMPInsiderClusterBuy",
                   {"expected_profit_mode": "static", "lookback_days": 400})

    insider = [r for r in out if r.namespace == "insider_v2"]
    assert len(insider) == 1
    assert (insider[0].window.end - insider[0].window.start).days == 400


def test_insider_model_mode_adds_the_estimator_namespaces():
    out = _resolve("FMPInsiderClusterBuy",
                   {"expected_profit_mode": "model", "lookback_days": 400})

    assert {"past_earnings_quarterly", "earnings_estimates_quarterly"} <= _namespaces(out)


# --------------------------------------------------------------------------- #
# DeterministicScorer
# --------------------------------------------------------------------------- #
DS_BASE = {"w_analyst": 0.0, "w_earnings": 0.0, "index_symbol": "SPY",
           "use_model_target": False}


def test_ds_declares_statements_the_benchmark_and_the_macro_series():
    out = _resolve("DeterministicScorer", DS_BASE)

    assert _namespaces(out, dep.KIND_HISTORY) == {
        "income_statement_annual", "balance_sheet_annual", "cashflow_statement_annual"}
    series = {(r.symbol, r.interval) for r in out if r.kind == dep.KIND_TIMESERIES}
    assert series == {("AAPL", "1d"), ("SPY", "1d")}, "the benchmark is its own price series"
    assert _namespaces(out, dep.KIND_SERIES) == {"VIXCLS", "UNRATE", "BAA10Y", "T10Y3M"}


def test_ds_macro_is_declared_even_though_its_score_weight_can_be_off():
    """fetch_macro_series runs unconditionally; spec section 5 says account for it."""
    out = _resolve("DeterministicScorer", {**DS_BASE, "w_analyst": 0.0})

    macro = [r for r in out if r.kind == dep.KIND_SERIES]
    assert macro and all(r.optional is False for r in macro)
    assert all(r.provider == dep.FRED_PROVIDER and r.symbol is None for r in macro)


def test_ds_w_analyst_zero_removes_the_analyst_history():
    off = _resolve("DeterministicScorer", DS_BASE)
    on = _resolve("DeterministicScorer", {**DS_BASE, "w_analyst": 0.3})

    assert {"grades_historical", "price_target"} & _namespaces(off) == set()
    assert {"grades_historical", "price_target"} <= _namespaces(on)


def test_ds_w_earnings_zero_removes_the_earnings_history():
    off = _resolve("DeterministicScorer", DS_BASE)
    on = _resolve("DeterministicScorer", {**DS_BASE, "w_earnings": 0.2})

    assert "past_earnings_quarterly" not in _namespaces(off)
    assert "past_earnings_quarterly" in _namespaces(on)


def test_ds_model_mode_adds_the_estimator_namespaces():
    out = _resolve("DeterministicScorer", {**DS_BASE, "use_model_target": True})

    assert {"past_earnings_quarterly", "earnings_estimates_quarterly"} <= _namespaces(out)


def test_ds_benchmark_follows_the_index_symbol_setting():
    out = _resolve("DeterministicScorer", {**DS_BASE, "index_symbol": "QQQ"})

    assert {r.symbol for r in out if r.kind == dep.KIND_TIMESERIES} == {"AAPL", "QQQ"}


def test_ds_price_series_covers_the_lookback_its_fetcher_asks_for():
    from ba2_experts.DeterministicScorer import data as ds_data

    out = _resolve("DeterministicScorer", DS_BASE)
    series = [r for r in out if r.kind == dep.KIND_TIMESERIES][0]

    assert (series.window.end - series.window.start).days == ds_data.OHLCV_LOOKBACK_DAYS


def test_ds_macro_ids_come_from_the_fetcher_not_a_second_copy():
    from ba2_experts.DeterministicScorer import data as ds_data

    out = _resolve("DeterministicScorer", DS_BASE)

    assert _namespaces(out, dep.KIND_SERIES) == set(ds_data.MACRO_SERIES_IDS)


# --------------------------------------------------------------------------- #
# The rule extras reach every supported expert
# --------------------------------------------------------------------------- #
def test_rule_and_risk_manager_extras_are_appended_to_a_supported_expert():
    out = dep.required_replay_inputs(
        "FMPRating",
        {"max_analyst_age_months": 0, "use_atr_stop": True, "sizing_mode": "risk_atr",
         "atr_period": 14},
        [{"triggers": {"a": {"event_type": "days_since_last_close"}}}],
        ["AAPL"], WINDOW)

    kinds = {r.kind for r in out}
    assert dep.KIND_INDICATOR in kinds and dep.KIND_STATE in kinds
