"""E1b: the equity RM's budget excludes the collateral a cash-secured put ties up.

Plan 2026-09-25 option-stage2-build, "Added after the E1 review", E1b. A style run can hold a
CSP member AND a stock-buying member (O_CC / O_PP). The expert's used balance charges an open
short put only its premium (``MarketExpertInterface._calculate_used_balance``), so the equity RM
could spend the cash the put's collateral requires. ``ba2_common.core.option_collateral_budget
.rm_equity_budget`` -- the ONE function both runtimes' classic RM calls -- charges every
reserving option position the GREATER of its reserve and its used-balance charge, on the
expert's own term only:

    available = min(balances.available, virtual - used - extra)

so a broker figure that already nets the collateral (live Alpaca's Reg-T buying power, which
``balances.available`` is clamped to) is never charged it a second time.
"""
from types import SimpleNamespace

import pytest

from ba2_common.core import trade_store as ts
from ba2_common.core.interfaces.MarketExpertInterface import ExpertBalance
from ba2_common.core.interfaces.OptionsAccountInterface import ReservePool
from ba2_common.core.option_collateral_budget import rm_equity_budget


class _Account:
    """Answers the reserve pool for the transaction ids it is asked about."""

    def __init__(self, reserves, unmeasurable=()):
        self._reserves = reserves
        self._unmeasurable = unmeasurable
        self.asked = []

    def reserved_option_buying_power_detail(self, transaction_ids=None):
        self.asked.append(None if transaction_ids is None else set(transaction_ids))
        picked = {t: r for t, r in self._reserves.items()
                  if transaction_ids is None or t in transaction_ids}
        return ReservePool(total=sum(picked.values()), unmeasurable=self._unmeasurable,
                           by_transaction=tuple(sorted(picked.items())))


def test_no_open_option_position_returns_the_SAME_balances_and_asks_nothing():
    """Pure-equity runs (and every run with no option open): byte-identical, zero cost."""
    balances = ExpertBalance(virtual=20_000.0, used=5_000.0, available=15_000.0)
    account = _Account({})
    assert rm_equity_budget(balances, account) is balances
    assert account.asked == []


def test_a_csp_is_charged_its_collateral_not_its_premium():
    # virtual 100k; the CSP's used charge is its $200 premium; its reserve is 48 x 100 x 10
    balances = ExpertBalance(virtual=100_000.0, used=200.0, available=99_800.0,
                             option_used=((11, 200.0),))
    out = rm_equity_budget(balances, _Account({11: 48_000.0}))
    assert out.available == pytest.approx(100_000.0 - 48_000.0)
    assert out.used == pytest.approx(48_000.0)
    assert out.virtual == balances.virtual


def test_a_broker_figure_that_already_nets_the_collateral_is_not_charged_twice():
    """Live Alpaca: ``balances.available`` is already clamped to Reg-T buying power, which the
    broker reduced by the CSP's collateral. The expert term is the only one charged."""
    reg_t_bp = 100_000.0 + 200.0 - 48_000.0          # cash + premium - collateral
    balances = ExpertBalance(virtual=100_000.0, used=200.0, available=reg_t_bp,
                             option_used=((11, 200.0),))
    out = rm_equity_budget(balances, _Account({11: 48_000.0}))
    assert out.available == pytest.approx(min(reg_t_bp, 100_000.0 - 48_000.0))
    assert out.available == pytest.approx(52_000.0), "the collateral was subtracted twice"


def test_non_reserving_option_positions_leave_the_budget_unchanged():
    """A long call (debit) or covered call reserves nothing: the budget object is returned."""
    balances = ExpertBalance(virtual=50_000.0, used=900.0, available=49_100.0,
                             option_used=((5, 900.0),))
    assert rm_equity_budget(balances, _Account({})) is balances


def test_a_position_whose_used_charge_exceeds_its_reserve_is_not_credited_back():
    """``extra`` is per position and never negative: an expensive position cannot lend its
    excess to a CSP's collateral."""
    balances = ExpertBalance(virtual=100_000.0, used=9_000.0, available=91_000.0,
                             option_used=((1, 8_800.0), (2, 200.0)))
    out = rm_equity_budget(balances, _Account({1: 3_000.0, 2: 10_000.0}))
    assert out.available == pytest.approx(100_000.0 - 9_000.0 - (10_000.0 - 200.0))


def test_an_unreadable_reserve_refuses_to_size():
    balances = ExpertBalance(virtual=100_000.0, used=200.0, available=99_800.0,
                             option_used=((11, 200.0),))
    with pytest.raises(ValueError, match="reserve.*UNKNOWN|unknown"):
        rm_equity_budget(balances, _Account({}, unmeasurable=("order 3 (cash_secured_put)",)))


def test_an_account_that_cannot_report_reserves_refuses_to_size():
    balances = ExpertBalance(virtual=100_000.0, used=200.0, available=99_800.0,
                             option_used=((11, 200.0),))
    with pytest.raises(ValueError, match="reserve"):
        rm_equity_budget(balances, SimpleNamespace(id=4))


# ------------------------------------------------------------------------------------------- #
# the per-option-position used charge rides the breakdown
# ------------------------------------------------------------------------------------------- #
def test_the_used_balance_pass_reports_each_option_positions_own_charge():
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderDirection, TransactionStatus
    from tests.test_used_balance_options import _StubAccount, _StubExpert, _option_txn

    with ts.inmem_trades():
        account = _StubAccount(stock_price=150.0, quote=None)
        expert = _StubExpert(expert_id=1)
        opt_id = _option_txn(expert_id=1, open_price=2.0, quantity=3, multiplier=100.0,
                             side=OrderDirection.SELL)
        add_instance(Transaction(symbol="AAPL", quantity=10, side=OrderDirection.BUY,
                                 status=TransactionStatus.OPENED, expert_id=1,
                                 open_price=140.0, multiplier=None))
        per_option = {}
        used = expert._calculate_used_balance(account, option_used_out=per_option)
        assert per_option == {opt_id: pytest.approx(600.0)}
        assert used == pytest.approx(600.0 + 1400.0)
        # ...and the plain call is unchanged
        assert expert._calculate_used_balance(account) == used


def test_an_option_position_with_NO_open_price_yet_is_still_charged_its_reserve():
    """Review M6. Both runtimes create the WAITING transaction through the shared
    ``AccountInterface._create_transaction_for_order``, whose ``open_price`` is the option's
    quote mid, else the order's limit price, else None. With None (no quote at submit, a
    market order) the used pass skips the transaction -- and the budget then never saw its
    collateral. It is recorded with a used charge of 0 so the reserve is charged in full,
    identically wherever the quote happened to be missing."""
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderDirection, TransactionStatus
    from tests.test_used_balance_options import _StubAccount, _StubExpert

    with ts.inmem_trades():
        account = _StubAccount(stock_price=150.0, quote=None)
        expert = _StubExpert(expert_id=1)
        waiting_id = add_instance(Transaction(
            symbol="META", quantity=2, side=OrderDirection.SELL,
            status=TransactionStatus.WAITING, expert_id=1, open_price=None,
            multiplier=100.0))
        per_option = {}
        used = expert._calculate_used_balance(account, option_used_out=per_option)
        assert used == 0.0, "the used total itself is unchanged (the row is still skipped)"
        assert per_option == {waiting_id: 0.0}

    balances = ExpertBalance(virtual=100_000.0, used=0.0, available=100_000.0,
                             option_used=((waiting_id, 0.0),))
    out = rm_equity_budget(balances, _Account({waiting_id: 60_000.0}))
    assert out.available == pytest.approx(40_000.0)
