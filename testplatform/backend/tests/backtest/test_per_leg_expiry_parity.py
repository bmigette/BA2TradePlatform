"""Per-leg expiries are written and read by ONE shared code path, in BOTH runtimes.

Plan ``docs/superpowers/plans/2026-08-31-options-grid2-convex-earnings-impl.md`` Task 6-PRE,
requirement 4.

WHAT "ONE SHARED PATH" ACTUALLY MEANS HERE
------------------------------------------
Measured with ``grep -rn "def submit_option_order" --include=*.py . | grep -v /tests/`` —
exactly TWO production definitions:

* ``OptionsAccountInterface.submit_option_order`` — the single-expiry guard, the parent row,
  the per-leg child rows, the intent stamp. ``AlpacaAccount`` does NOT override it, so live
  runs this and nothing else (pinned by identity below: one function object cannot drift
  from itself).
* ``BacktestAccount.submit_option_order`` — a thin passthrough that calls
  ``super().submit_option_order(...)`` and then drops its order cache so the fill engine's
  next read sees the new rows. It holds no expiry logic, which is pinned structurally AND
  behaviourally: the same legs submitted through the real override must produce byte-
  identical rows to the interface path, and an undeclared two-expiry submit must still be
  refused there.

On the READ side, ``OptionRiskManagement.build_structure`` (promoted precisely so "the live
exit pass and the shared entry gate build one book from one definition") turns stored rows
into legs, and both DTE readers reach ``option_expiry.resolve_structure_expiry``.

THE BEHAVIOURAL HALF, AND WHAT IT IS SENSITIVE TO
-------------------------------------------------
Identity proves the same code runs; it does not prove the code is right. So the second half
writes a real two-expiry structure through the real shared writer and reads it back through
both real readers, with a deliberate trap: the structure-level ``Transaction.expiry`` and
parent-order ``expiry`` are then stamped with a STALE legacy date that matches NEITHER leg.

A correct reader answers from the LEGS — 20 days for the roll window, 400 for the structure
exit. A reader that fell back to the legacy structure-level column would answer with the
stale date instead, and every assertion here would fail. That is the mutation this file is
built to kill.

Run from the backend dir (with the worktree on PYTHONPATH):
    python -m pytest tests/backtest/test_per_leg_expiry_parity.py -q
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance, update_instance
from ba2_common.core.interfaces import OptionsAccountInterface
from ba2_common.core.models import Transaction, TradingOrder
from ba2_common.core.option_expiry import (
    EXPIRY_RULE_ROLL_WINDOW,
    EXPIRY_RULE_STRUCTURE_EXIT,
    resolve_structure_expiry,
)
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection, OrderStatus

SIM_AS_OF = datetime(2024, 6, 15, 15, 30, tzinfo=timezone.utc)
SIM_TODAY = date(2024, 6, 15)

SHORT_EXPIRY = SIM_TODAY + timedelta(days=20)     # the overlay
LONG_EXPIRY = SIM_TODAY + timedelta(days=400)     # the LEAPS
STALE_EXPIRY = SIM_TODAY + timedelta(days=77)     # matches NEITHER leg, on purpose

PMCC = "pmcc"


# ===========================================================================
# 1. the WRITE path is one function, in both runtimes
# ===========================================================================
def test_the_backtest_override_is_a_thin_passthrough_with_no_expiry_logic():
    """``BacktestAccount`` DOES override ``submit_option_order`` — to invalidate its order
    cache so the fill engine's next read sees the new rows. What matters is that the override
    delegates the whole decision to ``super()`` and holds no expiry logic of its own; the
    behavioural half below proves the delegation empirically."""
    import inspect

    from app.services.backtest.backtest_account import BacktestAccount

    source = inspect.getsource(BacktestAccount.submit_option_order)
    body = source.split('"""')[-1]          # past the docstring

    assert "super().submit_option_order" in body, \
        "the backtest override no longer delegates to the shared implementation"
    assert "expiry" not in body.lower(), \
        "the backtest override has grown its own expiry handling — it must not decide this"


def test_the_live_account_does_not_override_the_submit_path():
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

    assert (AlpacaAccount.submit_option_order
            is OptionsAccountInterface.submit_option_order), (
        "AlpacaAccount has grown its own submit_option_order — live would no longer honour "
        "the same expiry rules as the backtest")


