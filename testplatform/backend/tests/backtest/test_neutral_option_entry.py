"""Real rule evaluation, cached two-leg premiums and option fills for both entry arms."""
import pytest
from ba2_common.core.types import OrderRecommendation as Signal, Recommendation
from app.services.backtest.options_cache import OptionsHistoryCache
from app.services.backtest.default_rulesets import seed_entry_ruleset_from_rules
from tests.backtest import test_option_entry_path as fixture
from tests.backtest.test_options_rule_e2e import START, _EXPIRY, _PREMIUM_180


@pytest.mark.parametrize("mode,signal,confidence,opens", [
    ("hold", Signal.HOLD, 10, True), ("hold", Signal.BUY, 20, False),
    ("hold", Signal.SELL, 20, False), ("low_confidence", Signal.BUY, 20, True),
    ("low_confidence", Signal.SELL, 20, True), ("low_confidence", Signal.BUY, 60, False),
    ("low_confidence", Signal.HOLD, 10, False), ("legacy", Signal.HOLD, 10, False),
])
def test_real_straddle_entry(mode, signal, confidence, opens, monkeypatch):
    original_seed = fixture._seed_cache
    def seed(db_path):
        original_seed(db_path)
        cache = OptionsHistoryCache(db_path)
        # The snapshot writer upserts; retain calls alongside the new put.
        cache.write_chain_rows("AAPL", START.date().isoformat(), [{
            "occ_symbol": "AAPL240315P00180000", "option_type": "put", "strike": 180.,
            "expiry": _EXPIRY.isoformat(), "bid": 4., "ask": 4.2, "last": 4.1,
            "iv": .3, "delta": -.5, "open_interest": 5000}])
        cache.write_bar_rows([{"occ_symbol": "AAPL240315P00180000", "date": d.isoformat(),
            "open": o, "high": h, "low": low, "close": c, "volume": 400,
            "underlying": "AAPL", "option_type": "put", "strike": 180.,
            "expiry": _EXPIRY.isoformat()} for d,o,h,low,c in _PREMIUM_180])
    monkeypatch.setattr(fixture, "_seed_cache", seed)
    engine, account, expert, ctx, expert_id, action = fixture._build_engine_with_option_entry(
        action_type="open_straddle", strike_method="percent_otm", strike_param=0.,
        dte_min=20, dte_max=45, sizing=10.)
    try:
        expert.settings["neutral_option_entry_mode"] = mode
        gate = ({"id": "hold", "field": "current_rating_neutral", "field_type": "flag"}
                if mode != "low_confidence" else
                {"id": "low", "field": "confidence", "op": "<=", "value": 30})
        rules = [{"id": "neutral", "conditions": {"type": "AND", "conditions": [gate,
            {"id": "flat", "field": "has_no_position", "field_type": "flag"}]},
            "actions": [action], "continue_processing": False}]
        ruleset = seed_entry_ruleset_from_rules(rules, name="neutral")
        engine.experts = [(expert, expert_id, expert.settings, ruleset)]
        expert.analyze_as_of = lambda as_of, context: Recommendation(
            signal=signal, confidence=confidence,
            current_price=context.account.get_instrument_current_price("AAPL"),
            details="neutral fixture", raw_outputs={})
        engine.run()
        positions = account.get_option_positions()
        assert len(positions) == (2 if opens else 0)
        assert (account.get_balance() < 100_000.) == opens
    finally:
        ctx.__exit__(None, None, None)
