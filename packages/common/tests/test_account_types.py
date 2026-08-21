"""ba2_common.core.account_types: the four broker-seam value objects.

These are pure dataclasses with no DB/SDK deps. They exist so that no call site
has to guess whether get_account_info() handed it a pydantic object (Alpaca), a
dict (IBKR/TastyTrade) or None (Alpaca auth failure).
"""
from datetime import date

from ba2_common.core.account_types import (
    CASH_TRANSFER_DEPOSIT,
    CASH_TRANSFER_DIVIDEND,
    CASH_TRANSFER_WITHDRAWAL,
    MARGIN_SOURCE_ASSET,
    MARGIN_SOURCE_DEFAULT,
    AccountSnapshot,
    CashTransfer,
    MarginInfo,
    OrderImpact,
)


def test_account_snapshot_defaults_every_money_field_to_none():
    """An empty snapshot means "the broker told us nothing", never "zero".

    The caller must raise rather than substitute a default, so a 0.0 default here
    would silently authorise a plan against an account we know nothing about.
    """
    snap = AccountSnapshot()
    assert snap.cash is None
    assert snap.equity is None
    assert snap.net_liquidation is None
    assert snap.buying_power is None
    assert snap.non_marginable_buying_power is None
    assert snap.margin_multiplier is None
    assert snap.long_market_value is None
    assert snap.short_market_value is None
    assert snap.pending_transfer_in is None
    assert snap.is_margin_account is False
    assert snap.supports_fractional is False
    assert snap.raw == {}


def test_account_snapshot_raw_dicts_are_not_shared_between_instances():
    a = AccountSnapshot()
    b = AccountSnapshot()
    a.raw["x"] = 1
    assert b.raw == {}


def test_cash_transfer_deposit_is_income():
    ev = CashTransfer(external_id="act-1", event_date=date(2026, 8, 1),
                      event_type=CASH_TRANSFER_DEPOSIT, amount=1000.0)
    assert ev.is_income is True


def test_cash_transfer_dividend_is_income():
    ev = CashTransfer(external_id="act-3", event_date=date(2026, 8, 10),
                      event_type=CASH_TRANSFER_DIVIDEND, amount=12.34, symbol="AAPL")
    assert ev.is_income is True


def test_cash_transfer_withdrawal_is_not_income():
    ev = CashTransfer(external_id="act-2", event_date=date(2026, 8, 5),
                      event_type=CASH_TRANSFER_WITHDRAWAL, amount=-250.0)
    assert ev.is_income is False


def test_cash_transfer_zero_amount_deposit_is_not_income():
    """A zero-dollar deposit cannot fund an allocation run."""
    ev = CashTransfer(external_id="act-4", event_date=date(2026, 8, 6),
                      event_type=CASH_TRANSFER_DEPOSIT, amount=0.0)
    assert ev.is_income is False


def test_cash_transfer_negative_amount_deposit_is_not_income():
    """A reversed/returned deposit arrives as a DEPOSIT with a negative amount.

    This is the case the ``amount > 0`` guard exists for: without it, a clawback
    would be counted as new money to allocate.
    """
    ev = CashTransfer(external_id="act-5", event_date=date(2026, 8, 7),
                      event_type=CASH_TRANSFER_DEPOSIT, amount=-1000.0)
    assert ev.is_income is False


def test_cash_transfer_event_type_literals_are_the_persisted_spellings():
    """These strings go into portfolio_income_event.event_type, so they are a
    schema contract: respelling one orphans every row already stored under it."""
    assert CASH_TRANSFER_DEPOSIT == "DEPOSIT"
    assert CASH_TRANSFER_WITHDRAWAL == "WITHDRAWAL"
    assert CASH_TRANSFER_DIVIDEND == "DIVIDEND"


def test_margin_info_defaults_to_the_conservative_source():
    info = MarginInfo(symbol="AAPL", bp_factor=2.0)
    assert info.source == MARGIN_SOURCE_DEFAULT == "default"
    assert info.marginable is True
    assert info.fractionable is False
    assert info.min_order_size is None
    assert info.min_trade_increment is None


def test_margin_info_defaults_the_fractional_money_floor_to_unknown():
    """``None`` means "the broker published no floor", never 0.0 -- the same
    no-fabricated-values rule ``min_trade_increment`` follows."""
    assert MarginInfo(symbol="AAPL", bp_factor=1.0).min_fractional_notional is None


def test_margin_info_separates_a_share_minimum_from_a_money_minimum():
    """UNITS, and they are NOT interchangeable.

    ``min_order_size`` is a SHARE COUNT: it mirrors Alpaca's
    ``Asset.min_order_size`` and the engine compares it against a rounded share
    quantity (``portfolio_allocation.round_quantity`` -> ``qty < min_order_size``).
    ``min_fractional_notional`` is DOLLARS.

    TastyTrade publishes only the money one -- verbatim, from a live dry-run:
    ``below_notional_value_minimum: Fractional equities orders cannot have a
    notional value less than $5.`` Parking that 5.0 in ``min_order_size`` would be
    read by the engine as "5 SHARES": on a $34 ETF that suppresses every order
    below $170 instead of every order below $5, i.e. a 34x over-suppression that
    silently stops the account trading and looks like nothing at all.
    """
    info = MarginInfo(symbol="SCHD", bp_factor=1.0, fractionable=True,
                      min_fractional_notional=5.0)
    assert info.min_fractional_notional == 5.0
    assert info.min_order_size is None      # different number, different UNIT


def test_margin_info_records_where_the_factor_came_from():
    info = MarginInfo(symbol="AAPL", bp_factor=1.0, initial_margin_rate=0.5,
                      source=MARGIN_SOURCE_ASSET)
    assert info.source == MARGIN_SOURCE_ASSET
    assert info.initial_margin_rate == 0.5


def test_order_impact_bp_cost_flips_the_brokers_negative_buy_sign():
    """TastyTrade reports a BUY as a NEGATIVE change_in_buying_power. The engine
    consumes a POSITIVE cost, so bp_cost must flip the sign."""
    impact = OrderImpact(symbol="AAPL", change_in_buying_power=-1500.0)
    assert impact.bp_cost == 1500.0


def test_order_impact_bp_cost_is_zero_when_the_order_frees_buying_power():
    impact = OrderImpact(symbol="AAPL", change_in_buying_power=900.0)
    assert impact.bp_cost == 0.0


def test_order_impact_bp_cost_is_zero_at_exactly_zero_change():
    """The boundary of the ``< 0`` branch: a no-op order consumes nothing."""
    impact = OrderImpact(symbol="AAPL", change_in_buying_power=0.0)
    assert impact.bp_cost == 0.0


def test_order_impact_defaults_to_accepted_with_no_errors():
    impact = OrderImpact(symbol="AAPL", change_in_buying_power=-10.0)
    assert impact.accepted is True
    assert impact.warnings == []
    assert impact.errors == []
