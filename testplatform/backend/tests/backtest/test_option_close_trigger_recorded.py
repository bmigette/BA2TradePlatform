"""BT/live option parity, plan Part C3/C4: an option trade row says WHY it closed.

Before this, ``get_round_trip_trades`` labelled every option exit by PRICE PROXIMITY
(``_exit_reason``): a ``close_option`` ticket is a plain limit order with no stop, so a
stop-loss close, a DTE exit and a take-profit all read ``take_profit``, and an expiry or an
assignment read ``exit`` -- while ``Transaction.close_reason`` said ``option_expiry`` for
both expiry AND assignment, and live said ``expired`` / ``assigned`` / ``exercised``.

Now every option closing order carries ``data["exit_record"]`` (``option_trade_record``)
whose ``trigger`` is an ``OptionCloseReason``, the trade row's ``exit_reason`` IS that
trigger, and the option-specific transaction closes (expiry / assignment / exercise /
margin liquidation) write the same enum value as ``close_reason`` -- the values the live
OCC-activity reconciler writes (pinned in ``tests/test_option_assignment.py``).

Run from the backend dir:
    python -m pytest tests/backtest/test_option_close_trigger_recorded.py -q
"""
from __future__ import annotations

from tests.backtest._spread_cfg import LEGACY_ZERO_SPREAD as _LEGACY_ZERO_SPREAD
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from ba2_common.core.option_trade_record import LEG_SNAPSHOT_FIELDS, OPTION_TRADE_RECORD_VERSION
from ba2_common.core.types import (
    AssetClass, ExpertActionType, OptionCloseReason, OptionRight, OrderDirection,
    OrderRecommendation, TransactionStatus,
)

CFG = {
    **_LEGACY_ZERO_SPREAD, "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

_EXPIRY = date(2024, 3, 15)
_CALL_180 = "AAPL240315C00180000"
_CALL_190 = "AAPL240315C00190000"
_CALL_220 = "AAPL240315C00220000"

#: spot path: entry decision 03-05, fill 03-06, a quiet 03-07/03-08, expiry 03-15.
def _bars(expiry_close: float, mid_close: float = 181.0):
    rows = [(datetime(2024, 3, 5), 180.0), (datetime(2024, 3, 6), 181.0),
            (datetime(2024, 3, 7), mid_close), (datetime(2024, 3, 8), mid_close),
            (datetime(2024, 3, 15), expiry_close), (datetime(2024, 3, 18), expiry_close)]
    return [{"Date": d, "Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 1000}
            for d, c in rows]


def _prem_bar(occ, d, close, strike, open_=None):
    o = close if open_ is None else open_
    return {"occ_symbol": occ, "date": d, "open": o, "high": max(o, close),
            "low": min(o, close), "close": close, "volume": 500, "underlying": "AAPL",
            "option_type": "call", "strike": strike, "expiry": _EXPIRY.isoformat()}


def _account(tmp_path, tag, *, expiry_close, contracts, prem_bars, mid_close=181.0):
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.options_cache import OptionsHistoryCache
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    cache_db = str(tmp_path / f"{tag}.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows("AAPL", "2024-03-01", [
        {"occ_symbol": occ, "option_type": "call", "strike": k,
         "expiry": _EXPIRY.isoformat(), "bid": 3.0, "ask": 3.2, "last": 3.1, "iv": 0.25}
        for occ, k in contracts])
    cache.write_bar_rows(prem_bars)
    provider = HistoricalOptionsProvider(cache_db)
    wire_backtest_seams()
    ctx = backtest_trading_db(tag)
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _bars(expiry_close, mid_close))
    ps.set_clock(datetime(2024, 3, 5))
    acct = BacktestAccount(1, ps, CFG, options_provider=provider)
    wire_backtest_seams().register_account(1, acct)
    return acct, ps, ctx


def _leg(occ, side, strike):
    from ba2_common.core.option_types import OptionLeg
    intent = "buy_to_open" if side == OrderDirection.BUY else "sell_to_open"
    return OptionLeg(contract_symbol=occ, side=side, position_intent=intent,
                     option_type=OptionRight.CALL, strike=strike, expiry=_EXPIRY,
                     underlying="AAPL")


