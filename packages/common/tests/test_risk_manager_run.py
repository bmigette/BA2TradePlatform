"""``risk_manager_run``: every symbol the manager received is accounted for, with a reason.

The record this module writes exists because the classic manager's ActivityLog row carried
only counts -- "reviewed 42, updated 7" answers "how many" and never "which, and why not".
So the properties worth pinning are the ones that make the record answer the second
question: no symbol silently missing, no refusal without a cause, and no quantity invented
for something that was never sized.

And one that is not about content at all: this must not write during a BACKTEST. The GA's
daily engine drives the same risk manager thousands of times per trial.
"""
import pytest

from ba2_common.core.risk_manager_run import (
    MODE_CLASSIC,
    OUTCOME_FUNDED,
    OUTCOME_PERMISSION,
    OUTCOME_UNFUNDED,
    build_decisions,
    decision,
    record_run,
)


@pytest.fixture
def expert_instance_id():
    """A real ExpertInstance, because ``RiskManagerRun.expert_instance_id`` is a FK."""
    from ba2_common.core.db import get_db
    from ba2_common.core.models import AccountDefinition, ExpertInstance
    with get_db() as session:
        account = AccountDefinition(name="rmr-test", provider="StubProvider")
        session.add(account)
        session.commit()
        session.refresh(account)
        expert = ExpertInstance(account_id=account.id, expert="StubExpert")
        session.add(expert)
        session.commit()
        session.refresh(expert)
        return expert.id


# ---------------------------------------------------------------------------------------
# A decision must explain itself
# ---------------------------------------------------------------------------------------

def test_a_refusal_without_a_reason_is_refused():
    """"Not funded" with no cause is the non-answer the counts already gave."""
    with pytest.raises(ValueError, match="carries no reason"):
        decision("AAPL", OUTCOME_UNFUNDED, "")


def test_a_decision_without_a_symbol_is_refused():
    with pytest.raises(ValueError, match="must name its symbol"):
        decision("", OUTCOME_FUNDED, "sized at 10")


def test_a_refused_symbol_carries_no_quantity_at_all():
    """Not 0. A refused symbol has NO quantity; 0 would read as "sized, at nothing",
    which is a different outcome the classic manager can genuinely produce."""
    row = decision("AAPL", OUTCOME_UNFUNDED, "budget exhausted")

    assert "quantity" not in row


def test_a_funded_symbol_carries_its_quantity():
    row = decision("AAPL", OUTCOME_FUNDED, "sized at 12", quantity=12)

    assert row["quantity"] == 12.0


# ---------------------------------------------------------------------------------------
# Every received symbol is accounted for
# ---------------------------------------------------------------------------------------

def test_every_received_symbol_appears_even_when_nothing_decided_it():
    """A drop point nobody instrumented must show up as an UNEXPLAINED refusal, not as a
    missing row -- a missing row reads as "the manager never saw it"."""
    rows = build_decisions(["AAPL", "MSFT"], funded={}, reasons={})

    assert [r["symbol"] for r in rows] == ["AAPL", "MSFT"]
    assert all(r["outcome"] == OUTCOME_UNFUNDED for r in rows)
    assert all(r["reason"] for r in rows)


def test_the_received_order_is_preserved():
    """The manager ranks by profit; the record must show the order it actually worked in."""
    rows = build_decisions(["ZZZ", "AAA", "MMM"], funded={"AAA": 5}, reasons={})

    assert [r["symbol"] for r in rows] == ["ZZZ", "AAA", "MMM"]


def test_an_outcome_for_a_symbol_never_received_is_a_caller_bug():
    """Appending it would produce a record claiming the manager saw something it did not."""
    with pytest.raises(ValueError, match="never in the received set"):
        build_decisions(["AAPL"], funded={"TSLA": 3}, reasons={})

    with pytest.raises(ValueError, match="never in the received set"):
        build_decisions(["AAPL"], funded={}, reasons={"TSLA": (OUTCOME_PERMISSION, "sell disabled")})


