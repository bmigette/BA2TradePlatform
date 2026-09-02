"""The DTE readers answer per-leg, and each one NAMES the leg it reads.

Ambiguity is the entire reason the single-expiry guard existed: with two expiries on one
structure, "the DTE" is not a quantity until somebody says which leg they mean. So the rule
is explicit rather than inferred, and it differs by QUESTION:

* ``option_lifecycle._dte`` feeds ``roll_dte`` — "when must the overlay be rolled?" — and
  reads the **SHORT** leg. Reading the LEAPS here would put a PMCC's roll a year out and the
  roll branch would never fire, which is exactly the dead-gene failure ``_dte`` was written
  to end.
* ``DaysToExpiryCondition`` backs the ``opt_dte`` exit — "is there still life to roll into?",
  the roll FLOOR — and reads the **LONG** leg. Reading the short here would flatten the whole
  structure every time the overlay approached its own expiry, throwing away a LEAPS with a
  year left because a 30-day call was expiring on schedule.

Design ``docs/superpowers/specs/2026-08-31-leaps-grid-design.md`` §4: "Roll loop: at short
expiry"; "Structure exit: long-leg DTE floor".

The third reader named by the requirement, the grid's ``opt_time`` exit, compares
``days_opened`` — elapsed time since the position opened. It reads no expiry and therefore
no leg; ``test_opt_time_reads_no_leg_because_it_reads_no_expiry`` pins that so the claim is
checked rather than asserted in a comment.

Every test here uses a structure whose two legs sit on DIFFERENT dates, so a reader that
silently took the other side produces a different number rather than the same one.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core.option_lifecycle import LifecycleLeg, OptionStructure, _dte

# The simulated evaluation bar: in the past, and never equal to the wall clock.
SIM_AS_OF = datetime(2024, 6, 15, 15, 30, tzinfo=timezone.utc)
SIM_TODAY = date(2024, 6, 15)

SHORT_EXPIRY = SIM_TODAY + timedelta(days=20)    # the overlay
LONG_EXPIRY = SIM_TODAY + timedelta(days=400)    # the LEAPS

PMCC = "pmcc"


# ===========================================================================
# reader 1 — option_lifecycle._dte : the ROLL WINDOW, read from the SHORT leg
# ===========================================================================
def _structure(*, strategy, expiry=None, legs=None):
    return OptionStructure(transaction_id=1, underlying="AAPL", strategy=strategy,
                           legs=legs if legs is not None else _pmcc_legs(), expiry=expiry)


def _pmcc_legs():
    """Long LEAPS (net +1) and short overlay (net -1), on different expiries."""
    return [
        LifecycleLeg(contract_symbol="AAPL_LEAPS", net_qty=1.0, expiry=LONG_EXPIRY),
        LifecycleLeg(contract_symbol="AAPL_OVERLAY", net_qty=-1.0, expiry=SHORT_EXPIRY),
    ]


def test_dte_reads_the_SHORT_leg_for_a_declared_two_expiry_structure():
    """The roll window. 20 days, not 400."""
    dte, blind = _dte(_structure(strategy=PMCC), SIM_TODAY)

    assert blind == ""
    assert dte == 20, "the roll window is the SHORT leg's remaining life"
    assert dte != 400


def test_dte_does_not_read_the_LONG_leg():
    """Stated separately from the assertion above so the failure message is unambiguous when
    the rule is swapped."""
    dte, _ = _dte(_structure(strategy=PMCC), SIM_TODAY)
    assert dte != (LONG_EXPIRY - SIM_TODAY).days


def test_dte_still_refuses_an_UNDECLARED_two_expiry_structure():
    """The pre-existing contradiction rule, untouched for everything not declared."""
    dte, blind = _dte(_structure(strategy="bull_call_spread"), SIM_TODAY)

    assert dte is None
    assert "conflicting expiries" in blind
    assert str(SHORT_EXPIRY) in blind and str(LONG_EXPIRY) in blind


def test_dte_is_unchanged_for_a_single_expiry_structure():
    """The byte-identical claim, at this reader."""
    legs = [LifecycleLeg(contract_symbol="A", net_qty=1.0, expiry=SHORT_EXPIRY),
            LifecycleLeg(contract_symbol="B", net_qty=-1.0, expiry=SHORT_EXPIRY)]
    dte, blind = _dte(_structure(strategy="bull_call_spread", legs=legs), SIM_TODAY)

    assert (dte, blind) == (20, "")


def test_dte_on_a_single_expiry_structure_is_the_same_whether_or_not_it_is_declared():
    """A per-leg question asked of a single-expiry structure returns the single expiry."""
    legs = [LifecycleLeg(contract_symbol="A", net_qty=1.0, expiry=SHORT_EXPIRY),
            LifecycleLeg(contract_symbol="B", net_qty=-1.0, expiry=SHORT_EXPIRY)]

    assert _dte(_structure(strategy="bull_call_spread", legs=legs), SIM_TODAY) == \
           _dte(_structure(strategy=PMCC, legs=legs), SIM_TODAY)


def test_dte_is_still_unknown_when_nothing_records_an_expiry():
    legs = [LifecycleLeg(contract_symbol="A", net_qty=1.0, expiry=None)]
    dte, blind = _dte(_structure(strategy=PMCC, legs=legs), SIM_TODAY)

    assert dte is None
    assert "no expiry" in blind


def test_dte_refuses_a_declared_structure_with_no_SHORT_leg():
    """Fail-closed: a PMCC that has lost its overlay has no roll window. Answering from the
    LEAPS would schedule the next roll 400 days out."""
    legs = [LifecycleLeg(contract_symbol="A", net_qty=1.0, expiry=LONG_EXPIRY),
            LifecycleLeg(contract_symbol="B", net_qty=1.0, expiry=SHORT_EXPIRY)]
    dte, blind = _dte(_structure(strategy=PMCC, legs=legs), SIM_TODAY)

    assert dte is None and "conflicting expiries" in blind


def test_dte_ignores_a_closed_short_and_reads_the_live_one():
    """A roll in flight: the bought-back overlay nets to zero and stops binding."""
    legs = _pmcc_legs() + [LifecycleLeg(contract_symbol="AAPL_OLD", net_qty=0.0,
                                        expiry=SIM_TODAY + timedelta(days=3))]
    dte, blind = _dte(_structure(strategy=PMCC, legs=legs), SIM_TODAY)

    assert (dte, blind) == (20, "")


# ===========================================================================
# reader 2 — DaysToExpiryCondition : the STRUCTURE EXIT, read from the LONG leg
# ===========================================================================
def _setup_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "per_leg_dte.sqlite"))
    db.init_db()
    return db


class _FakeAccount:
    id = 1


def _rec(as_of=SIM_AS_OF):
    return SimpleNamespace(created_at=as_of, instance_id=1, symbol="AAPL")


def _option_txn(db, *, strategy, expiry=None):
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import AssetClass, OrderDirection, TransactionStatus

    return db.add_instance(Transaction(
        symbol="AAPL", quantity=1, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_date=SIM_AS_OF - timedelta(days=40),
        asset_class=AssetClass.OPTION, option_strategy=strategy, multiplier=100,
        expiry=expiry))


def _parent_order(db, txn_id, *, strategy, expiry=None):
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import AssetClass, OrderDirection, OrderStatus, OrderType

    order = TradingOrder(
        account_id=1, symbol="AAPL", quantity=1, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, transaction_id=txn_id,
        asset_class=AssetClass.OPTION, underlying_symbol="AAPL",
        option_strategy=strategy, multiplier=100, expiry=expiry,
        created_at=datetime.now(timezone.utc))
    db.add_instance(order, expunge_after_flush=True)
    return order


def _leg(db, txn_id, contract, *, side, expiry, qty=1):
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import AssetClass, OptionRight, OrderStatus, OrderType

    leg = TradingOrder(
        account_id=1, symbol="AAPL", quantity=qty, side=side, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, transaction_id=txn_id, asset_class=AssetClass.OPTION,
        contract_symbol=contract, underlying_symbol="AAPL", option_type=OptionRight.CALL,
        strike=100.0, multiplier=100, expiry=expiry, open_price=1.0, filled_qty=qty,
        created_at=datetime.now(timezone.utc))
    db.add_instance(leg, expunge_after_flush=True)
    return leg


def _cond(order, *, op="<=", value=21):
    from ba2_common.core.TradeConditions import DaysToExpiryCondition
    return DaysToExpiryCondition(
        account=_FakeAccount(), instrument_name="AAPL", expert_recommendation=_rec(),
        operator_str=op, value=value, existing_order=order)


def _pmcc_position(db, *, strategy=PMCC):
    from ba2_common.core.types import OrderDirection

    txn_id = _option_txn(db, strategy=strategy)
    parent = _parent_order(db, txn_id, strategy=strategy)
    _leg(db, txn_id, "AAPL_LEAPS", side=OrderDirection.BUY, expiry=LONG_EXPIRY)
    _leg(db, txn_id, "AAPL_OVERLAY", side=OrderDirection.SELL, expiry=SHORT_EXPIRY)
    return parent


def test_the_condition_reads_the_LONG_leg_for_a_declared_two_expiry_structure(tmp_path):
    """The roll FLOOR: 400 days of structure life, not the overlay's 20."""
    db = _setup_db(tmp_path)
    cond = _cond(_pmcc_position(db))

    assert cond.evaluate() is False, "400 DTE is not <= 21 — this structure must not exit"
    assert cond.get_calculated_value() == 400


