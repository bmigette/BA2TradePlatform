"""
Regression tests for the fixed bug B1 (options audit 2026-07-22): profit_loss_* conditions compared the UNDERLYING price against the option PREMIUM open_price.

Bug chain (all in packages/common/ba2_common/core), now fixed:
- TradeConditions.ProfitLossPercentCondition.evaluate / ProfitLossAmountCondition.evaluate
  called TradeCondition.get_current_price() -> account.get_instrument_current_price(
  self.instrument_name). For option positions the engine passes instrument_name =
  the UNDERLYING symbol (e.g. "AAPL"), so current_price was the share price (190).
- _get_pnl_via_transaction then called TransactionHelper.calculate_pnl(transaction,
  current_price), which compared that price against transaction.open_price — for an
  option transaction the PREMIUM (e.g. 4.20) — and applied no x100 contract multiplier.
- Result: a long option showed pnl% ~= (190-4.20)/4.20 ~= +4424% on first evaluation,
  so any profit_loss_percent > X take-profit exit fired immediately; profit_loss_amount
  was equally wrong (wrong instrument, no multiplier).

Fixed behavior: P&L of an option position is computed from the OPTION premium
(option quote vs. premium open_price: bid for long, ask for short, last as
fallback) with the contract multiplier applied to the amount — see
TradeConditions._get_option_pnl_via_transaction and
TransactionHelper.calculate_option_pnl. With the premium flat (or down, as with
the MockAccount canned quote), a +50% / +$50 take-profit must NOT fire. The
extra tests below cover the short direction, the bid->last fallback, the
missing-quote path, and calculate_option_pnl itself.
"""
from datetime import date

import pytest

from ba2_trade_platform.core.TradeConditions import (
    ProfitLossPercentCondition, ProfitLossAmountCondition,
)
from ba2_trade_platform.core.TransactionHelper import TransactionHelper
from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.core.option_types import OptionQuote
from ba2_trade_platform.core.types import (
    OrderDirection, OrderType, OrderStatus, TransactionStatus,
    AssetClass, OptionRight,
)
from tests.factories import create_transaction, create_trading_order

# Underlying share price served by MockAccount for "AAPL" in these tests.
UNDERLYING_PRICE = 190.0
# Premium actually paid for the option (transaction/order open_price).
ENTRY_PREMIUM = 4.20
# OCC contract symbol of the seeded long call.
CONTRACT_SYMBOL = "AAPL260116C00190000"


def _seed_long_option_position(mock_account, mock_expert_instance):
    """Seed an open long-call Transaction + its filled entry TradingOrder.

    The Transaction carries the PREMIUM as open_price (4.20) and multiplier=100,
    exactly as the option order path populates it; the entry order carries the
    option metadata (asset_class=OPTION, contract_symbol, ...). Returns the entry
    order (what the engine passes as existing_order to the conditions).
    """
    txn = create_transaction(
        symbol="AAPL", quantity=1, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=ENTRY_PREMIUM,
        multiplier=100, expert_id=mock_expert_instance.id,
    )
    entry_order = create_trading_order(
        account_id=mock_account.id, symbol="AAPL", quantity=1,
        side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT,
        status=OrderStatus.FILLED, filled_qty=1, open_price=ENTRY_PREMIUM,
        transaction_id=txn.id,
        asset_class=AssetClass.OPTION, contract_symbol=CONTRACT_SYMBOL,
        option_type=OptionRight.CALL, strike=190.0, expiry=date(2026, 1, 16),
        underlying_symbol="AAPL", multiplier=100,
    )
    return entry_order


def test_profit_loss_percent_long_option_flat_premium(
    mock_account, mock_expert_instance, sample_recommendation,
):
    """profit_loss_percent must use the option premium, not the underlying price.

    Setup: long 1 AAPL call opened at a $4.20 premium; AAPL shares trade at $190.
    The option premium has NOT moved up (MockAccount's canned option quote is
    last=2.10, i.e. flat-to-down vs. entry), so the true P&L is ~0% or negative —
    a "pnl% > 50" take-profit must NOT fire.

    Buggy behavior: the condition fetches the UNDERLYING price (190) and compares
    it against the $4.20 premium -> calculated_value ~= +4424% and evaluate() is
    True, so this test FAILS until B1 is fixed.
    """
    mock_account._prices["AAPL"] = UNDERLYING_PRICE
    entry_order = _seed_long_option_position(mock_account, mock_expert_instance)

    cond = ProfitLossPercentCondition(
        account=mock_account,
        instrument_name="AAPL",  # the engine passes the UNDERLYING for option positions
        expert_recommendation=sample_recommendation,
        operator_str=">",
        value=50.0,
        existing_order=entry_order,
    )

    result = cond.evaluate()

    # Correct behavior: premium flat/down -> no 50%+ gain -> condition must not fire.
    assert result is False, (
        f"profit_loss_percent > 50 fired on a flat option position; "
        f"calculated_value={cond.calculated_value} (underlying-vs-premium mixup?)"
    )
    # And the reported percentage must be premium-based and sane — not a
    # multi-thousand-percent artifact of comparing share price to premium.
    assert cond.calculated_value is not None
    assert abs(cond.calculated_value) < 100.0, (
        f"profit_loss_percent reported {cond.calculated_value:+.2f}% for an option "
        f"whose premium is flat; expected a premium-based value (roughly 0%, "
        f"well under 100% in magnitude)"
    )