def _open(acct, legs, strategy, limit=None):
    acct.submit_option_order(legs=legs, quantity=1,
                             order_type="market" if limit is None else "limit",
                             limit_price=limit, option_strategy=strategy)
    acct.refresh_orders()
    acct.refresh_transactions()
    assert acct.get_option_positions(), "the fixture entry did not fill"


def _expire(acct, ps):
    from app.services.backtest.daily_engine import DailyBacktestEngine
    engine = DailyBacktestEngine.__new__(DailyBacktestEngine)
    engine.account = acct
    engine.price = ps
    engine.config = CFG
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))


def _option_rows(acct):
    return [t for t in acct.get_round_trip_trades() if t.get("contract_symbol")]


def _option_txns():
    from ba2_common.core.trade_store import transactions_where
    return [t for t in transactions_where(status=TransactionStatus.CLOSED)
            if t.asset_class == AssetClass.OPTION]


def _only(rows, occ):
    matching = [r for r in rows if r["contract_symbol"] == occ]
    assert len(matching) == 1, matching
    return matching[0]


# ---------------------------------------------------------------------------
# expiry / assignment / exercise: the trigger AND Transaction.close_reason
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("side,expiry_close,expected", [
    (OrderDirection.SELL, 170.0, OptionCloseReason.EXPIRED_OTM),   # short OTM: expires
    (OrderDirection.BUY, 170.0, OptionCloseReason.EXPIRED_OTM),    # long OTM: expires
    (OrderDirection.SELL, 200.0, OptionCloseReason.ASSIGNED),      # short ITM: assigned
    (OrderDirection.BUY, 200.0, OptionCloseReason.EXERCISED),      # long ITM: exercised
])
def test_a_single_leg_expiry_records_the_occ_event(tmp_path, side, expiry_close, expected):
    acct, ps, ctx = _account(
        tmp_path, f"exp{side.value}{int(expiry_close)}", expiry_close=expiry_close,
        contracts=[(_CALL_180, 180.0)], prem_bars=[_prem_bar(_CALL_180, "2024-03-06", 4.0, 180.0)])
    try:
        _open(acct, [_leg(_CALL_180, side, 180.0)],
              "long_call" if side == OrderDirection.BUY else "naked_call")
        _expire(acct, ps)
        row = _only(_option_rows(acct), _CALL_180)
        assert row["exit_reason"] == expected.value
        assert row["exit_record"]["trigger"] == expected.value
        assert row["exit_record"]["leg"] is None          # a settlement prices from no quote
        (txn,) = _option_txns()
        # The SAME value the live reconciler writes (expired_otm / assigned / exercised),
        # where this used to be "option_expiry" for all three.
        assert txn.close_reason == expected.value
    finally:
        ctx.__exit__(None, None, None)


def test_a_defined_risk_combo_records_each_legs_own_event(tmp_path):
    """A bull call spread settling BOTH legs ITM at expiry: the long leg is exercised, the
    short leg assigned -- per leg, as the OCC would report them -- and the transaction's
    close_reason is the most consequential of them (assigned > exercised > expired_otm, the
    live reconciler's rule; never ``option_expiry_combo``)."""
    acct, ps, ctx = _account(
        tmp_path, "combo", expiry_close=200.0,
        contracts=[(_CALL_180, 180.0), (_CALL_190, 190.0)],
        prem_bars=[_prem_bar(_CALL_180, "2024-03-06", 4.0, 180.0),
                   _prem_bar(_CALL_190, "2024-03-06", 1.5, 190.0)])
    try:
        _open(acct, [_leg(_CALL_180, OrderDirection.BUY, 180.0),
                     _leg(_CALL_190, OrderDirection.SELL, 190.0)], "bull_call_spread")
        _expire(acct, ps)
        rows = _option_rows(acct)
        assert _only(rows, _CALL_180)["exit_reason"] == OptionCloseReason.EXERCISED.value
        assert _only(rows, _CALL_190)["exit_reason"] == OptionCloseReason.ASSIGNED.value
        (txn,) = _option_txns()
        assert txn.close_reason == OptionCloseReason.ASSIGNED.value
    finally:
        ctx.__exit__(None, None, None)


