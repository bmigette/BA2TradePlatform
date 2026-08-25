"""GAP 2: ``SmartRiskManagerQueue._execute_task`` must read equity through the seam.

``_execute_task`` records the expert's virtual equity on the ``SmartRiskManagerJob``
twice -- once before the run and once after -- and both reads were:

    account_info = account.get_account_info()
    if account_info and account_info.equity:
        account_equity = float(account_info.equity)

Two defects in the same three lines.

1. ``account_info.equity`` is ATTRIBUTE access on a BROKER-SHAPED value.
   ``get_account_info()`` returns a pydantic ``TradeAccount`` on Alpaca but a plain
   ``dict`` on IBKR (``{"equity": ...}``) and on TastyTrade (``{"net_liquidating_value":
   ...}`` -- no ``equity`` key at all), and ``{}`` on an auth failure. On every
   dict-shaped broker the attribute access raises ``AttributeError``, the enclosing
   ``except Exception`` downgrades it to a ``logger.warning``, and the job silently
   records ``None`` equity for both ends of the run. This is the same defect, in the
   same shape, as the one already fixed in
   ``AccountInterface._validate_position_size_limits`` and ``TradeActions`` Task 34:
   ``get_account_snapshot()`` is the broker-agnostic seam that exists for it.

2. ``if account_info and account_info.equity:`` is TRUTHINESS ON A FLOAT. A measured
   $0.00 equity is falsy, so a real, correctly-read zero is discarded and recorded as
   ``None`` -- "the account holds nothing" becomes indistinguishable from "the broker
   would not tell us".

Each is paired with its inverse: an Alpaca-shaped object must not regress, and an
equity the broker genuinely did not publish must STILL be recorded as ``None``.
"""
import pytest
from datetime import datetime, timezone
from types import SimpleNamespace

from tests.conftest import MockAccount, MockExpert
from tests.factories import create_account_definition, create_expert_instance
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import SmartRiskManagerJob


# A fixed instant, deliberately NOT "now": a test that passes only on the day it was
# written is a test that will fail silently later.
FROZEN_NOW = datetime(2026, 3, 17, 14, 30, 0, tzinfo=timezone.utc)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW if tz is not None else FROZEN_NOW.replace(tzinfo=None)


def _capture_errors(monkeypatch):
    """Collect ``logger.error`` text emitted by SmartRiskManagerQueue.

    NOT caplog: ``ba2_trade_platform/logger.py`` sets ``propagate = False``, so
    caplog's root handler never sees the record, and other test modules replace the
    logger module with a MagicMock at import time. Patching the module-under-test's
    own ``logger`` is immune to both.
    """
    import sys
    import ba2_trade_platform.core.SmartRiskManagerQueue  # noqa: F401
    module = sys.modules["ba2_trade_platform.core.SmartRiskManagerQueue"]
    messages = []
    monkeypatch.setattr(module.logger, "error", lambda msg, *a, **k: messages.append(str(msg)))
    return messages


def _capture_warnings(monkeypatch):
    """The pre-fix behaviour reported the AttributeError as a WARNING and carried on."""
    import sys
    import ba2_trade_platform.core.SmartRiskManagerQueue  # noqa: F401
    module = sys.modules["ba2_trade_platform.core.SmartRiskManagerQueue"]
    messages = []
    monkeypatch.setattr(module.logger, "warning", lambda msg, *a, **k: messages.append(str(msg)))
    return messages


def _run(monkeypatch, account, pct=100.0):
    """Drive one ``_execute_task`` and return (initial_equity, final_equity, job)."""
    import sys
    from ba2_trade_platform.core.SmartRiskManagerQueue import (
        SmartRiskManagerQueue, SmartRiskManagerTask,
    )
    module = sys.modules["ba2_trade_platform.core.SmartRiskManagerQueue"]
    monkeypatch.setattr(module, "datetime", _FrozenDateTime)

    expert_record = create_expert_instance(
        account_id=account.id, expert="MockExpert", virtual_equity_pct=pct
    )
    expert = MockExpert(expert_record.id)

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
                                account_id=account.id)
    q._execute_task(task, "worker-test")
    job = get_instance(SmartRiskManagerJob, task.job_id)
    return job.initial_portfolio_equity, job.final_portfolio_equity, job


class _TastyShapedAccount(MockAccount):
    """``TastyTradeAccount.get_account_info()``: a dict with NO ``equity`` key."""

    def get_account_info(self):
        return {
            "account_number": "5WX00000",
            "buying_power": 50_000.0,
            "net_liquidating_value": 100_000.0,
            "cash_balance": 25_000.0,
            "margin_equity": 100_000.0,
        }


class _IbkrShapedAccount(MockAccount):
    """``IBKRAccount.get_account_info()``: a dict that DOES key ``equity``.

    Still broken pre-fix -- ``dict`` has no ``.equity`` ATTRIBUTE however it is keyed.
    """

    def get_account_info(self):
        return {"account_number": "DU111111", "currency": "USD",
                "equity": 100_000.0, "cash": 25_000.0, "buying_power": 50_000.0}


class _AlpacaShapedAccount(MockAccount):
    """Alpaca's pydantic ``TradeAccount``: attributes, and the numbers are STRINGS."""

    def get_account_info(self):
        return SimpleNamespace(equity="100000", buying_power="50000", cash="25000")


class _MuteAccount(MockAccount):
    """A broker that published no balance figure at all."""

    def get_account_info(self):
        return {"account_number": "5WX00000"}