def test_profit_loss_amount_long_option_flat_premium(
    mock_account, mock_expert_instance, sample_recommendation,
):
    """profit_loss_amount must be premium-based with the x100 contract multiplier.

    Setup as above: long 1 call at $4.20 premium. Correct amount =
    (current_premium - 4.20) * 100 * qty — i.e. ~$0 for a flat premium (or -$210
    with MockAccount's canned quote at 2.10): the position has NOT gained value,
    so a "pnl$ > 50" take-profit must NOT fire and the amount must be <= 0.

    Buggy behavior: amount = (190 - 4.20) * 1 = +185.80 (underlying price, no
    multiplier) -> evaluate() is True, so this test FAILS until B1 is fixed.
    """
    mock_account._prices["AAPL"] = UNDERLYING_PRICE
    entry_order = _seed_long_option_position(mock_account, mock_expert_instance)

    cond = ProfitLossAmountCondition(
        account=mock_account,
        instrument_name="AAPL",
        expert_recommendation=sample_recommendation,
        operator_str=">",
        value=50.0,
        existing_order=entry_order,
    )

    result = cond.evaluate()

    # The premium never rose above the $4.20 entry, so the position cannot be
    # up $50 — the take-profit must not fire...
    assert result is False, (
        f"profit_loss_amount > $50 fired on a flat option position; "
        f"calculated_value={cond.calculated_value} (underlying-vs-premium mixup?)"
    )
    # ...and the reported amount must not show a profit at all.
    assert cond.calculated_value is not None
    assert cond.calculated_value <= 0.0, (
        f"profit_loss_amount reported ${cond.calculated_value:+.2f} for an option "
        f"whose premium is flat/down; expected a premium-based amount with the "
        f"x100 multiplier (roughly $0, certainly not positive)"
    )


def _seed_short_option_position(mock_account, mock_expert_instance):
    """Seed an open short-call Transaction + its filled entry TradingOrder.

    Sell-to-open: transaction/order side SELL, premium credited as open_price,
    multiplier=100 — mirrors the live option order path.
    """
    txn = create_transaction(
        symbol="AAPL", quantity=1, side=OrderDirection.SELL,
        status=TransactionStatus.OPENED, open_price=ENTRY_PREMIUM,
        multiplier=100, expert_id=mock_expert_instance.id,
    )
    entry_order = create_trading_order(
        account_id=mock_account.id, symbol="AAPL", quantity=1,
        side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        status=OrderStatus.FILLED, filled_qty=1, open_price=ENTRY_PREMIUM,
        transaction_id=txn.id,
        asset_class=AssetClass.OPTION, contract_symbol=CONTRACT_SYMBOL,
        option_type=OptionRight.CALL, strike=190.0, expiry=date(2026, 1, 16),
        underlying_symbol="AAPL", multiplier=100,
    )
    return entry_order


def test_profit_loss_percent_short_option_marks_at_ask(
    mock_account, mock_expert_instance, sample_recommendation,
):
    """Short options must mark at the ASK (the cost to buy back), not the bid.

    Canned quote: bid=2.0, ask=2.2, last=2.1; entry premium 4.20. Correct
    premium-based P&L: (4.20 - 2.20) / 4.20 = +47.62% — profitable, but below
    50%, so "pnl% > 50" must NOT fire. Had the bid been used, the value would
    be (4.20 - 2.00) / 4.20 = +52.38% and the condition would wrongly fire.
    """
    mock_account._prices["AAPL"] = UNDERLYING_PRICE
    entry_order = _seed_short_option_position(mock_account, mock_expert_instance)

    cond = ProfitLossPercentCondition(
        account=mock_account,
        instrument_name="AAPL",
        expert_recommendation=sample_recommendation,
        operator_str=">",
        value=50.0,
        existing_order=entry_order,
    )

    result = cond.evaluate()

    assert result is False
    assert cond.calculated_value == pytest.approx(47.619, abs=0.001)