def test_a_combo_with_one_leg_exercised_and_one_expired_closes_exercised(tmp_path):
    acct, ps, ctx = _account(
        tmp_path, "combo2", expiry_close=185.0,
        contracts=[(_CALL_180, 180.0), (_CALL_190, 190.0)],
        prem_bars=[_prem_bar(_CALL_180, "2024-03-06", 4.0, 180.0),
                   _prem_bar(_CALL_190, "2024-03-06", 1.5, 190.0)])
    try:
        _open(acct, [_leg(_CALL_180, OrderDirection.BUY, 180.0),
                     _leg(_CALL_190, OrderDirection.SELL, 190.0)], "bull_call_spread")
        _expire(acct, ps)
        rows = _option_rows(acct)
        assert _only(rows, _CALL_180)["exit_reason"] == "exercised"
        assert _only(rows, _CALL_190)["exit_reason"] == "expired_otm"
        (txn,) = _option_txns()
        assert txn.close_reason == "exercised"
    finally:
        ctx.__exit__(None, None, None)


def test_settlement_precedence_is_one_rule():
    from ba2_common.core.option_trade_record import settlement_close_reason
    assert settlement_close_reason(["expired_otm", "assigned", "exercised"]) == "assigned"
    assert settlement_close_reason(["expired_otm", OptionCloseReason.EXERCISED]) == "exercised"
    assert settlement_close_reason(["expired_otm"]) == "expired_otm"
    with pytest.raises(ValueError):
        settlement_close_reason(["stop_loss"])
    with pytest.raises(ValueError):
        settlement_close_reason([])


def test_a_margin_call_buyback_records_forced_liquidation(tmp_path):
    acct, ps, ctx = _account(
        tmp_path, "margin", expiry_close=170.0, contracts=[(_CALL_220, 220.0)],
        prem_bars=[_prem_bar(_CALL_220, "2024-03-06", 3.0, 220.0),
                   _prem_bar(_CALL_220, "2024-03-07", 3.0, 220.0)])
    try:
        _open(acct, [_leg(_CALL_220, OrderDirection.SELL, 220.0)], "naked_call")
        ps.set_clock(datetime(2024, 3, 7))
        acct._cash = -100_000.0                       # force the maintenance breach
        assert acct.maybe_margin_call_liquidation() is True
        row = _only(_option_rows(acct), _CALL_220)
        assert row["exit_reason"] == OptionCloseReason.FORCED_LIQUIDATION.value
        (txn,) = _option_txns()
        assert txn.close_reason == OptionCloseReason.FORCED_LIQUIDATION.value
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# a RULE-fired close: the trigger comes from the firing rule, not from the price
# ---------------------------------------------------------------------------
def _rule_close(acct, entry_order, triggers, rule_id=41, name="opt_sl"):
    """The close_option action exactly as the evaluator builds it for a firing rule."""
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator

    ev = TradeActionEvaluator.__new__(TradeActionEvaluator)
    ev.account = acct
    rec = SimpleNamespace(id=1, instance_id=None, recommended_action=OrderRecommendation.SELL)
    rule = SimpleNamespace(id=rule_id, name=name, triggers=triggers, actions={})
    action = ev._create_trade_action(
        ExpertActionType.CLOSE_OPTION, {"action_type": "close_option"}, "AAPL",
        OrderRecommendation.SELL, entry_order, rec, event_action=rule)
    action.submit_to_broker = True
    return action