class TestTheEquityReadGoesThroughTheSnapshotSeam:

    def test_a_dict_shaped_broker_records_its_equity(self, monkeypatch):
        """THE DEFECT: MockAccount already returns ``{"balance":…, "equity":…}`` --
        the plainest dict shape there is -- and ``account_info.equity`` raises
        AttributeError on it. Both ends of the run recorded ``None``."""
        acct_def = create_account_definition()
        account = _IbkrShapedAccount(acct_def.id)
        warnings = _capture_warnings(monkeypatch)

        initial, final, _job = _run(monkeypatch, account, pct=100.0)

        assert initial == pytest.approx(100_000.0), (initial, warnings)
        assert final == pytest.approx(100_000.0), (final, warnings)
        assert not any("Could not get" in w for w in warnings), (
            "a dict-shaped broker is not an error condition", warnings)

    def test_a_tastytrade_shaped_broker_with_no_equity_key_records_its_equity(self, monkeypatch):
        """The seam maps ``net_liquidating_value`` -> equity. Duck-typing ``.equity``
        could never have worked here even if the value had been a pydantic object."""
        acct_def = create_account_definition()
        account = _TastyShapedAccount(acct_def.id)

        initial, final, _job = _run(monkeypatch, account, pct=100.0)

        assert initial == pytest.approx(100_000.0)
        assert final == pytest.approx(100_000.0)

    def test_the_sleeve_percentage_is_still_applied(self, monkeypatch):
        """Routing through the seam must not drop the virtual-equity multiplication."""
        acct_def = create_account_definition()
        account = _TastyShapedAccount(acct_def.id)

        initial, final, _job = _run(monkeypatch, account, pct=25.0)

        assert initial == pytest.approx(25_000.0)
        assert final == pytest.approx(25_000.0)

    def test_an_alpaca_shaped_object_broker_is_not_regressed(self, monkeypatch):
        """THE INVERSE #1: the one shape that DID work must keep working -- including
        Alpaca shipping its numbers as strings."""
        acct_def = create_account_definition()
        account = _AlpacaShapedAccount(acct_def.id)

        initial, final, _job = _run(monkeypatch, account, pct=10.0)

        assert initial == pytest.approx(10_000.0)
        assert final == pytest.approx(10_000.0)


class TestAMeasuredZeroEquityRecordsAsZero:

    def test_zero_equity_is_recorded_as_zero_not_none(self, monkeypatch):
        """THE DEFECT: ``if account_info and account_info.equity:`` is truthiness on a
        float. A drained account measured at $0.00 was recorded as ``None`` -- an
        unknown -- so the operator cannot tell it from an unreachable broker."""
        class _EmptyAccount(MockAccount):
            def get_account_info(self):
                return {"equity": 0.0, "cash": 0.0, "buying_power": 0.0}

        acct_def = create_account_definition()
        account = _EmptyAccount(acct_def.id)

        initial, final, _job = _run(monkeypatch, account, pct=100.0)

        assert initial == 0.0, "a measured $0 equity is an answer, not an unknown"
        assert final == 0.0
        assert initial is not None and final is not None

    def test_a_zero_percent_sleeve_of_a_funded_account_is_still_zero(self, monkeypatch):
        """The other zero on the same line, already fixed once: 0% of $100k is $0.00,
        recorded, not ``None``."""
        acct_def = create_account_definition()
        account = _IbkrShapedAccount(acct_def.id)

        initial, final, _job = _run(monkeypatch, account, pct=0.0)

        assert (initial, final) == (0.0, 0.0)

    def test_an_unpublished_equity_is_STILL_recorded_as_none(self, monkeypatch):
        """THE INVERSE #2, and the whole point of the distinction: a broker that
        published no balance figure at all leaves the field ``None`` -- and says so
        LOUDLY, because a risk manager reasoning about an unknown portfolio is a
        different situation from one reasoning about an empty portfolio."""
        acct_def = create_account_definition()
        account = _MuteAccount(acct_def.id)
        errors = _capture_errors(monkeypatch)

        initial, final, _job = _run(monkeypatch, account, pct=100.0)

        assert initial is None, "an unpublished equity must not be fabricated as 0.0"
        assert final is None
        assert any("equity" in e.lower() for e in errors), errors
        assert len(errors) >= 2, (
            "both the initial and the final read must report the gap", errors)

    def test_an_exploding_broker_is_still_survivable(self, monkeypatch):
        """THE INVERSE #3: the equity read is bookkeeping, not the job. A broker that
        raises must not take the whole Smart Risk Manager run down with it."""
        class _ExplodingAccount(MockAccount):
            def get_account_info(self):
                raise RuntimeError("broker down")

        acct_def = create_account_definition()
        account = _ExplodingAccount(acct_def.id)

        initial, final, job = _run(monkeypatch, account, pct=100.0)

        assert initial is None and final is None
        assert job.status == "COMPLETED", "the run itself succeeded"


class TestTheJobRecordIsAnchoredInTime:

    def test_the_run_date_is_the_real_clock_not_a_default(self, monkeypatch):
        acct_def = create_account_definition()
        account = _IbkrShapedAccount(acct_def.id)

        _initial, _final, job = _run(monkeypatch, account, pct=100.0)

        run_date = job.run_date
        if run_date.tzinfo is None:
            run_date = run_date.replace(tzinfo=timezone.utc)
        assert run_date == FROZEN_NOW
