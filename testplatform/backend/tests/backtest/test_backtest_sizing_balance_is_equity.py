"""Finding 6, FIXED 2026-10-07: the backtest's SIZING balance is its EQUITY, as live's is.

``BacktestAccount.get_balance()`` stays the cash ledger. Every sizing / threshold / ceiling reader
starts from ``_plain_balance`` (``get_tradable_balance``, ``get_option_tradable_balance``, the margin
ceiling, the capital mapping), and that is the deployed equity here. The buying-power clamp the
expert applies reads the snapshot's ``buying_power`` (cash, floored at 0), so cash cannot go negative.

Hermetic: a hand-built price series, no network.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from tests.backtest.test_backtest_account_contract import _acct


def _with_a_winning_position(acct, ps, *, cash=90_000.0, qty=100.0, avg=100.0, mark_day=datetime(2024, 1, 5)):
    """100 shares bought at 100 (cash 90,000 after the 10,000 purchase), marked at 107."""
    from app.services.backtest.backtest_account import _Position

    ps.set_clock(mark_day)
    acct._cash = cash
    acct._positions["AAPL"] = _Position(symbol="AAPL", qty=qty, avg_price=avg)
    acct._bump_option_memo()


def test_flat_account_cash_equals_equity_so_nothing_moves_when_flat():
    acct, ctx, _ps = _acct()
    try:
        assert acct.get_balance() == 100_000.0
        assert acct._plain_balance() == 100_000.0
        assert acct.get_tradable_balance() == 100_000.0
        assert acct.get_option_tradable_balance() == 100_000.0
        assert acct.get_tradable_equity() == 100_000.0
    finally:
        ctx.__exit__(None, None, None)


def test_invested_account_sizes_from_equity_while_get_balance_stays_cash():
    acct, ctx, ps = _acct()
    try:
        _with_a_winning_position(acct, ps)
        equity = 90_000.0 + 100.0 * 107.0                       # 100,700
        assert acct.get_balance() == pytest.approx(90_000.0)    # the cash ledger is untouched
        assert acct._plain_balance() == pytest.approx(equity)
        # every reader of the seam sees the SAME base
        assert acct.get_tradable_balance() == pytest.approx(equity)
        assert acct.get_option_tradable_balance() == pytest.approx(equity)      # audit item 7
        assert acct.get_tradable_equity() == pytest.approx(equity)
        assert acct.get_tradable_balance() == pytest.approx(acct.get_tradable_equity())
    finally:
        ctx.__exit__(None, None, None)


def test_the_equity_cap_still_bounds_the_sizing_balance():
    """``deployed_equity`` = min(cap, equity): the cap is what may be spent, never ``true_equity``."""
    acct, ctx, ps = _acct()
    try:
        _with_a_winning_position(acct, ps)
        acct._equity_cap = 95_000.0
        assert acct._plain_balance() == pytest.approx(95_000.0)
        assert acct.true_equity() == pytest.approx(100_700.0)
    finally:
        ctx.__exit__(None, None, None)


def test_the_buying_power_clamp_still_bounds_spending_by_cash():
    """An equity base above cash (an unrealised gain) cannot fund more than the cash on hand: the
    snapshot's buying power is cash, so cash can never go negative without margin."""
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

    acct, ctx, ps = _acct()
    try:
        _with_a_winning_position(acct, ps, cash=3_000.0, qty=100.0, avg=100.0)   # equity 13,700
        assert acct._plain_balance() == pytest.approx(13_700.0)
        assert MarketExpertInterface._get_actual_available_balance(acct) == pytest.approx(3_000.0)
        # and an overdrawn ledger floors at zero, never negative buying power
        acct._cash = -50.0
        assert MarketExpertInterface._get_actual_available_balance(acct) == 0.0
    finally:
        ctx.__exit__(None, None, None)


def test_the_option_buying_power_gate_is_on_the_equity_base():
    """``check_option_buying_power`` (``available_option_buying_power``) read cash in a backtest and
    equity live. Equity 100,700, cash 90,000, a 95,000 reserve: refused on cash, allowed on equity."""
    acct, ctx, ps = _acct()
    try:
        _with_a_winning_position(acct, ps)
        assert acct.available_option_buying_power() == pytest.approx(100_700.0)   # no reserves open
        assert acct.check_option_buying_power(95_000.0) is True
        assert acct.check_option_buying_power(100_701.0) is False
    finally:
        ctx.__exit__(None, None, None)


def test_only_the_backtest_account_overrides_the_seam():
    """The one override lives here (live adapters keep the shared implementation: see
    tests/test_sizing_balance_live_unchanged.py)."""
    import inspect
    from app.services.backtest.backtest_account import BacktestAccount
    from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
    assert BacktestAccount._plain_balance is not ReadOnlyAccountInterface._plain_balance
    assert "deployed_equity" in inspect.getsource(BacktestAccount._plain_balance)