def test_the_condition_does_not_read_the_SHORT_leg(tmp_path):
    """The expensive direction: exiting a LEAPS with a year left because the 20-day overlay
    is expiring on schedule."""
    db = _setup_db(tmp_path)
    cond = _cond(_pmcc_position(db))
    cond.evaluate()

    assert cond.get_calculated_value() != 20


def test_the_condition_still_refuses_an_UNDECLARED_two_expiry_structure(tmp_path):
    """Pre-existing rows that disagree are still a contradiction, not a measurement."""
    db = _setup_db(tmp_path)
    cond = _cond(_pmcc_position(db, strategy="bull_call_spread"))

    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None
    assert "conflicting expiries" in cond.get_actual_value_display()


def test_the_condition_refuses_a_declared_structure_with_no_LONG_leg(tmp_path):
    """Fail-closed. A position holding only short overlays has no structure floor, and
    reporting the short's date as one would call a naked short a healthy covered structure."""
    from ba2_common.core.types import OrderDirection

    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, strategy=PMCC)
    parent = _parent_order(db, txn_id, strategy=PMCC)
    _leg(db, txn_id, "AAPL_A", side=OrderDirection.SELL, expiry=SHORT_EXPIRY)
    _leg(db, txn_id, "AAPL_B", side=OrderDirection.SELL, expiry=LONG_EXPIRY)

    cond = _cond(parent)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