def test_a_single_leg_stop_loss_close_is_recorded_stop_loss_not_take_profit(tmp_path):
    """THE DEFECT C3 names: a long call closed by the ``opt_sl`` stop. The close is a
    limit ticket with no stop price, so the price-proximity guess said ``take_profit``;
    the row now reads the firing rule's trigger, and the close order carries the leg as
    the close priced it (its quote, its spot, its DTE)."""
    acct, ps, ctx = _account(
        tmp_path, "sl", expiry_close=170.0, mid_close=170.0, contracts=[(_CALL_180, 180.0)],
        prem_bars=[_prem_bar(_CALL_180, "2024-03-06", 4.0, 180.0),
                   _prem_bar(_CALL_180, "2024-03-07", 1.0, 180.0),
                   _prem_bar(_CALL_180, "2024-03-08", 1.0, 180.0)])
    try:
        _open(acct, [_leg(_CALL_180, OrderDirection.BUY, 180.0)], "long_call")
        entry = next(o for o in acct.get_orders() if o.contract_symbol == _CALL_180)
        ps.set_clock(datetime(2024, 3, 7))
        action = _rule_close(acct, entry, {"c0": {"event_type": "profit_loss_percent",
                                                   "operator": "<", "value": -50}})
        result = action.execute()
        assert result["success"], result["message"]
        acct.refresh_orders()          # fills off the NEXT bar (03-08), next_bar_open
        acct.refresh_transactions()

        row = _only(_option_rows(acct), _CALL_180)
        assert row["exit_reason"] == OptionCloseReason.STOP_LOSS.value
        close = next(o for o in acct.get_orders()
                     if o.contract_symbol == _CALL_180 and o.side == OrderDirection.SELL)
        # What the retired guess says about the very same fill -- the C3 defect, measured.
        assert acct._exit_reason(close, row["exit_price"]) == "take_profit"

        record = close.data["exit_record"]
        assert record["version"] == OPTION_TRADE_RECORD_VERSION
        assert (record["trigger"], record["rule_id"], record["rule_name"]) == (
            "stop_loss", 41, "opt_sl")
        (leg,) = record["legs"]
        assert set(leg) == set(LEG_SNAPSHOT_FIELDS)
        assert leg["contract_symbol"] == _CALL_180 and leg["side"] == "sell"
        assert leg["position_intent"] == "sell_to_close"
        assert leg["spot"] == pytest.approx(170.0)
        assert leg["greeks_source"] == "bs_from_close"
        # The backtest's decision on bar 03-07 executes in N(03-07) = 03-08: 7 days left.
        assert leg["dte"] == (_EXPIRY - date(2024, 3, 8)).days
        assert leg["data_session"] == "2024-03-07"
        assert record["legs_without_quote"] == []
        # ... and the row carries that same leg snapshot.
        assert row["exit_record"]["leg"] == leg
    finally:
        ctx.__exit__(None, None, None)


def test_a_close_with_no_rule_behind_it_is_recorded_manual(tmp_path):
    from ba2_common.core.TradeActions import create_action

    acct, ps, ctx = _account(
        tmp_path, "manual", expiry_close=181.0, contracts=[(_CALL_180, 180.0)],
        prem_bars=[_prem_bar(_CALL_180, "2024-03-06", 4.0, 180.0),
                   _prem_bar(_CALL_180, "2024-03-07", 4.0, 180.0),
                   _prem_bar(_CALL_180, "2024-03-08", 4.0, 180.0)])
    try:
        _open(acct, [_leg(_CALL_180, OrderDirection.BUY, 180.0)], "long_call")
        entry = next(o for o in acct.get_orders() if o.contract_symbol == _CALL_180)
        ps.set_clock(datetime(2024, 3, 7))
        action = create_action(ExpertActionType.CLOSE_OPTION, "AAPL", acct,
                               OrderRecommendation.SELL, entry, SimpleNamespace(id=1))
        action.submit_to_broker = True
        assert action.execute()["success"]
        close = next(o for o in acct.get_orders()
                     if o.contract_symbol == _CALL_180 and o.side == OrderDirection.SELL)
        assert close.data["exit_record"]["trigger"] == OptionCloseReason.MANUAL.value
        assert close.data["exit_record"]["rule_id"] is None
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# C4: the trades JSON -- structure parts once, legs per row, equity untouched
# ---------------------------------------------------------------------------
def test_structure_level_parts_ride_the_first_leg_only(tmp_path):
    acct, ps, ctx = _account(
        tmp_path, "c4", expiry_close=170.0,
        contracts=[(_CALL_180, 180.0), (_CALL_190, 190.0)],
        prem_bars=[_prem_bar(_CALL_180, "2024-03-06", 4.0, 180.0),
                   _prem_bar(_CALL_190, "2024-03-06", 1.5, 190.0)])
    try:
        # A direct account submit writes no entry_record (only the shared action path does):
        # the row says so with None, it never invents one.
        _open(acct, [_leg(_CALL_180, OrderDirection.BUY, 180.0),
                     _leg(_CALL_190, OrderDirection.SELL, 190.0)], "bull_call_spread")
        parent = next(o for o in acct.get_orders()
                      if o.contract_symbol is None and o.option_strategy == "bull_call_spread")
        parent.data = {**(parent.data or {}), "entry_record": {
            "version": OPTION_TRADE_RECORD_VERSION, "legs": [
                {"contract_symbol": _CALL_180, "marker": "L180"},
                {"contract_symbol": _CALL_190, "marker": "S190"}],
            "legs_without_quote": [], "structure": {"strategy": "bull_call_spread"}}}
        _expire(acct, ps)
        rows = _option_rows(acct)
        head, tail = rows[0], rows[1]
        assert head["option_strategy"] == "bull_call_spread"
        assert "recommendation_confidence" in head      # None: no recommendation behind it
        assert head["entry_record"]["structure"] == {"strategy": "bull_call_spread"}
        assert head["entry_record"]["version"] == OPTION_TRADE_RECORD_VERSION
        assert "option_strategy" not in tail and "structure" not in tail["entry_record"]
        by_contract = {r["contract_symbol"]: r["entry_record"]["leg"]["marker"] for r in rows}
        assert by_contract == {_CALL_180: "L180", _CALL_190: "S190"}
        # Both legs expired OTM, each with its own exit record.
        assert [r["exit_record"]["trigger"] for r in rows] == ["expired_otm", "expired_otm"]
    finally:
        ctx.__exit__(None, None, None)


