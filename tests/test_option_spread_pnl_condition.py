"""Regression tests for bug B9 (options audit follow-up, 2026-07-24): profit_loss_*
conditions on a MULTI-LEG option structure fell through to the legacy equity path.

Bug chain (packages/common/ba2_common/core/TradeConditions.py):
- _get_pnl_for_condition only routed option orders WITH a contract_symbol to the
  option-aware path. A multi-leg PARENT order (asset_class=OPTION, no contract_symbol —
  the contract lives on each child leg) fell through to the legacy equity path.
- The legacy path compared the UNDERLYING share price (~$190) against the parent's
  NET-PREMIUM open_price (~$3.75 debit / -$2.50 credit), producing astronomic
  percentages: ~+4,900% for a debit parent (any TP fired on the FIRST evaluation —
  runtime proof: backtest run 759, 8/13 structures closed after 1 bar at a real
  +0.7..+11.7% against a 75% TP threshold) and ~-7,500% for a credit parent (TP could
  never fire; any stop-loss fired instantly).

Fixed behavior: _get_spread_pnl_via_transaction prices the STRUCTURE — net current
premium = sum(sign x leg_premium x leg_ratio), each leg marked from the account's
option quote exactly like the single-leg path (long legs at bid, short at ask, last
fallback) — against the parent's entry net premium (sign normalised via the parent
side). A debit spread's TP fires at the real structure gain; a credit spread's TP is
"% of credit captured" and its SL "% of credit lost".
"""
from datetime import date

import pytest

from ba2_trade_platform.core.TradeConditions import (
    ProfitLossPercentCondition, ProfitLossAmountCondition,
)
from ba2_trade_platform.core.option_types import OptionQuote
from ba2_trade_platform.core.types import (
    OrderDirection, OrderType, OrderStatus, TransactionStatus,
    AssetClass, OptionRight,
)
from tests.factories import create_transaction, create_trading_order

EXPIRY = date(2026, 1, 16)
LONG_CALL = "AAPL260116C00190000"
SHORT_CALL = "AAPL260116C00195000"
FAR_CALL = "AAPL260116C00200000"
SHORT_PUT = "AAPL260116P00180000"


def _set_leg_quotes(mock_account, quotes):
    """Per-contract canned quotes: {contract_symbol: (bid, ask, last)}."""
    def _quote(contract_symbol):
        bid, ask, last = quotes[contract_symbol]
        return OptionQuote(symbol=contract_symbol, bid=bid, ask=ask, last=last,
                           implied_volatility=0.30, delta=0.5, gamma=0.02,
                           theta=-0.03, vega=0.1)
    mock_account.get_option_quote = _quote


def _seed_spread(mock_account, mock_expert_instance, *, side, open_price, legs,
                 structures=2, option_strategy="test_spread"):
    """Seed an open multi-leg Transaction + parent order (NO contract_symbol) + FILLED
    child legs linked via parent_order_id. ``legs``:
    [(contract, side, qty_ratio, entry_premium)] — the engine records each leg's fill
    premium on the leg order, so the seeded legs carry it too (the structure P&L prices
    realized cash from it)."""
    txn = create_transaction(
        symbol="AAPL", quantity=structures, side=side,
        status=TransactionStatus.OPENED, open_price=open_price,
        multiplier=100, expert_id=mock_expert_instance.id,
    )
    parent = create_trading_order(
        account_id=mock_account.id, symbol="AAPL", quantity=structures,
        side=side, order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        filled_qty=structures, open_price=open_price, transaction_id=txn.id,
        asset_class=AssetClass.OPTION, contract_symbol=None,
        option_strategy=option_strategy, underlying_symbol="AAPL", multiplier=100,
    )
    for contract, leg_side, ratio, entry_premium in legs:
        right = OptionRight.CALL if "C" in contract[10:] else OptionRight.PUT
        create_trading_order(
            account_id=mock_account.id, symbol="AAPL",
            quantity=structures * ratio, side=leg_side, order_type=OrderType.MARKET,
            status=OrderStatus.FILLED, filled_qty=structures * ratio,
            open_price=entry_premium,
            transaction_id=txn.id, parent_order_id=parent.id,
            asset_class=AssetClass.OPTION, contract_symbol=contract,
            option_type=right, strike=190.0, expiry=EXPIRY,
            underlying_symbol="AAPL", multiplier=100,
        )
    return parent


