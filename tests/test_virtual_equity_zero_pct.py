"""A sleeve deliberately allocated 0% must get 0% — not the whole account.

``ExpertInstance.virtual_equity_pct`` is ``float = Field(default=100.0)``: NOT NULL,
so it can never arrive as None. Every ``or 100.0`` / ``if pct else 100.0`` guarding
it can therefore only ever fire on a REAL, user-entered ``0`` — and it turns "give
this expert nothing" into "give this expert the entire account". The Settings page
takes the value from a free-text input (``float(self.virtual_equity_input.value)``)
with no lower bound, so 0 is reachable in one keystroke.

Four clones of the same coercion, all fixed together (a survivor is a live defect):

  * ``MarketExpertInterface.get_virtual_balance``      -- sizes every order
  * ``SmartRiskManagerQueue._execute_task`` (x2)       -- the equity the risk manager
                                                          reasons about, start and end
  * ``BalanceUsagePerExpertChart``                     -- what the operator SEES

Each is paired with its inverse: an ordinary non-zero percentage must be unchanged.
"""
import pytest
from types import SimpleNamespace

from tests.conftest import MockAccount, MockExpert
from tests.factories import create_account_definition, create_expert_instance
from ba2_trade_platform.core.models import ExpertInstance
from ba2_trade_platform.core.db import get_instance, update_instance


class _StubResolver:
    """Instance resolver handing back one canned account."""

    def __init__(self, account):
        self._account = account

    def get_account_instance(self, account_id):
        return self._account

    def get_expert_instance(self, expert_id):
        raise NotImplementedError

    def get_account_instance_from_transaction(self, transaction):
        raise NotImplementedError


def _expert_at(monkeypatch, pct, balance=100_000.0):
    acct_def = create_account_definition()
    account = MockAccount(acct_def.id)
    account._balance = balance
    expert_instance = create_expert_instance(
        account_id=acct_def.id, expert="MockExpert", virtual_equity_pct=pct
    )
    monkeypatch.setattr("ba2_common.core.instance_resolver._resolver", _StubResolver(account))
    return MockExpert(expert_instance.id), expert_instance, account


class TestVirtualBalanceHonoursAZeroPercentSleeve:
    def test_zero_percent_allocates_zero(self, monkeypatch):
        """THE DEFECT: ``pct or 100.0`` handed a 0% sleeve 100% of the account."""
        expert, _record, _account = _expert_at(monkeypatch, pct=0.0)
        assert expert.get_virtual_balance() == 0.0

    def test_an_ordinary_percentage_is_unchanged(self, monkeypatch):
        """THE INVERSE: the everyday path must not move."""
        expert, _record, _account = _expert_at(monkeypatch, pct=10.0)
        assert expert.get_virtual_balance() == pytest.approx(10_000.0)

    def test_one_hundred_percent_is_unchanged(self, monkeypatch):
        expert, _record, _account = _expert_at(monkeypatch, pct=100.0)
        assert expert.get_virtual_balance() == pytest.approx(100_000.0)

    def test_a_zero_percent_sleeve_has_no_available_balance_either(self, monkeypatch):
        """The consequence that matters: ``get_available_balance`` is what the order
        gates read, and it is derived from the virtual balance."""
        expert, _record, _account = _expert_at(monkeypatch, pct=0.0)
        assert expert.get_available_balance() == 0.0

    def test_a_zero_balance_account_is_still_distinguishable_from_an_error(self, monkeypatch):
        """A measured $0 ACCOUNT balance yields a measured $0 virtual balance (not
        None); an UNREADABLE account balance still yields None."""
        expert, _record, account = _expert_at(monkeypatch, pct=50.0, balance=0.0)
        assert expert.get_virtual_balance() == 0.0

        account._balance = None
        assert expert.get_virtual_balance() is None