def test_both_runtimes_supply_only_the_broker_hook():
    """What each runtime is ALLOWED to differ on: how the order reaches a broker."""
    from app.services.backtest.backtest_account import BacktestAccount
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

    assert (BacktestAccount._submit_option_order_impl
            is not AlpacaAccount._submit_option_order_impl), \
        "the broker hook is the seam; if these were identical the test doubles below lie"


# ===========================================================================
# 2. the READ path is one function too
# ===========================================================================
def test_both_dte_readers_resolve_through_the_one_shared_accessor(monkeypatch, written_pmcc):
    """Not "they agree" — they are WIRED to the same function, and each asks it a different
    named question. A second, private copy of the selection logic in either reader would
    leave its rule out of ``calls`` and fail here."""
    from ba2_common.core import option_lifecycle
    from ba2_common.core import option_expiry as oe
    from ba2_common.core.db import get_instance
    from ba2_common.core.OptionRiskManagement import build_structure
    from ba2_common.core.TradeConditions import DaysToExpiryCondition

    calls = []

    def _spy(*args, **kwargs):
        calls.append(kwargs["rule"])
        return resolve_structure_expiry(*args, **kwargs)

    # option_lifecycle imports the name at module scope; TradeConditions imports it inside
    # the method. Patch both bindings so neither reader can miss the spy.
    monkeypatch.setattr(option_lifecycle, "resolve_structure_expiry", _spy)
    monkeypatch.setattr(oe, "resolve_structure_expiry", _spy)

    structure = build_structure(get_instance(Transaction, written_pmcc.txn_id))
    option_lifecycle._dte(structure, SIM_TODAY)

    DaysToExpiryCondition(
        account=SimpleNamespace(id=1), instrument_name="AAPL",
        expert_recommendation=SimpleNamespace(created_at=SIM_AS_OF, instance_id=1,
                                              symbol="AAPL"),
        operator_str="<=", value=21, existing_order=written_pmcc.parent).evaluate()

    assert calls == [EXPIRY_RULE_ROLL_WINDOW, EXPIRY_RULE_STRUCTURE_EXIT], (
        "both readers must reach the ONE shared accessor, each naming its own rule — "
        f"got {calls}")


# ===========================================================================
# 3. behaviour: written once by the shared writer, read by both real readers
# ===========================================================================
def _leg(expiry, strike, side, intent):
    occ = f"AAPL{expiry:%y%m%d}C{int(strike * 1000):08d}"
    return OptionLeg(contract_symbol=occ, side=side, position_intent=intent,
                     option_type=OptionRight.CALL, strike=strike, expiry=expiry,
                     underlying="AAPL")


class _Account(OptionsAccountInterface):
    """A concrete option account. The ONLY thing it supplies is the broker hook — exactly
    what BacktestAccount and AlpacaAccount each supply, and nothing more."""

    def __init__(self):
        self.id = 1

    def get_option_chain(self, *a, **k):
        raise AssertionError("no chain fetch in this test")

    def get_option_quote(self, contract_symbol):
        raise AssertionError("no quote fetch in this test")

    def get_atm_implied_volatility(self, underlying):
        raise AssertionError("no IV fetch in this test")

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None):
        raise AssertionError("this test never closes")

    def _create_transaction_for_order(self, trading_order):
        from ba2_common.core.types import AssetClass, TransactionStatus
        trading_order.transaction_id = add_instance(Transaction(
            symbol="AAPL", quantity=trading_order.quantity, side=trading_order.side,
            multiplier=100, expert_id=None, asset_class=AssetClass.OPTION,
            status=TransactionStatus.OPENED, open_date=SIM_AS_OF - timedelta(days=5)))

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        trading_order.status = OrderStatus.FILLED
        trading_order.broker_order_id = "double-1"
        trading_order.filled_qty = trading_order.quantity
        trading_order.open_price = 20.0
        update_instance(trading_order)
        for child in (leg_orders or []):
            child.status = OrderStatus.FILLED
            child.filled_qty = child.quantity
            child.open_price = 20.0 if child.side == OrderDirection.BUY else 2.0
            update_instance(child)
        return trading_order