def _pct_cond(mock_account, rec, parent, op, value):
    return ProfitLossPercentCondition(
        account=mock_account, instrument_name="AAPL",  # engine passes the UNDERLYING
        expert_recommendation=rec, operator_str=op, value=value,
        existing_order=parent,
    )


# --- debit spread (BUY parent, open_price = net debit per structure) ----------------

def _debit_spread(mock_account, mock_expert_instance):
    # Bull call spread, 2 structures, net debit $3.75/structure (long 4.50 - short 0.75).
    return _seed_spread(
        mock_account, mock_expert_instance,
        side=OrderDirection.BUY, open_price=3.75,
        legs=[(LONG_CALL, OrderDirection.BUY, 1, 4.50),
              (SHORT_CALL, OrderDirection.SELL, 1, 0.75)],
        option_strategy="bull_call_spread",
    )


def test_debit_spread_tp_ignores_underlying_and_stays_put_below_threshold(
    mock_account, mock_expert_instance, sample_recommendation,
):
    """Structure +20% (net 4.50 vs 3.75 debit): a >50% TP must NOT fire — and the
    reported value must be the STRUCTURE's +20%, not the old ~+4,900% underlying-vs-
    premium artifact (underlying is at 190 the whole time)."""
    mock_account._prices["AAPL"] = 190.0
    parent = _debit_spread(mock_account, mock_expert_instance)
    _set_leg_quotes(mock_account, {LONG_CALL: (5.50, 5.60, 5.55),
                                   SHORT_CALL: (0.95, 1.00, 0.97)})

    cond = _pct_cond(mock_account, sample_recommendation, parent, ">", 50.0)
    assert cond.evaluate() is False
    assert cond.calculated_value == pytest.approx(20.0, abs=0.01)


def test_debit_spread_tp_fires_at_real_structure_gain(
    mock_account, mock_expert_instance, sample_recommendation,
):
    """Net premium doubles (7.50 vs 3.75) -> +100%: TP >50 fires, TP >150 does not;
    amount = (7.50-3.75) x 2 structures x 100 = $750."""
    mock_account._prices["AAPL"] = 190.0
    parent = _debit_spread(mock_account, mock_expert_instance)
    _set_leg_quotes(mock_account, {LONG_CALL: (8.50, 8.60, 8.55),
                                   SHORT_CALL: (0.95, 1.00, 0.97)})

    assert _pct_cond(mock_account, sample_recommendation, parent, ">", 50.0).evaluate() is True
    cond = _pct_cond(mock_account, sample_recommendation, parent, ">", 150.0)
    assert cond.evaluate() is False
    assert cond.calculated_value == pytest.approx(100.0, abs=0.01)

    amt = ProfitLossAmountCondition(
        account=mock_account, instrument_name="AAPL",
        expert_recommendation=sample_recommendation, operator_str=">", value=700.0,
        existing_order=parent,
    )
    assert amt.evaluate() is True
    assert amt.calculated_value == pytest.approx(750.0, abs=0.01)


# --- credit spread (SELL parent, open_price = net credit per structure) -------------

def _credit_spread(mock_account, mock_expert_instance, open_price=-2.50):
    # Short strangle, 2 structures, net credit $2.50/structure (1.25 + 1.25).
    return _seed_spread(
        mock_account, mock_expert_instance,
        side=OrderDirection.SELL, open_price=open_price,
        legs=[(SHORT_PUT, OrderDirection.SELL, 1, 1.25),
              (FAR_CALL, OrderDirection.SELL, 1, 1.25)],
        option_strategy="short_strangle",
    )


