"""``--market-exit spy`` in the REAL ``DailyBacktestEngine``, and its scoping under ``--regime-split-entry``.

The ``<prefix>-spy-stop`` rule adjusts the stop to the open price (percent 0 = breakeven) while SPY is below its
200-day, and continues processing. Four engine facts are pinned here, each with a control that proves the
fixture can tell the cases apart:

1. SPY above its 200-day: the rule leaves the stop alone;
2. SPY below it: the stop moves to the position's open price;
3. SPY trend UNKNOWN (too little history): the rule does nothing (an unknown regime matches neither side);
4. WITH the split (``entry_tag``): the DOWN section opens while SPY is below its 200-day, so an unscoped rule
   would pull ITS stop to breakeven at once; scoped by ``entry_tag == <UP tag>`` it touches only the position the
   UP section opened. The equity ``buy`` carries the tag onto the entry order in the backtest
   (``test_per_entry_routing``) and in live (``tests/test_live_per_entry_routing.py``), so the same scoping holds live.

Run from the backend dir:
    python -m pytest tests/backtest/test_market_exit_spy_stop_engine.py -q
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from ba2_common.core import benchmark_regime as br
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.market_condition_templates import (
    SPY_DOWN_ENTRY_TAG, SPY_UP_ENTRY_TAG, independent_exit_rules, pinned_benchmark_leaf)
from ba2_common.core.rule_models import normalize_trade_rules
from ba2_common.core.types import OrderRecommendation, Recommendation

from app.services.strategy_param_space import decode_params
from tests.backtest._no_atr import NoAtrIndicatorProvider
from tests.backtest.test_max_loss_stop_engine import CFG

A, B = "AAA", "BBB"
START, END = datetime(2024, 1, 2), datetime(2024, 1, 31)
CRASH = date(2024, 1, 16)            # SPY is above its 200-day before this day and below it from this day
B_FROM = date(2024, 1, 22)           # BBB is only bought from here on (SPY is below its 200-day then)


def _weekdays(start, end):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _weekdays(START.date(), END.date())


def _calendar(kind):
    """``up``: always above; ``crash``: above until CRASH, far below after; ``short``: fewer than 200 bars (unknown)."""
    dates = _weekdays(date(2023, 1, 2) if kind != "short" else date(2023, 11, 1), date(2024, 2, 29))
    closes = []
    for i, d in enumerate(dates):
        base = 100.0 + 0.1 * i
        closes.append(base * 0.6 if kind == "crash" and d >= CRASH else base)
    return br.BenchmarkRegimeCalendar(dates, closes)


class _Expert(MarketExpertInterface):
    """BUY AAA every bar; BUY BBB only from B_FROM (before it, a HOLD-grade confidence)."""

    bypasses_classic_rm = False
    _gather_symbol = None

    def __init__(self, id: int):
        super().__init__(id)
        self._settings_cache = {}

    @classmethod
    def description(cls) -> str:
        return "Stub expert for the spy stop engine test."

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        day = as_of.date() if hasattr(as_of, "date") else as_of
        buy = self._gather_symbol == A or day >= B_FROM
        return Recommendation(
            signal=OrderRecommendation.BUY, confidence=80.0 if buy else 10.0,
            current_price=context.account.get_instrument_current_price(self._gather_symbol),
            details="stub", expected_profit_percent=10.0, raw_outputs={})


def _bars(offset):
    """Rising by 1 a day with the low above the previous close: a stop at an entry's open price is never hit."""
    out = []
    for i, d in enumerate(DAYS):
        c = 100.0 + offset + i
        out.append({"Date": d, "Open": c, "High": c + 0.5, "Low": c - 0.1, "Close": c, "Volume": 1_000_000})
    return out


def _buy(tag):
    action = {"id": "buy", "action_type": "buy"}
    if tag is not None:
        action["entry_tag"] = tag
    return action


def _entry_rules(split):
    base = [{"id": "conf", "field": "confidence", "op": ">=", "value": 70.0},
            {"id": "flat", "field": "has_no_position"}]
    if not split:
        return [{"id": "eq", "conditions": {"type": "AND", "conditions": base}, "actions": [_buy(None)]}]
    mk = lambda rid, token, tag: {                                                       # noqa: E731
        "id": rid, "conditions": {"type": "AND", "conditions": base + [
            pinned_benchmark_leaf(f"{rid}-spy-trend", "spy_trend_regime", token)]},
        "actions": [_buy(tag)]}
    return [mk("eq", "above_200", SPY_UP_ENTRY_TAG), mk("eq-dn", "below_200", SPY_DOWN_ENTRY_TAG)]