def test_profit_loss_amount_short_option_marks_at_ask(
    mock_account, mock_expert_instance, sample_recommendation,
):
    """Short option amount = (open_premium - ask) * |qty| * multiplier.

    (4.20 - 2.20) * 1 * 100 = +$200: the short gained value as the premium
    fell, so a "pnl$ > 50" take-profit MUST fire, at exactly $200 (the ask;
    the bid would give $220).
    """
    mock_account._prices["AAPL"] = UNDERLYING_PRICE
    entry_order = _seed_short_option_position(mock_account, mock_expert_instance)

    cond = ProfitLossAmountCondition(
        account=mock_account,
        instrument_name="AAPL",
        expert_recommendation=sample_recommendation,
        operator_str=">",
        value=50.0,
        existing_order=entry_order,
    )

    result = cond.evaluate()

    assert result is True
    assert cond.calculated_value == pytest.approx(200.0)


def test_profit_loss_long_option_bid_missing_falls_back_to_last(
    mock_account, mock_expert_instance, sample_recommendation, monkeypatch,
):
    """Long option with no bid in the quote marks at last.

    Quote last=2.10 -> amount = (2.10 - 4.20) * 1 * 100 = -$210: still no
    profit, so the "pnl$ > 50" take-profit must not fire.
    """
    monkeypatch.setattr(
        mock_account, "get_option_quote",
        lambda contract_symbol: OptionQuote(
            symbol=contract_symbol, bid=None, ask=2.2, last=2.1))
    mock_account._prices["AAPL"] = UNDERLYING_PRICE
    entry_order = _seed_long_option_position(mock_account, mock_expert_instance)

    cond = ProfitLossAmountCondition(
        account=mock_account,
        instrument_name="AAPL",
        expert_recommendation=sample_recommendation,
        operator_str=">",
        value=50.0,
        existing_order=entry_order,
    )

    result = cond.evaluate()

    assert result is False
    assert cond.calculated_value == pytest.approx(-210.0)


def test_profit_loss_option_missing_quote_declines_to_evaluate(
    mock_account, mock_expert_instance, sample_recommendation, monkeypatch,
):
    """No option quote -> decline: evaluate() is False and calculated_value
    stays None (fail-loud — never a fabricated premium)."""
    monkeypatch.setattr(
        mock_account, "get_option_quote", lambda contract_symbol: None)
    mock_account._prices["AAPL"] = UNDERLYING_PRICE
    entry_order = _seed_long_option_position(mock_account, mock_expert_instance)

    cond = ProfitLossPercentCondition(
        account=mock_account,
        instrument_name="AAPL",
        expert_recommendation=sample_recommendation,
        operator_str=">",
        value=50.0,
        existing_order=entry_order,
    )

    result = cond.evaluate()

    assert result is False
    assert cond.calculated_value is None


class TestCalculateOptionPnl:
    """Unit tests for TransactionHelper.calculate_option_pnl (the B1 helper)."""

    def test_long_option_scales_by_multiplier(self):
        txn = Transaction(
            symbol="AAPL", quantity=1, side=OrderDirection.BUY,
            open_price=4.20, multiplier=100,
        )
        pnl = TransactionHelper.calculate_option_pnl(txn, current_premium=5.20, multiplier=100)
        assert pnl["amount"] == pytest.approx(100.0)
        assert pnl["percent"] == pytest.approx(23.8095, abs=1e-4)

    def test_short_option_negates_direction(self):
        txn = Transaction(
            symbol="AAPL", quantity=1, side=OrderDirection.SELL,
            open_price=4.20, multiplier=100,
        )
        pnl = TransactionHelper.calculate_option_pnl(txn, current_premium=2.20, multiplier=100)
        assert pnl["amount"] == pytest.approx(200.0)
        assert pnl["percent"] == pytest.approx(47.619, abs=0.001)

    def test_missing_premium_returns_none(self):
        txn = Transaction(
            symbol="AAPL", quantity=1, side=OrderDirection.BUY,
            open_price=4.20, multiplier=100,
        )
        assert TransactionHelper.calculate_option_pnl(txn, current_premium=None, multiplier=100) is None

    def test_missing_multiplier_returns_none(self):
        txn = Transaction(
            symbol="AAPL", quantity=1, side=OrderDirection.BUY,
            open_price=4.20, multiplier=100,
        )
        assert TransactionHelper.calculate_option_pnl(txn, current_premium=5.20, multiplier=None) is None