def test_credit_spread_tp_fires_at_half_credit_decay(
    mock_account, mock_expert_instance, sample_recommendation,
):
    """Premiums halve (net -1.25 vs -2.50 credit) -> +50% of credit captured: TP >40
    fires, TP >60 does not. (Pre-fix the SELL parent computed ~-7,500% and could
    NEVER take profit.)"""
    mock_account._prices["AAPL"] = 190.0
    parent = _credit_spread(mock_account, mock_expert_instance)
    _set_leg_quotes(mock_account, {SHORT_PUT: (0.70, 0.75, 0.72),
                                   FAR_CALL: (0.45, 0.50, 0.47)})

    assert _pct_cond(mock_account, sample_recommendation, parent, ">", 40.0).evaluate() is True
    cond = _pct_cond(mock_account, sample_recommendation, parent, ">", 60.0)
    assert cond.evaluate() is False
    assert cond.calculated_value == pytest.approx(50.0, abs=0.01)


def test_credit_spread_sl_fires_when_premium_doubles(
    mock_account, mock_expert_instance, sample_recommendation,
):
    """Premiums double (net -5.00 vs -2.50 credit) -> -100% of credit: SL <-50 fires.
    open_price stored ABSOLUTE (2.50) here — the sign normalisation via the parent's
    SELL side must price it identically to a stored -2.50."""
    mock_account._prices["AAPL"] = 190.0
    parent = _credit_spread(mock_account, mock_expert_instance, open_price=2.50)
    _set_leg_quotes(mock_account, {SHORT_PUT: (2.90, 3.00, 2.95),
                                   FAR_CALL: (1.90, 2.00, 1.95)})

    cond = _pct_cond(mock_account, sample_recommendation, parent, "<", -50.0)
    assert cond.evaluate() is True
    assert cond.calculated_value == pytest.approx(-100.0, abs=0.01)


# --- ratio'd structure (1-2-1 butterfly) ---------------------------------------------

def test_butterfly_ratio_legs_are_weighted(
    mock_account, mock_expert_instance, sample_recommendation,
):
    """1-2-1 call butterfly, 1 structure, net debit $1.50: current net =
    5.00 - 2x1.50 + 0.60 = 2.60 -> +73.33%. The x2 body leg must count twice."""
    mock_account._prices["AAPL"] = 190.0
    parent = _seed_spread(
        mock_account, mock_expert_instance,
        side=OrderDirection.BUY, open_price=1.50, structures=1,
        legs=[(LONG_CALL, OrderDirection.BUY, 1, 5.00),
              (SHORT_CALL, OrderDirection.SELL, 2, 2.00),
              (FAR_CALL, OrderDirection.BUY, 1, 0.50)],
        option_strategy="call_butterfly",
    )
    _set_leg_quotes(mock_account, {LONG_CALL: (5.00, 5.10, 5.05),
                                   SHORT_CALL: (1.45, 1.50, 1.47),
                                   FAR_CALL: (0.60, 0.65, 0.62)})

    cond = _pct_cond(mock_account, sample_recommendation, parent, ">", 50.0)
    assert cond.evaluate() is True
    assert cond.calculated_value == pytest.approx(73.33, abs=0.01)


# --- decline paths (no fabricated numbers) -------------------------------------------

def test_spread_without_filled_legs_declines(
    mock_account, mock_expert_instance, sample_recommendation,
):
    """A parent with no filled child legs cannot be priced -> condition declines
    (False, calculated_value None) instead of falling back to the underlying price."""
    txn = create_transaction(
        symbol="AAPL", quantity=2, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=3.75,
        multiplier=100, expert_id=mock_expert_instance.id,
    )
    parent = create_trading_order(
        account_id=mock_account.id, symbol="AAPL", quantity=2,
        side=OrderDirection.BUY, order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        filled_qty=2, open_price=3.75, transaction_id=txn.id,
        asset_class=AssetClass.OPTION, contract_symbol=None,
        underlying_symbol="AAPL", multiplier=100,
    )
    cond = _pct_cond(mock_account, sample_recommendation, parent, ">", 50.0)
    assert cond.evaluate() is False
    assert cond.calculated_value is None