def _run(run_id, calendar, *, split=False, scoped=True, rule_on=True):
    """Full engine.run(); returns ``{symbol: (stop_loss, open_price)}`` of each entry transaction, read before teardown.

    The strategy goes through ``decode_params`` like a GA trial's does (rule toggled on, percent 0 = breakeven)."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import (
        seed_entry_ruleset_from_rules, seed_exit_ruleset_from_rules)
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderDirection

    template = SimpleNamespace(
        entry_rules=normalize_trade_rules(_entry_rules(split)),
        exit_rules=normalize_trade_rules(independent_exit_rules(
            "eq", "long", ("spy",), spy_up_tag=SPY_UP_ENTRY_TAG if scoped else None)))
    flat = {"exit:eq-spy-stop:enabled": 1 if rule_on else 0, "exit:eq-spy-stop:a0:action_value": 0.0}
    decoded = decode_params(template, flat)

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"spy-stop-{run_id}")
    ctx.__enter__()
    try:
        seed_account_definition(run_id, CFG)
        enter_id = seed_entry_ruleset_from_rules(decoded["entry_rules"], name=f"spy-enter-{run_id}")
        open_id = seed_exit_ruleset_from_rules(decoded["exit_rules"], name=f"spy-open-{run_id}")
        seed_expert_instance(account_id=run_id, expert_class_name="_Expert",
                             enter_market_ruleset_id=enter_id, open_positions_ruleset_id=open_id,
                             instance_id=run_id)
        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars(A, _bars(0.0))
        ps.load_bars(B, _bars(30.0))
        ps.set_clock(START)
        account = BacktestAccount(run_id, ps, CFG)
        resolver.register_account(run_id, account)
        expert = _Expert(run_id)
        expert.save_settings({
            "allow_automated_trade_opening": (True, "bool"), "allow_automated_trade_modification": (True, "bool"),
            "enable_buy": (True, "bool"), "sizing_mode": ("risk_atr", "str"),
            "risk_per_trade_pct": (8.0, "float"), "min_stop_loss_pct": (8.0, "float"),
            "use_atr_stop": (False, "bool")})
        resolver.register_expert(run_id, expert)
        engine = DailyBacktestEngine(
            account=account, experts=[(expert, run_id, {}, enter_id)], price_source=ps,
            config={"start_date": START, "end_date": END, "enabled_instruments": [A, B], "seed": 42},
            indicator_provider=NoAtrIndicatorProvider(), benchmark_calendar=calendar,
            benchmark_regime_required=True)
        engine.run()
        out = {}
        for o in sorted(account.get_orders(), key=lambda o: o.id):
            if o.side == OrderDirection.BUY and o.depends_on_order is None and o.symbol not in out:
                txn = get_instance(Transaction, o.transaction_id)
                tag = (o.data or {}).get("entry_tag")
                out[o.symbol] = SimpleNamespace(stop=txn.stop_loss, open=o.open_price, tag=tag)
        return out
    finally:
        ctx.__exit__(None, None, None)


@pytest.fixture(autouse=True)
def _clean_seam():
    br.reset_benchmark_regime()
    yield
    br.reset_benchmark_regime()


def _breakeven(p):
    return p.stop == pytest.approx(p.open)


def _safeguard(p):
    return p.stop is not None and p.stop < p.open * 0.95          # the entry's own 8% stop, untouched


# ------------------------------------------------------------------ 1-3. the rule itself


def test_spy_above_its_200_day_leaves_the_stop_alone():
    res = _run(9801, _calendar("up"), scoped=False)
    assert A in res and _safeguard(res[A]), vars(res[A])


def test_spy_below_its_200_day_moves_the_stop_to_the_open_price():
    res = _run(9802, _calendar("crash"), scoped=False)
    assert _breakeven(res[A]), vars(res[A])


def test_the_rule_toggled_off_does_nothing_even_below_the_200_day():
    res = _run(9803, _calendar("crash"), scoped=False, rule_on=False)
    assert _safeguard(res[A]), vars(res[A])


def test_an_unknown_spy_trend_does_nothing():
    res = _run(9804, _calendar("short"), scoped=False)
    assert A in res and _safeguard(res[A]), vars(res[A])


# ------------------------------------------------------------------ 4. the split


def test_the_split_tags_each_entry_with_its_section():
    res = _run(9805, _calendar("crash"), split=True)
    assert res[A].tag == SPY_UP_ENTRY_TAG, vars(res[A])          # opened on 01-02, SPY above its 200-day
    assert res[B].tag == SPY_DOWN_ENTRY_TAG, vars(res[B])        # opened after B_FROM, SPY below it


def test_without_the_scope_the_rule_pulls_a_down_entry_stop_to_breakeven_the_control():
    res = _run(9806, _calendar("crash"), split=True, scoped=False)
    assert _breakeven(res[A]) and _breakeven(res[B]), (vars(res[A]), vars(res[B]))


def test_scoped_to_the_up_section_a_down_entry_is_not_touched_and_an_up_entry_is():
    res = _run(9807, _calendar("crash"), split=True, scoped=True)
    assert _breakeven(res[A]), vars(res[A])                      # opened by the UP section: adjusted
    assert _safeguard(res[B]), vars(res[B])                      # opened by the DOWN section: its stop stays