def test_an_equity_row_gains_no_key_and_keeps_its_price_proximity_label(tmp_path):
    """Equity rows are byte-identical: the SAME keys in the SAME order, and ``exit_reason``
    still from ``_exit_reason``."""
    from app.services.backtest.results import _trade_row

    acct, ps, ctx = _account(tmp_path, "eq", expiry_close=181.0, contracts=[], prem_bars=[])
    try:
        from ba2_common.core.models import TradingOrder
        from ba2_common.core.types import OrderStatus, OrderType
        acct.submit_order(TradingOrder(account_id=1, symbol="AAPL", quantity=10,
                                       side=OrderDirection.BUY, order_type=OrderType.MARKET,
                                       status=OrderStatus.PENDING))
        acct.refresh_orders(); acct.refresh_transactions()
        ps.set_clock(datetime(2024, 3, 7))
        acct.submit_order(TradingOrder(account_id=1, symbol="AAPL", quantity=10,
                                       side=OrderDirection.SELL, order_type=OrderType.MARKET,
                                       status=OrderStatus.PENDING,
                                       transaction_id=acct.get_orders()[0].transaction_id),
                          is_closing_order=True)
        ps.set_clock(datetime(2024, 3, 8))
        acct.refresh_orders(); acct.refresh_transactions()
        (trade,) = acct.get_round_trip_trades()
        assert list(trade) == [
            "symbol", "entry_time", "exit_time", "direction", "entry_price", "exit_price",
            "size", "multiplier", "pnl", "pnl_pct", "bars_held", "exit_reason",
            "contract_symbol", "underlying_symbol", "option_type", "strike", "expiry",
            "transaction_id"]
        assert trade["multiplier"] == 1 and isinstance(trade["multiplier"], int)
        assert trade["exit_reason"] == "exit"
        assert "entry_record" not in _trade_row(trade)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# the records ride only on PERSISTED runs: ``option_trade_records`` (output shape only)
# ---------------------------------------------------------------------------
_RECORD_KEYS = {"entry_record", "exit_record", "option_strategy", "recommendation_confidence"}


def _golden_leap_account(records: bool = True):
    """The O_LEAP golden fixture, run through the real engine (one DTE-exit round trip), on an
    account that builds the full option trade record (``records``) or only the trigger."""
    from tests.backtest import test_option_golden_run as g
    from tests.backtest.test_grid2_engine_paths import (
        _PlainBuyExpert, _harness, _launcher, _leap_rules)

    entry_rules, exit_rules = _leap_rules(_launcher(), dte_floor=g._DTE_FLOOR)
    engine, account, ctx = _harness(
        symbol=g._SYMBOL, underlying_rows=g._underlying_rows(), chain_rows=g._chain_rows(),
        bar_rows=g._bar_rows(), entry_rules=entry_rules, exit_rules=exit_rules,
        entry_action=entry_rules[0]["actions"][0],
        expert_factory=lambda eid: _PlainBuyExpert(eid, g._SYMBOL),
        start=g._START, end=g._END, account_id=g._ACCOUNT_ID)
    account._option_trade_records = records
    engine.run()
    return account, ctx