def test_the_condition_is_unchanged_for_a_single_expiry_structure(tmp_path):
    """The byte-identical claim, at this reader."""
    from ba2_common.core.types import OrderDirection

    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, strategy="bull_call_spread", expiry=SHORT_EXPIRY)
    parent = _parent_order(db, txn_id, strategy="bull_call_spread", expiry=SHORT_EXPIRY)
    _leg(db, txn_id, "AAPL_A", side=OrderDirection.BUY, expiry=SHORT_EXPIRY)
    _leg(db, txn_id, "AAPL_B", side=OrderDirection.SELL, expiry=SHORT_EXPIRY)

    cond = _cond(parent)
    assert cond.evaluate() is True                    # 20 <= 21
    assert cond.get_calculated_value() == 20


def test_a_declared_structure_on_ONE_expiry_reads_that_expiry(tmp_path):
    """A per-leg question on a single-expiry structure returns the single expiry."""
    from ba2_common.core.types import OrderDirection

    db = _setup_db(tmp_path)
    txn_id = _option_txn(db, strategy=PMCC)
    parent = _parent_order(db, txn_id, strategy=PMCC)
    _leg(db, txn_id, "AAPL_A", side=OrderDirection.BUY, expiry=SHORT_EXPIRY)
    _leg(db, txn_id, "AAPL_B", side=OrderDirection.SELL, expiry=SHORT_EXPIRY)

    cond = _cond(parent)
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 20


# ===========================================================================
# THE CONTRAST — the two readers disagree, on purpose, on the same position
# ===========================================================================
def test_the_two_readers_give_DIFFERENT_answers_for_the_same_structure(tmp_path):
    """The point of naming the rules. If these ever coincided, every test above would pass
    under a single shared rule and would have proved nothing."""
    db = _setup_db(tmp_path)
    cond = _cond(_pmcc_position(db))
    cond.evaluate()

    roll_window_dte, _ = _dte(_structure(strategy=PMCC), SIM_TODAY)
    structure_exit_dte = cond.get_calculated_value()

    assert roll_window_dte == 20, "roll window = SHORT leg"
    assert structure_exit_dte == 400, "structure exit = LONG leg"
    assert roll_window_dte != structure_exit_dte


# ===========================================================================
# reader 3 — the grid's opt_time exit reads NO leg, because it reads no expiry
# ===========================================================================
def test_opt_time_reads_no_leg_because_it_reads_no_expiry():
    """``opt_time`` compares ``days_opened`` — elapsed time, not remaining life. The
    requirement names it alongside ``opt_dte``, so this pins WHY it has no leg rule rather
    than leaving the claim to a comment. If someone ever repoints it at an expiry field,
    this test says so and the rule table must gain a row."""
    import pathlib
    import sys

    launcher = (pathlib.Path(__file__).resolve().parents[3]
                / "testplatform" / "ba2test_launcher.py")
    if not launcher.exists():                       # packages/ may be consumed standalone
        pytest.skip("launcher not present in this checkout")

    sys.path.insert(0, str(launcher.parent))
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_ba2test_launcher_opt_time", launcher)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(launcher.parent))

    rules = module._option_exit_rules("O_LC")
    by_id = {r["id"]: r for r in rules}

    time_leaves = by_id["opt_time"]["conditions"]["conditions"]
    assert [leaf["field"] for leaf in time_leaves] == ["days_opened"], \
        "opt_time must read elapsed days; an expiry field here needs a named leg rule"

    dte_leaves = by_id["opt_dte"]["conditions"]["conditions"]
    assert [leaf["field"] for leaf in dte_leaves] == ["days_to_expiry"], \
        "opt_dte is the reader that reaches DaysToExpiryCondition (the LONG-leg rule)"
