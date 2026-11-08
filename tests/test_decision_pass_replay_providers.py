"""Rules that read MARKET-DATA PROVIDERS replay from the provider tape.

A condition such as ``percent_below_recent_high`` (OHLCV) or ``days_to_earnings`` (fundamentals
earnings calendar) resolves its provider through ``TradeConditions``' injected resolver. Live, the
provider's own tap (``market_data.get_ohlcv_data`` / ``fundamentals_details.get_past_earnings``)
records the read inside the pass' capture scope; the decision replay serves it back from the tape
through the gather tape's actors. A read the recording does not hold is ``missing_capture`` naming
the provider and method.

The fake providers here carry the SAME tap decorators and identity functions as the shipped
providers (``MarketDataProviderInterface.get_ohlcv_data``, ``FMPCompanyDetailsProvider.get_past_earnings``).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from ba2_common.core.interfaces.MarketDataProviderInterface import ohlcv_identity
from ba2_common.core.replay import ReplayStatus, load_bundle
from ba2_common.core.replay.observe import observe_provider
from ba2_common.core.types import AnalysisUseCase, ExpertActionType, OrderRecommendation
from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import past_earnings_identity
from tests import factories
from tests.test_decision_pass_capture_roundtrip import _Account
from tests.test_decision_pass_replay import (
    DIFFERENCE, MATCH, MISSING, drop_observations, enter_bundle_of, rewrite_payload, rows, run_replay)
from tests.test_funded_entry_loop import FROZEN_NOW, _EnterExpert, _run_enter


class FakeOHLCV:
    """Carries the shipped OHLCV tap; ``high`` is the 20-day high the condition reads."""

    high = 110.0

    @observe_provider("market_data", "get_ohlcv_data", identity=ohlcv_identity)
    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d",
                       use_cache=True, max_cache_age_hours=24, lookback_days=30):
        days = pd.date_range(end=datetime(2026, 11, 6), periods=40, freq="D")
        return pd.DataFrame({"Date": days, "Open": 100.0, "High": self.high, "Low": 90.0,
                             "Close": 100.0, "Volume": 1_000_000})


class FakeDetails:
    """Carries the shipped ``get_past_earnings`` tap and answers a calendar with one scheduled print."""

    raises = False

    @observe_provider("fundamentals_details", "get_past_earnings", identity=past_earnings_identity, record_errors=True)
    def get_past_earnings(self, symbol, frequency, end_date, lookback_periods=8, format_type="markdown"):
        if FakeDetails.raises:
            raise RuntimeError("FMP calendar unavailable")
        report = date.today() + timedelta(days=10)
        return {"earnings": [{"report_date": report.isoformat(), "fiscal_date_ending": "2026-09-30"}]}


@pytest.fixture
def fake_providers(monkeypatch):
    from ba2_trade_platform.modules import dataproviders

    def get_provider(category, name, **kwargs):
        if category == "ohlcv":
            return FakeOHLCV()
        if category == "fundamentals_details":
            return FakeDetails()
        raise ValueError(f"no fake provider for {category}/{name}")

    monkeypatch.setattr(dataproviders, "get_provider", get_provider)
    FakeOHLCV.high = 110.0
    FakeDetails.raises = False


def _world(extra_triggers):
    acct_def = factories.create_account_definition(provider="MockAccount")
    rs = factories.create_ruleset(name="enter-providers", subtype=AnalysisUseCase.ENTER_MARKET)
    triggers = {"trigger_0": {"event_type": "bullish"},
                "trigger_1": {"event_type": "confidence", "operator": ">=", "value": 60.0}}
    triggers.update(extra_triggers)
    ea = factories.create_event_action(
        name="buy-provider", subtype=AnalysisUseCase.ENTER_MARKET, triggers=triggers,
        actions={"action_0": {"action_type": ExpertActionType.BUY.value},
                 "action_1": {"action_type": ExpertActionType.ADJUST_STOP_LOSS.value,
                              "value": -5.0, "reference_value": "current_price"}})
    factories.link_rule_to_ruleset(rs.id, ea.id, 0)
    inst = factories.create_expert_instance(account_id=acct_def.id, expert="_EnterExpert",
                                            virtual_equity_pct=100.0, enter_market_ruleset_id=rs.id)
    expert = _EnterExpert(inst.id)
    expert.save_settings({
        "allow_automated_trade_opening": (True, "bool"), "enable_buy": (True, "bool"),
        "enable_sell": (False, "bool"),
        "max_virtual_equity_per_instrument_percent": (40.0, "float")})
    profit = 30.0
    for sym in ("AAPL", "MSFT"):
        factories.create_recommendation(
            instance_id=inst.id, symbol=sym, recommended_action=OrderRecommendation.BUY,
            expected_profit_percent=profit, price_at_date=100.0, confidence=90.0,
            created_at=FROZEN_NOW)
        profit -= 5.0
    return inst, expert, _Account(acct_def.id)


INDICATOR = {"trigger_2": {"event_type": "percent_below_recent_high", "operator": ">=", "value": 5.0}}
EARNINGS = {"trigger_2": {"event_type": "days_to_earnings", "operator": "<=", "value": 30}}


def _bundle(tmp_path, test_engine, triggers):
    return enter_bundle_of(tmp_path, test_engine, lambda: _world(triggers),
                           lambda w: _run_enter(w[1], w[2], w[0].id))


@pytest.mark.parametrize("triggers,provider_method", [
    (INDICATOR, "get_ohlcv_data"), (EARNINGS, "get_past_earnings")])
def test_a_rule_reading_a_provider_replays_from_the_tape_to_a_match(
        tmp_path, test_engine, fake_providers, triggers, provider_method):
    bundle_dir = _bundle(tmp_path, test_engine, triggers)
    recorded = [o for o in load_bundle(bundle_dir).observations if o.method == provider_method]
    assert recorded, "the live pass must have recorded the provider read inside its capture scope"

    report = run_replay(bundle_dir)

    for row in report.results:
        assert row.status == MATCH, f"{row.expert_class} {row.symbol}: {row.detail} {row.field_diffs}"
    assert {r.symbol for r in rows(report, "rules")} == {"AAPL", "MSFT"}
    assert rows(report, "execute"), "the rule fired live, so the entries replay too"


@pytest.mark.parametrize("triggers,provider,method", [
    (INDICATOR, "market_data", "get_ohlcv_data"),
    (EARNINGS, "fundamentals_details", "get_past_earnings")])
def test_a_deleted_provider_observation_is_missing_capture_naming_provider_and_method(
        tmp_path, test_engine, fake_providers, triggers, provider, method):
    bundle_dir = _bundle(tmp_path, test_engine, triggers)
    drop_observations(bundle_dir, lambda o: o.provider == provider and o.method == method)

    report = run_replay(bundle_dir)

    rules = rows(report, "rules")
    assert [r.status for r in rules] == [MISSING] * 2
    for row in rules:
        assert provider in row.detail and method in row.detail, row.detail
    assert all(r.status == MISSING for r in rows(report, "size") + rows(report, "execute"))


def test_a_changed_recorded_provider_value_is_a_rules_difference(tmp_path, test_engine, fake_providers):
    bundle_dir = _bundle(tmp_path, test_engine, INDICATOR)

    def flatten_highs(df):
        df["High"] = 100.0                     # the recorded bars now show no dip: the condition is false

    rewrite_payload(bundle_dir, lambda o: o.method == "get_ohlcv_data" and o.tap_depth == 0, flatten_highs)
    report = run_replay(bundle_dir)
    assert any(r.status == DIFFERENCE for r in rows(report, "rules"))


def test_a_provider_read_that_failed_live_replays_as_the_same_failure(tmp_path, test_engine, fake_providers):
    FakeDetails.raises = True                  # live: the condition's best-effort handler returns False
    bundle_dir = _bundle(tmp_path, test_engine, EARNINGS)

    report = run_replay(bundle_dir)

    for row in report.results:
        assert row.status in (MATCH, ReplayStatus.COVERAGE_NOT_RUN), \
            f"{row.expert_class} {row.symbol}: {row.status} {row.detail}"