@pytest.fixture
def written_pmcc(tmp_path, _seed_backtest_credentials):
    """A real PMCC, written by the real shared writer, then given a STALE structure-level
    expiry that matches neither leg — the trap described in the module docstring.

    Repoints ``ba2_common.core.db`` back to the session's seeded-credentials DB on teardown
    (see ``conftest.py::_seed_backtest_credentials``): this fixture's ``configure_db`` is a
    raw GLOBAL reassignment (not the per-thread/backtest-run overrides that restore
    themselves), so leaving it pointed at this test's throwaway db starved every later test in
    the session of the seeded FMP/finnhub keys.
    """
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "parity.sqlite"))
    db.init_db()

    account = _Account()
    parent = account.submit_option_order(
        [_leg(LONG_EXPIRY, 100.0, OrderDirection.BUY, "buy_to_open"),
         _leg(SHORT_EXPIRY, 130.0, OrderDirection.SELL, "sell_to_open")],
        quantity=1, order_type="limit", limit_price=18.0, option_strategy=PMCC)

    from ba2_common.core.db import get_instance
    txn = get_instance(Transaction, parent.transaction_id)
    assert txn.expiry is None, "the writer must not stamp a single expiry on a diagonal"

    # Now poison both structure-level values, the way a legacy/back-filled row could.
    txn.expiry = STALE_EXPIRY
    update_instance(txn)
    parent.expiry = STALE_EXPIRY
    update_instance(parent)

    try:
        yield SimpleNamespace(parent=parent, txn_id=parent.transaction_id, db=db)
    finally:
        db.configure_db(str(_seed_backtest_credentials))


def test_the_writer_persisted_one_row_per_leg_with_its_own_expiry(written_pmcc):
    from ba2_common.core.trade_store import orders_where

    children = [o for o in orders_where(transaction_id=written_pmcc.txn_id)
                if o.parent_order_id == written_pmcc.parent.id]
    assert len(children) == 2
    assert {c.expiry for c in children} == {SHORT_EXPIRY, LONG_EXPIRY}


def test_the_roll_window_reader_answers_from_the_SHORT_LEG_not_the_legacy_column(written_pmcc):
    """``option_lifecycle._dte`` over the shared ``build_structure``."""
    from ba2_common.core.db import get_instance
    from ba2_common.core.option_lifecycle import _dte
    from ba2_common.core.OptionRiskManagement import build_structure

    structure = build_structure(get_instance(Transaction, written_pmcc.txn_id))
    dte, blind = _dte(structure, SIM_TODAY)

    assert blind == ""
    assert dte == 20, "the roll window is the SHORT leg"
    assert dte != (STALE_EXPIRY - SIM_TODAY).days, \
        "the reader fell back to the legacy structure-level column"


def test_the_structure_exit_reader_answers_from_the_LONG_LEG_not_the_legacy_column(written_pmcc):
    """``DaysToExpiryCondition`` over the same stored rows."""
    from ba2_common.core.TradeConditions import DaysToExpiryCondition

    cond = DaysToExpiryCondition(
        account=SimpleNamespace(id=1), instrument_name="AAPL",
        expert_recommendation=SimpleNamespace(created_at=SIM_AS_OF, instance_id=1,
                                              symbol="AAPL"),
        operator_str="<=", value=21, existing_order=written_pmcc.parent)

    assert cond.evaluate() is False, "400 DTE of structure life is not <= 21"
    assert cond.get_calculated_value() == 400, "the structure exit is the LONG leg"
    assert cond.get_calculated_value() != (STALE_EXPIRY - SIM_TODAY).days, \
        "the reader fell back to the legacy structure-level column"


def test_the_two_readers_disagree_on_the_same_stored_position(written_pmcc):
    """One position, two questions, two answers — and neither is the stale 77."""
    from ba2_common.core.db import get_instance
    from ba2_common.core.option_lifecycle import _dte
    from ba2_common.core.OptionRiskManagement import build_structure
    from ba2_common.core.TradeConditions import DaysToExpiryCondition

    structure = build_structure(get_instance(Transaction, written_pmcc.txn_id))
    roll_dte, _ = _dte(structure, SIM_TODAY)

    cond = DaysToExpiryCondition(
        account=SimpleNamespace(id=1), instrument_name="AAPL",
        expert_recommendation=SimpleNamespace(created_at=SIM_AS_OF, instance_id=1,
                                              symbol="AAPL"),
        operator_str="<=", value=21, existing_order=written_pmcc.parent)
    cond.evaluate()

    assert (roll_dte, cond.get_calculated_value()) == (20, 400)
    stale = (STALE_EXPIRY - SIM_TODAY).days
    assert stale not in (roll_dte, cond.get_calculated_value())