class TestBalanceUsageChartHonoursAZeroPercentSleeve:
    """What the operator SEES must agree with what the sizing code does."""

    def _totals(self, monkeypatch, pct, account_balance=100_000.0):
        import sys
        from ba2_trade_platform.ui.components.BalanceUsagePerExpertChart import (
            BalanceUsagePerExpertChart,
        )
        # ``ui.components.__init__`` re-exports the class under the module's own name,
        # so monkeypatch's dotted-path lookup lands on the CLASS. Take the module from
        # sys.modules instead.
        chart_module = sys.modules[
            "ba2_trade_platform.ui.components.BalanceUsagePerExpertChart"]
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        account._balance = account_balance
        create_expert_instance(
            account_id=acct_def.id, expert="MockExpert", virtual_equity_pct=pct
        )
        monkeypatch.setattr(chart_module, "get_selected_account_id", lambda: None)
        monkeypatch.setattr(chart_module, "get_expert_ids_for_account", lambda _acc_id: None)
        monkeypatch.setattr(
            "ba2_trade_platform.core.utils.get_account_instance_from_id",
            lambda account_id, session=None, use_cache=True: account,
        )
        # calculate_expert_balance_data never touches ``self``; __init__ builds NiceGUI
        # widgets, which a headless test has no business doing.
        return BalanceUsagePerExpertChart.calculate_expert_balance_data(None)

    def test_zero_percent_charts_as_zero(self, monkeypatch):
        data = self._totals(monkeypatch, pct=0.0)
        assert data, "the expert must still appear in the chart"
        assert [d["total"] for d in data.values()] == [0.0]

    def test_an_ordinary_percentage_is_unchanged(self, monkeypatch):
        data = self._totals(monkeypatch, pct=25.0)
        assert [d["total"] for d in data.values()] == [pytest.approx(25_000.0)]

    def test_a_zero_ACCOUNT_balance_still_charts_the_expert(self, monkeypatch):
        """THE INVERSE at the other end: the guard above the multiply is
        `if account_balance is None: continue`, and it must stay an `is None`. An
        account that measurably holds $0 belongs on the chart AT ZERO -- dropping it
        makes an empty account indistinguishable from an unreachable broker."""
        data = self._totals(monkeypatch, pct=25.0, account_balance=0.0)
        assert len(data) == 1, "a $0 account is measured, not missing"
        assert [d["total"] for d in data.values()] == [0.0]


class TestSmartRiskManagerEquityHonoursAZeroPercentSleeve:
    """The equity the risk manager records for a run, start and end."""

    def _run(self, monkeypatch, pct):
        from ba2_trade_platform.core.SmartRiskManagerQueue import (
            SmartRiskManagerQueue, SmartRiskManagerTask,
        )
        from ba2_trade_platform.core.models import SmartRiskManagerJob

        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        expert_record = create_expert_instance(
            account_id=acct_def.id, expert="MockExpert", virtual_equity_pct=pct
        )
        expert = MockExpert(expert_record.id)

        # An Alpaca-shaped account_info: the queue reads ``account_info.equity``.
        monkeypatch.setattr(
            account, "get_account_info",
            lambda: SimpleNamespace(equity="100000", buying_power="50000"),
        )
        monkeypatch.setattr(
            "ba2_trade_platform.core.utils.get_expert_instance_from_id",
            lambda expert_instance_id, use_cache=True: expert,
        )
        monkeypatch.setattr(
            "ba2_trade_platform.core.utils.get_account_instance_from_id",
            lambda account_id, session=None, use_cache=True: account,
        )
        import ba2_trade_platform.core.SmartRiskManagerGraph as _graph
        monkeypatch.setattr(
            _graph, "run_smart_risk_manager",
            lambda expert_instance_id, account_id, job_id=None: {
                "success": True, "iterations": 1, "actions_count": 0, "summary": "",
            },
        )

        q = SmartRiskManagerQueue(num_workers=0)
        task = SmartRiskManagerTask(id="t1", expert_instance_id=expert_record.id,
                                    account_id=acct_def.id)
        q._execute_task(task, "worker-test")
        job = get_instance(SmartRiskManagerJob, task.job_id)
        return job.initial_portfolio_equity, job.final_portfolio_equity

    def test_zero_percent_records_zero_equity(self, monkeypatch):
        """THE DEFECT, twice: a 0% sleeve was recorded as owning the whole $100k both
        before and after the run, so every risk decision was sized off it."""
        assert self._run(monkeypatch, pct=0.0) == (0.0, 0.0)

    def test_an_ordinary_percentage_is_unchanged(self, monkeypatch):
        initial, final = self._run(monkeypatch, pct=5.0)
        assert initial == pytest.approx(5_000.0)
        assert final == pytest.approx(5_000.0)


def test_the_field_can_never_be_null_so_the_or_only_ever_hit_a_real_zero():
    """The premise behind deleting the coercion, pinned so it cannot rot: the column
    is non-nullable with a 100.0 default, so ``pct or 100.0`` had exactly one
    reachable trigger -- a user-entered 0."""
    field = ExpertInstance.model_fields["virtual_equity_pct"]
    assert field.default == 100.0
    assert field.annotation is float, "an Optional[float] would change the argument"

    acct_def = create_account_definition()
    created = create_expert_instance(account_id=acct_def.id, expert="MockExpert")
    assert get_instance(ExpertInstance, created.id).virtual_equity_pct == 100.0

    stored = get_instance(ExpertInstance, created.id)
    stored.virtual_equity_pct = 0.0
    update_instance(stored)
    assert get_instance(ExpertInstance, created.id).virtual_equity_pct == 0.0