def test_even_money_structure_declines(
    mock_account, mock_expert_instance, sample_recommendation,
):
    """Net entry premium ~0 (even-money structure): the percent basis is undefined —
    decline rather than divide by ~zero."""
    parent = _seed_spread(
        mock_account, mock_expert_instance,
        side=OrderDirection.SELL, open_price=0.0,
        legs=[(SHORT_PUT, OrderDirection.SELL, 1, 1.25),
              (FAR_CALL, OrderDirection.SELL, 1, 1.25)],
    )
    _set_leg_quotes(mock_account, {SHORT_PUT: (0.70, 0.75, 0.72),
                                   FAR_CALL: (0.45, 0.50, 0.47)})
    cond = _pct_cond(mock_account, sample_recommendation, parent, ">", 1.0)
    assert cond.evaluate() is False
    assert cond.calculated_value is None


# --- partially-closed structures (B10 follow-through) --------------------------------

def _close_leg_individually(mock_account, txn_id, contract, side_closed, qty, premium):
    """A STANDALONE single-leg close fill riding the same transaction — the shape the
    margin-liquidation buy-back and close_option_position both produce (NO
    parent_order_id)."""
    right = OptionRight.CALL if "C" in contract[10:] else OptionRight.PUT
    return create_trading_order(
        account_id=mock_account.id, symbol="AAPL",
        quantity=qty, side=side_closed, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, filled_qty=qty, open_price=premium,
        transaction_id=txn_id, parent_order_id=None,
        asset_class=AssetClass.OPTION, contract_symbol=contract,
        option_type=right, strike=190.0, expiry=EXPIRY,
        underlying_symbol="AAPL", multiplier=100,
    )


def test_partially_closed_structure_prices_realized_plus_remainder(
    mock_account, mock_expert_instance, sample_recommendation,
):
    """Strangle ($2.50 credit, 2 structures) whose CALL leg was bought back individually
    at 0.50: the condition must price REALIZED (call +0.75/contract) + REMAINING (put
    still held, marked to flatten), not treat the closed call as still short.

    cash = +2x1.25 (put) + 2x1.25 (call) - 2x0.50 (buy-back) = +4.00; put ask now 0.25
    -> flatten -0.50; amount = 3.50 x 100 = $350 on the $500 basis = +70%. (The stale
    all-legs formula would read the flat call as still held and report a different
    number.)"""
    mock_account._prices["AAPL"] = 190.0
    parent = _credit_spread(mock_account, mock_expert_instance)
    _close_leg_individually(mock_account, parent.transaction_id, FAR_CALL,
                            OrderDirection.BUY, qty=2, premium=0.50)
    _set_leg_quotes(mock_account, {SHORT_PUT: (0.20, 0.25, 0.22),
                                   FAR_CALL: (0.05, 0.10, 0.07)})

    cond = _pct_cond(mock_account, sample_recommendation, parent, ">", 65.0)
    assert cond.evaluate() is True
    assert cond.calculated_value == pytest.approx(70.0, abs=0.01)


def test_fully_flat_structure_declines(
    mock_account, mock_expert_instance, sample_recommendation,
):
    """Both legs closed individually -> nothing left to manage: decline (False, None)
    rather than pricing a phantom position."""
    mock_account._prices["AAPL"] = 190.0
    parent = _credit_spread(mock_account, mock_expert_instance)
    _close_leg_individually(mock_account, parent.transaction_id, FAR_CALL,
                            OrderDirection.BUY, qty=2, premium=0.50)
    _close_leg_individually(mock_account, parent.transaction_id, SHORT_PUT,
                            OrderDirection.BUY, qty=2, premium=0.25)
    _set_leg_quotes(mock_account, {SHORT_PUT: (0.20, 0.25, 0.22),
                                   FAR_CALL: (0.05, 0.10, 0.07)})

    cond = _pct_cond(mock_account, sample_recommendation, parent, ">", 1.0)
    assert cond.evaluate() is False
    assert cond.calculated_value is None