# ===========================================================================
# 4. the BACKTEST runtime's own override produces byte-identical per-leg rows
# ===========================================================================
def _backtest_shaped_account():
    """An account that submits through ``BacktestAccount``'s REAL override.

    A genuine SUBCLASS, not a borrowed function: the override calls zero-argument
    ``super()``, which requires ``self`` to actually be a ``BacktestAccount``. ``__init__``
    is deliberately not run — a real one needs an engine, a price source and a ledger, none
    of which has anything to do with how a leg's expiry is persisted — so only the two
    attributes this path touches are set.

    Everything below the override is the real inherited code: the guard, the parent row and
    the per-leg children all come from ``OptionsAccountInterface``.
    """
    from app.services.backtest.backtest_account import BacktestAccount

    class _BacktestShapedAccount(BacktestAccount):
        def __init__(self):                       # noqa: D107 - see docstring above
            self.id = 1
            self.cache_invalidations = 0

        def invalidate_order_cache(self):
            self.cache_invalidations += 1

        _create_transaction_for_order = _Account._create_transaction_for_order
        _submit_option_order_impl = _Account._submit_option_order_impl

    return _BacktestShapedAccount()


def _pmcc_legs():
    return [_leg(LONG_EXPIRY, 100.0, OrderDirection.BUY, "buy_to_open"),
            _leg(SHORT_EXPIRY, 130.0, OrderDirection.SELL, "sell_to_open")]


def _submit_and_describe(account):
    """(parent expiry, txn expiry, {(side, expiry)}) for a PMCC written by ``account``."""
    from ba2_common.core.db import get_instance
    from ba2_common.core.trade_store import orders_where

    parent = account.submit_option_order(_pmcc_legs(), quantity=1, order_type="limit",
                                         limit_price=18.0, option_strategy=PMCC)
    txn = get_instance(Transaction, parent.transaction_id)
    children = [o for o in orders_where(transaction_id=parent.transaction_id)
                if o.parent_order_id == parent.id]
    return parent.expiry, txn.expiry, {(c.side, c.expiry) for c in children}


def test_the_two_runtimes_persist_IDENTICAL_per_leg_rows(tmp_path, _seed_backtest_credentials):
    """The interface path and the backtest override, same legs, same result.

    Restores the seeded-credentials DB on exit -- see ``written_pmcc``'s docstring for why a
    raw ``configure_db`` here must not outlive this test.
    """
    from ba2_common.core import db

    try:
        db.configure_db(str(tmp_path / "live_side.sqlite"))
        db.init_db()
        live_side = _submit_and_describe(_Account())

        db.configure_db(str(tmp_path / "backtest_side.sqlite"))
        db.init_db()
        backtest_account = _backtest_shaped_account()
        backtest_side = _submit_and_describe(backtest_account)

        assert backtest_side == live_side
        assert live_side[0] is None and live_side[1] is None, \
            "neither runtime may stamp a single expiry on a two-expiry structure"
        assert live_side[2] == {(OrderDirection.BUY, LONG_EXPIRY),
                                (OrderDirection.SELL, SHORT_EXPIRY)}
        assert backtest_account.cache_invalidations == 1, \
            "the override's own job (dropping the order cache) still happened"
    finally:
        db.configure_db(str(_seed_backtest_credentials))