def test_a_fitness_trial_row_has_the_pre_record_shape_and_the_same_numbers():
    """``option_records=False`` (a GA fitness trial): no record key on any row, and every other
    key -- P&L, dates, the corrected ``exit_reason`` -- identical to the persisted form."""
    account, ctx = _golden_leap_account()
    try:
        full = account.get_round_trip_trades(option_records=True)
        lean = account.get_round_trip_trades(option_records=False)
        assert full and len(full) == len(lean)
        for f, l in zip(full, lean):
            assert not (_RECORD_KEYS & set(l))
            assert _RECORD_KEYS <= set(f)
            assert {k: v for k, v in f.items() if k not in _RECORD_KEYS} == l
        assert lean[0]["exit_reason"] == "dte_exit"
    finally:
        ctx.__exit__(None, None, None)


def test_the_flag_never_moves_the_fitness():
    from app.services.backtest.results import build_results
    from app.services.strategy_fitness import compute_fitness

    account, ctx = _golden_leap_account()
    try:
        base = {"initial_capital": 100_000.0, "start_date": "2024-01-02",
                "end_date": "2024-07-26", "account_settings": dict(account._cfg)}
        persisted = build_results(account, {**base, "option_trade_records": True})
        trial = build_results(account, {**base, "option_trade_records": False})
        assert _RECORD_KEYS <= set(persisted["trades"][0])
        assert not (_RECORD_KEYS & set(trial["trades"][0]))
        assert [t["pnl"] for t in persisted["trades"]] == [t["pnl"] for t in trial["trades"]]
        for metric in ("total_return", "sharpe_ratio", "car", "calmar_ratio", "profit_factor"):
            assert compute_fitness(metric, dict(persisted)) == compute_fitness(metric, dict(trial)), metric
    finally:
        ctx.__exit__(None, None, None)


def test_an_options_run_that_does_not_state_the_flag_is_refused():
    from app.services.backtest.results import build_results, require_option_trade_records

    with pytest.raises(ValueError, match="option_trade_records"):
        require_option_trade_records({})
    with pytest.raises(ValueError, match="True or False"):
        require_option_trade_records({"option_trade_records": 1})
    account, ctx = _golden_leap_account()
    try:
        with pytest.raises(ValueError, match="option_trade_records"):
            build_results(account, {"initial_capital": 100_000.0})
    finally:
        ctx.__exit__(None, None, None)


def test_run_daily_backtest_refuses_before_running_an_unstated_options_config(tmp_path):
    from app.services.backtest.daily_backtest_handler import run_daily_backtest

    with pytest.raises(ValueError, match="option_trade_records"):
        run_daily_backtest({"backtest_id": 1, "start_date": "2024-03-01",
                            "end_date": "2024-03-29", "enabled_instruments": ["AAPL"],
                            "experts": [], "initial_capital": 1000.0,
                            "account_settings": {}, "warmup_days": 0, "seed": 1,
                            "options_cache_db": str(tmp_path / "none.sqlite")})


def test_every_trial_config_states_the_flag_and_the_final_generation_records():
    """The knob rides the ``_build_daily_trial_config`` WHITELIST (a key missing there is dead),
    it has NO default, and the GA's final generation -- whose full results become the
    persisted top-N rows -- is switched to True by ``_maybe_mark_want_full``."""
    from app.services import strategy_optimization_handler as H

    cfg = {"backtest_id": 7, "start_date": "2024-02-01", "end_date": "2024-02-29",
           "enabled_instruments": ["AAPL"], "experts": [{"class": "X", "settings": {}}],
           "initial_capital": 1.0, "account_settings": {}, "warmup_days": 0, "seed": 1}
    decoded = {"expert_overrides": {}, "exit_rules": []}
    for flag in (True, False):
        assert H._build_daily_trial_config(cfg, decoded,
                                           option_trade_records=flag)["option_trade_records"] is flag
    with pytest.raises(TypeError):
        H._build_daily_trial_config(cfg, decoded)                  # no default
    with pytest.raises(TypeError):
        H._build_daily_trial_config(cfg, decoded, option_trade_records="yes")
    trial = H._build_daily_trial_config(cfg, decoded, option_trade_records=False)
    assert H._maybe_mark_want_full(trial, False) is trial
    last = H._maybe_mark_want_full(trial, True)
    assert last["option_trade_records"] is True and trial["option_trade_records"] is False