def test_funded_and_refused_symbols_are_distinguished():
    rows = build_decisions(
        ["AAPL", "MSFT", "TSLA"],
        funded={"AAPL": 12},
        reasons={"MSFT": (OUTCOME_PERMISSION, "SELL disabled for this expert")})
    by_symbol = {r["symbol"]: r for r in rows}

    assert by_symbol["AAPL"]["outcome"] == OUTCOME_FUNDED
    assert by_symbol["AAPL"]["quantity"] == 12.0
    assert by_symbol["MSFT"]["outcome"] == OUTCOME_PERMISSION
    assert "SELL disabled" in by_symbol["MSFT"]["reason"]
    assert by_symbol["TSLA"]["outcome"] == OUTCOME_UNFUNDED


# ---------------------------------------------------------------------------------------
# The backtest guard
# ---------------------------------------------------------------------------------------

def test_a_backtest_writes_nothing(monkeypatch):
    """The GA drives the same risk manager thousands of times per trial. A row per call
    would put a DB insert in the innermost loop of the grid."""
    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: True)

    run_id = record_run(expert_instance_id=1, account_id=1, mode=MODE_CLASSIC,
                        decisions=[decision("AAPL", OUTCOME_FUNDED, "sized", quantity=1)])

    assert run_id is None


def test_a_live_run_is_persisted_with_its_counts(monkeypatch, expert_instance_id):
    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: False)
    rows = build_decisions(["AAPL", "MSFT", "TSLA"],
                           funded={"AAPL": 12},
                           reasons={"MSFT": (OUTCOME_PERMISSION, "SELL disabled")})

    run_id = record_run(expert_instance_id=expert_instance_id, account_id=None,
                        mode=MODE_CLASSIC, decisions=rows,
                        context={"available_balance": 5000.0})

    assert run_id is not None
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import RiskManagerRun
    saved = get_instance(RiskManagerRun, run_id)
    assert saved.mode == MODE_CLASSIC
    assert saved.symbols_received == 3
    assert saved.symbols_funded == 1
    assert [d["symbol"] for d in saved.decisions] == ["AAPL", "MSFT", "TSLA"]
    assert saved.context["available_balance"] == 5000.0


def test_a_persistence_failure_does_not_propagate(monkeypatch, expert_instance_id):
    """The manager has already sized and persisted its orders by the time this runs.
    An exception here would turn a successful pass into a failed one and leave those
    orders behind with no explanation."""
    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: False)

    def _boom(*a, **kw):
        raise RuntimeError("database on fire")

    monkeypatch.setattr("ba2_common.core.db.add_instance", _boom)

    run_id = record_run(expert_instance_id=expert_instance_id, account_id=None,
                        mode=MODE_CLASSIC,
                        decisions=[decision("AAPL", OUTCOME_FUNDED, "sized", quantity=1)])

    assert run_id is None


def test_a_backtest_builds_no_decisions_at_all(monkeypatch):
    """The guard has to be asked BEFORE the work, not just before the write.

    ``record_run`` refuses in a backtest either way, but the classic manager's recorder
    would otherwise walk every pending order and format a sentence for each one --  per
    bar, per expert, per GA trial -- and then throw it away.
    """
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement

    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: True)

    built = []

    def _spy(*a, **kw):
        built.append(a)
        raise AssertionError("a decision was built during a backtest")

    monkeypatch.setattr("ba2_common.core.risk_manager_run.decision", _spy)

    rm = TradeRiskManagement.__new__(TradeRiskManagement)
    rm.logger = __import__("logging").getLogger("test")

    class _Order:
        id = 1
        symbol = "AAPL"
        side = "BUY"
        quantity = 10

    # Must return quietly, having touched nothing.
    rm._record_classic_run(
        expert_instance_id=1, account_id=1, started_at=None,
        pending_orders=[_Order()], dropped_by_permission=[],
        orders_with_recommendations=[], orders_to_update=[], orders_to_delete=[],
        symbol_prices={}, context={})

    assert built == []


def test_an_unavailable_backtest_seam_does_not_guess_live(monkeypatch):
    """If we cannot tell which world we are in, do NOT write. Guessing "live" puts the
    insert back in the GA's inner loop, which is the failure this guard exists for."""
    import ba2_common.core.trade_store as store
    monkeypatch.delattr(store, "inmem_trades_active")

    run_id = record_run(expert_instance_id=1, account_id=None, mode=MODE_CLASSIC,
                        decisions=[decision("AAPL", OUTCOME_FUNDED, "sized", quantity=1)])

    assert run_id is None
