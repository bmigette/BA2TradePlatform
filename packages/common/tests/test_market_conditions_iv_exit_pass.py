"""A lazily resolved chain field obeys the exit-pass contract (re-review 2026-11-15, item 5).

The composite resolves a live chain profile's field when the condition READS it (``by_field.get``),
not in ``observe``. A programming error raised there must, on a live EXIT pass, read as unknown and
leave the position's other exit rules running -- exactly as a failing ``observe`` does -- and still
propagate on an entry pass. And the decision scope's end-of-pass summary flush never raises.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import ba2_common.core.TradeConditions as TC
from ba2_common.core import market_condition_live as live
from ba2_common.core.market_condition_iv_live import ChainLiveReader
from ba2_common.core.market_condition_readers import CompositeMarketConditionReader
from ba2_common.core.market_conditions import IV_PROFILE, STATUS_VALID, FeatureRow, Observation
from ba2_common.core.types import ExpertEventType

MOMENT = datetime(2025, 11, 17, 15, 0, tzinfo=timezone.utc)


class _Broken(ChainLiveReader):
    option_source = "alphavantage"

    def _evidence(self, symbol, session):
        raise TypeError("a bug in the chain reader")


class _Ohlcv:
    profile = "ohlcv-v1"
    calc_version = "ohlcv-v1/calc-2"

    def observe(self, symbol, session):
        values = {f: Observation(10.0, STATUS_VALID) for f in
                  ("underlying_trend_slope_50_atr14", "underlying_adx_14", "underlying_realized_vol_ratio_5_20")}
        return FeatureRow(values=values, calc_versions={f: self.calc_version for f in values})


@pytest.fixture
def scope(monkeypatch):
    saved = TC.get_market_condition_context_resolver()
    resolver = live.LiveMarketConditionResolver(("ohlcv-v1", IV_PROFILE),
                                                reader=CompositeMarketConditionReader([_Ohlcv(), _Broken(IV_PROFILE)]))
    TC.set_market_condition_context_resolver(resolver)
    monkeypatch.setattr(live, "_replay_now", lambda: MOMENT)
    yield resolver
    TC.set_market_condition_context_resolver(saved)


def _leaf(event, value):
    return TC.create_condition(event, object(), "AAA", None, operator_str=">", value=value)


def test_on_an_exit_pass_the_broken_chain_field_is_unknown_and_the_next_rule_still_runs(scope):
    end = live.begin_exit_pass(1)
    try:
        with live.market_condition_decision_scope():
            iv_leaf = _leaf(ExpertEventType.N_UNDERLYING_IV_RV_RATIO, 1.0)
            assert iv_leaf.evaluate() is False                 # unknown per contract, no raise
            assert "TypeError" in iv_leaf.last_reason
            adx_leaf = _leaf(ExpertEventType.N_UNDERLYING_ADX, 5.0)
            assert adx_leaf.evaluate() is True                 # the remaining exit rule still runs
    finally:
        end()


def test_on_an_entry_pass_the_programming_error_propagates(scope):
    with live.market_condition_decision_scope():
        with pytest.raises(TypeError):
            _leaf(ExpertEventType.N_UNDERLYING_IV_RV_RATIO, 1.0).evaluate()


def test_the_end_of_pass_summary_flush_never_raises(scope, monkeypatch):
    def boom(self, token=None):
        raise RuntimeError("the summary itself broke")

    monkeypatch.setattr(_Broken, "flush_pass_summary", boom)
    with live.market_condition_decision_scope():
        pass                                                   # leaving the scope must not raise