def test_a_multi_leg_stop_close_stamps_the_parent_and_every_leg_row_reads_it(tmp_path):
    """A spread closed by the ``opt_sl_ml`` stop (``loss_pct_of_max_loss >``): the exit record
    is written ONCE, on the closing ticket's PARENT (``_close_multi_leg``), the leg children
    carry none, and each leg's round-trip row finds it through ``_record_carrier`` -- trigger
    ``stop_loss``, with that leg's own exit snapshot."""
    bars = []
    for d in ("2024-03-06", "2024-03-07", "2024-03-08"):
        bars += [_prem_bar(_CALL_180, d, 4.0 if d == "2024-03-06" else 2.0, 180.0),
                 _prem_bar(_CALL_190, d, 1.5 if d == "2024-03-06" else 1.0, 190.0)]
    acct, ps, ctx = _account(tmp_path, "mlsl", expiry_close=170.0, mid_close=172.0,
                             contracts=[(_CALL_180, 180.0), (_CALL_190, 190.0)],
                             prem_bars=bars)
    try:
        _open(acct, [_leg(_CALL_180, OrderDirection.BUY, 180.0),
                     _leg(_CALL_190, OrderDirection.SELL, 190.0)], "bull_call_spread")
        entry = next(o for o in acct.get_orders()
                     if o.contract_symbol is None and o.option_strategy == "bull_call_spread")
        ps.set_clock(datetime(2024, 3, 7))
        action = _rule_close(acct, entry, {"c0": {"event_type": "loss_pct_of_max_loss",
                                                   "operator": ">", "value": 50}},
                             rule_id=77, name="opt_sl_ml")
        result = action.execute()
        assert result["success"], result["message"]
        acct.refresh_orders()
        acct.refresh_transactions()

        close_parent = next(o for o in acct.get_orders()
                            if o.contract_symbol is None and o.option_strategy == "close")
        record = close_parent.data["exit_record"]
        assert (record["trigger"], record["rule_id"]) == ("stop_loss", 77)
        assert {l["contract_symbol"] for l in record["legs"]} == {_CALL_180, _CALL_190}
        children = [o for o in acct.get_orders() if o.parent_order_id == close_parent.id]
        assert len(children) == 2
        assert all("exit_record" not in (c.data or {}) for c in children)

        rows = _option_rows(acct)
        for occ in (_CALL_180, _CALL_190):
            row = _only(rows, occ)
            assert row["exit_reason"] == "stop_loss"
            assert row["exit_record"]["leg"]["contract_symbol"] == occ
    finally:
        ctx.__exit__(None, None, None)


def test_a_non_recording_run_decides_and_scores_exactly_like_a_recording_one():
    """The GA-trial account builds NO leg snapshot and NO entry record (only the close
    trigger), and the run is the same run: identical rows (minus the record keys), identical
    curve, identical fitness."""
    from app.services.backtest.results import build_results
    from app.services.strategy_fitness import compute_fitness

    outs = {}
    for records in (True, False):
        account, ctx = _golden_leap_account(records)
        try:
            cfg = {"initial_capital": 100_000.0, "start_date": "2024-01-02",
                   "end_date": "2024-07-26", "account_settings": dict(account._cfg),
                   "option_trade_records": records}
            outs[records] = build_results(account, cfg)
            orders = account.get_orders()
            entries = [o for o in orders if "entry_record" in (o.data or {})]
            exits = [o.data["exit_record"] for o in orders if "exit_record" in (o.data or {})]
            assert exits and all(e["trigger"] == "dte_exit" for e in exits)
            if records:
                assert entries and all("legs" in e for e in exits)
            else:
                assert not entries and all(e.get("lean") is True and "legs" not in e
                                           for e in exits)
        finally:
            ctx.__exit__(None, None, None)
    full, lean = outs[True], outs[False]
    assert [{k: v for k, v in t.items() if k not in _RECORD_KEYS} for t in full["trades"]]         == lean["trades"]
    assert full["equity_curve"] == lean["equity_curve"]
    for metric in ("total_return", "sharpe_ratio", "car", "calmar_ratio", "profit_factor"):
        assert compute_fitness(metric, dict(full)) == compute_fitness(metric, dict(lean)), metric
