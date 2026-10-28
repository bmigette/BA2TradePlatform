"""overalloc_min_res_pct: on an over-allocated account (virtual_equity_pct summed past 100 %), the
OTHER experts may not spend the undeployed part of an expert's reserved account share."""
import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

from tests import factories
from tests.test_available_balance_clamp import _BalanceExpert, _FakeAccount


class _Expert(_BalanceExpert):
    def __init__(self, id_val, reserve_pct):
        super().__init__(id_val)
        self._reserve = reserve_pct

    @property
    def settings(self):
        return {"overalloc_min_res_pct": self._reserve}


def _run(account, experts, reader):
    from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver

    class _R:
        def get_account_instance(self, account_id):
            return account

        def get_expert_instance(self, expert_id):
            return experts[expert_id]

    prev = get_instance_resolver()
    try:
        set_instance_resolver(_R())
        return reader()
    finally:
        set_instance_resolver(prev)


def _setup(reserve_pct, buying_power, *, reserver_enabled=True):
    acct = factories.create_account_definition()
    a = factories.create_expert_instance(account_id=acct.id, expert="_BalanceExpert",
                                         virtual_equity_pct=100.0)
    b = factories.create_expert_instance(account_id=acct.id, expert="_BalanceExpert",
                                         virtual_equity_pct=50.0, enabled=reserver_enabled)
    experts = {a.id: _Expert(a.id, 0.0), b.id: _Expert(b.id, reserve_pct)}
    account = _FakeAccount(acct.id, balance=100_000.0, account_info={"buying_power": buying_power})
    return account, experts, a.id, b.id


@pytest.mark.usefixtures("reset_test_db")
def test_other_expert_cannot_spend_the_reserve():
    account, experts, a, b = _setup(15.0, buying_power=40_000.0)
    assert _run(account, experts, experts[a].get_available_balance) == pytest.approx(25_000.0)


@pytest.mark.usefixtures("reset_test_db")
def test_reserve_never_limits_its_owner():
    account, experts, a, b = _setup(15.0, buying_power=40_000.0)
    assert _run(account, experts, experts[b].get_available_balance) == pytest.approx(40_000.0)


@pytest.mark.usefixtures("reset_test_db")
def test_zero_reserve_and_disabled_reserver_change_nothing():
    account, experts, a, _ = _setup(0.0, buying_power=40_000.0)
    assert _run(account, experts, experts[a].get_available_balance) == pytest.approx(40_000.0)
    account, experts, a, _ = _setup(15.0, buying_power=40_000.0, reserver_enabled=False)
    assert _run(account, experts, experts[a].get_available_balance) == pytest.approx(40_000.0)


@pytest.mark.usefixtures("reset_test_db")
def test_reserve_capped_at_the_reserver_slice_and_out_of_range_refused():
    account, experts, a, b = _setup(80.0, buying_power=90_000.0)   # b's slice is 50 %
    assert _run(account, experts, experts[a].get_available_balance) == pytest.approx(40_000.0)
    experts[b]._reserve = 150.0
    assert _run(account, experts, experts[a].get_available_balance) is None


def test_builtin_setting_defaults_to_zero():
    MarketExpertInterface._ensure_builtin_settings()
    assert MarketExpertInterface._builtin_settings["overalloc_min_res_pct"]["default"] == 0.0


class _MarginAccount(_FakeAccount):
    """Margin on: the platform ceiling (tradable balance) is below the broker's buying power."""

    def __init__(self, id_val, headroom, **kw):
        super().__init__(id_val, **kw)
        self._headroom = headroom

    def get_tradable_balance(self):
        return 150_000.0    # balance 100k x margin_factor 1.5

    def get_stock_exposure_headroom(self):
        return self._headroom


@pytest.mark.usefixtures("reset_test_db")
def test_reserve_also_binds_the_margin_ceiling():
    """Audit 2026-10-28 H-1: buying power (90k - 30k reserved = 60k) leaves room, but the ceiling
    headroom (50k) must lose the other expert's reserve too, or the reserve is spent through it."""
    acct = factories.create_account_definition()
    a = factories.create_expert_instance(account_id=acct.id, expert="_BalanceExpert",
                                         virtual_equity_pct=100.0)
    b = factories.create_expert_instance(account_id=acct.id, expert="_BalanceExpert",
                                         virtual_equity_pct=50.0)
    experts = {a.id: _Expert(a.id, 0.0), b.id: _Expert(b.id, 20.0)}    # b reserves 20% of 150k
    account = _MarginAccount(acct.id, headroom=50_000.0, balance=100_000.0,
                             account_info={"buying_power": 90_000.0})
    assert _run(account, experts, experts[a.id].get_available_balance) == pytest.approx(20_000.0)
    assert _run(account, experts, experts[b.id].get_available_balance) == pytest.approx(50_000.0)