def test_the_guard_refuses_an_undeclared_structure_in_the_BACKTEST_runtime_too(
        tmp_path, _seed_backtest_credentials):
    """Fail-closed is not a live-only property. The override must not open a hole.

    Restores the seeded-credentials DB on exit -- see ``written_pmcc``'s docstring for why a
    raw ``configure_db`` here must not outlive this test.
    """
    from sqlmodel import Session, select

    from ba2_common.core import db

    try:
        db.configure_db(str(tmp_path / "backtest_refusal.sqlite"))
        db.init_db()
        account = _backtest_shaped_account()

        with pytest.raises(ValueError, match="single expiry"):
            account.submit_option_order(_pmcc_legs(), quantity=1, order_type="limit",
                                        limit_price=18.0, option_strategy="calendar_spread")

        with Session(db.get_engine()) as session:
            assert session.exec(select(TradingOrder)).all() == [], \
                "a refused structure left order rows behind in the backtest runtime"
            assert session.exec(select(Transaction)).all() == [], \
                "a refused structure left a Transaction behind in the backtest runtime"
        assert account.cache_invalidations == 0, \
            "a refusal must not even reach the override's post-step"
    finally:
        db.configure_db(str(_seed_backtest_credentials))


def test_the_shared_accessor_gives_those_same_two_answers_directly(written_pmcc):
    """The accessor, called directly on the same legs, is where both numbers come from."""
    from ba2_common.core.db import get_instance
    from ba2_common.core.option_expiry import ExpiryLeg
    from ba2_common.core.OptionRiskManagement import build_structure

    structure = build_structure(get_instance(Transaction, written_pmcc.txn_id))
    legs = [ExpiryLeg(expiry=l.expiry, net_qty=l.net_qty) for l in structure.legs]

    roll = resolve_structure_expiry(legs, strategy=PMCC, rule=EXPIRY_RULE_ROLL_WINDOW)
    exit_ = resolve_structure_expiry(legs, strategy=PMCC, rule=EXPIRY_RULE_STRUCTURE_EXIT)

    assert (roll.expiry, exit_.expiry) == (SHORT_EXPIRY, LONG_EXPIRY)
    assert roll.rule_applied == EXPIRY_RULE_ROLL_WINDOW
    assert exit_.rule_applied == EXPIRY_RULE_STRUCTURE_EXIT


# ---------------------------------------------------------------------------
# THE FILL SEAM (plan Task 6, 2026-09-02 review): one function, two runtimes
# ---------------------------------------------------------------------------
def test_the_transaction_refresh_is_ONE_implementation_both_runtimes_reach():
    """A two-expiry structure's max loss MOVES after entry -- the long's debit less every
    credit its rolling overlay banks -- so it has to be re-derived when a fill is observed.
    That is only safe if "when a fill is observed" is ONE place.

    It is. ``ReadOnlyAccountInterface.refresh_transactions`` is the single implementation:
    the backtest account overrides it ONLY to re-stamp wall-clock dates onto the simulated
    clock and delegates the lifecycle to ``super()``, and no live account overrides it at all
    -- their broker-specific work is ``refresh_orders``, which lands the fills this loop then
    rolls into the transaction. So the synchronous backtest fill and the asynchronous live one
    reach the same derivation.
    """
    import inspect

    from app.services.backtest.backtest_account import BacktestAccount
    from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface

    # The DERIVATION itself is defined once and nowhere overridden -- so both runtimes run the
    # same function object, not two that happen to agree.
    assert (BacktestAccount._restamp_declared_multi_expiry_max_loss
            is ReadOnlyAccountInterface._restamp_declared_multi_expiry_max_loss)

    # And the backtest's own override of the surrounding loop DELEGATES rather than reimplements.
    src = inspect.getsource(BacktestAccount.refresh_transactions)
    assert "super().refresh_transactions()" in src, (
        "BacktestAccount stopped delegating the transaction lifecycle; the two runtimes would "
        "then observe fills through two different implementations")


def test_the_live_account_does_not_override_the_transaction_refresh():
    """The identity half, for the runtime this repo cannot execute."""
    import importlib

    for module, cls in (("ba2_trade_platform.modules.accounts.AlpacaAccount", "AlpacaAccount"),
                        ("ba2_trade_platform.modules.accounts.TastyTradeAccount",
                         "TastyTradeAccount")):
        try:
            account_cls = getattr(importlib.import_module(module), cls)
        except Exception:  # noqa: BLE001 -- a broker SDK this env lacks is not a finding
            continue
        assert "refresh_transactions" not in vars(account_cls), (
            f"{cls} overrides refresh_transactions: the live fill would reach a different "
            f"implementation from the backtest's")
