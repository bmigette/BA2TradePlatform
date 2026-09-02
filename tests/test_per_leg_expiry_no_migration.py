"""Per-leg expiries needed NO migration — the proof, and the fixture-DB half of it.

Plan ``docs/superpowers/plans/2026-08-31-options-grid2-convex-earnings-impl.md`` Task 6-PRE,
requirement 5. That requirement asked for a migration tested forward and backward on a
fixture DB with existing single-expiry rows, and allowed the alternative it turned out to
need: **a "no migration needed" proof**, because per-leg expiry storage already existed.

THE EVIDENCE, RESTATED AS ASSERTIONS
------------------------------------
``TradingOrder.expiry`` has been a real, nullable column since alembic revision
``08de6c7b6eed`` ("add option fields to tradingorder"), and ``submit_option_order`` writes
one child row per leg carrying its own date, linked by ``parent_order_id``.
``Transaction.expiry`` was therefore never the STORAGE — it is a denormalised structure-level
SUMMARY of a set already recorded per leg. Task 6-PRE only had to supply a rule for reading
those legs when they disagree, which is code, not schema.

So instead of "upgrade then downgrade", the properties to pin are:

1. alembic still has exactly ONE head, and it is the same one as before this task;
2. this task added no revision file;
3. the ``transaction`` table gained no column — pinned against a frozen list, so ANY future
   addition fails here and says out loud that a migration is owed;
4. the per-leg column this task depends on already exists, and its revision predates the
   head — i.e. the storage was not quietly added by this task under another name.

THE FIXTURE-DB HALF
-------------------
"Those rows read identically after upgrade" still has real content with no migration: there
is exactly one schema, so a database written before this task is the same file afterwards,
and what must be shown is that the NEW readers give the OLD answers on it. ``legacy_db``
below is a scratch SQLite holding an ordinary single-expiry bull call spread — a transaction,
a stamped parent and two legs, exactly the rows the pre-task writer produced — and the reads
are asserted against explicit expected numbers rather than against each other.

No real database is touched: every engine here is a scratch file under ``tmp_path``.
"""
from __future__ import annotations

import pathlib
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSIONS_DIR = REPO_ROOT / "alembic" / "versions"

#: The alembic head this task INHERITED and must leave untouched.
EXPECTED_HEAD = "b7f3d21c98ae"

#: The revision that added the per-leg column, long before this task.
PER_LEG_COLUMN_REVISION = "08de6c7b6eed"

#: Every column on ``transaction``. Frozen deliberately: this task's whole claim is that it
#: added none, and a bare "no new column" assertion cannot be written without a baseline.
#: If you are here because you added one, that is fine — write the alembic revision (with a
#: downgrade), then add the name below.
TRANSACTION_COLUMNS = {
    "asset_class", "close_date", "close_price", "close_reason", "created_at", "expert_id",
    "expiry", "id", "meta_data", "multiplier", "open_date", "open_price", "option_strategy",
    "quantity", "side", "sl_manual_override", "status", "stop_loss", "symbol", "take_profit",
    "tp_manual_override",
}

SIM_AS_OF = datetime(2024, 6, 15, 15, 30, tzinfo=timezone.utc)
SIM_TODAY = date(2024, 6, 15)
LEGACY_EXPIRY = SIM_TODAY + timedelta(days=30)      # an ordinary single-expiry structure


# ===========================================================================
# 1-2. alembic is untouched
# ===========================================================================
def test_alembic_still_has_exactly_one_head_and_it_is_unchanged():
    """Two heads are not cosmetic — ``alembic upgrade head`` refuses outright."""
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    heads = list(script.get_heads())

    assert heads == [EXPECTED_HEAD], (
        f"alembic head moved to {heads}. Task 6-PRE added no migration; if a later change "
        f"needs one, update EXPECTED_HEAD here and say why in the commit.")


def test_this_task_added_no_revision_file():
    """A revision mentioning per-leg expiries would contradict the whole design note."""
    offenders = []
    for path in VERSIONS_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        if "per-leg expiry" in text or "per_leg_expiry" in text or "leg_expiries" in text:
            offenders.append(path.name)

    assert offenders == [], (
        f"a per-leg-expiry revision exists ({offenders}) — either the no-migration design "
        f"changed, or a column was added without updating this proof")


# ===========================================================================
# 3. the Transaction table gained no column
# ===========================================================================
def test_the_transaction_table_gained_no_column():
    from ba2_common.core.models import Transaction

    actual = {c.name for c in Transaction.__table__.columns}
    assert actual == TRANSACTION_COLUMNS, (
        f"the transaction table changed shape. Added: {sorted(actual - TRANSACTION_COLUMNS)}; "
        f"removed: {sorted(TRANSACTION_COLUMNS - actual)}. `transaction` is a LIVE money "
        f"record: any change here owes an alembic revision WITH a downgrade.")


def test_transaction_expiry_is_still_a_single_nullable_date():
    """It keeps its single-value meaning, and it is still optional."""
    from ba2_common.core.models import Transaction

    expiry_like = [c.name for c in Transaction.__table__.columns if "expir" in c.name]
    assert expiry_like == ["expiry"], \
        "a second expiry column on `transaction` is exactly what the design ruled out"
    assert Transaction.__table__.columns["expiry"].nullable is True


# ===========================================================================
# 4. the per-leg storage this task relies on predates it
# ===========================================================================
def test_the_per_leg_column_already_exists_and_is_nullable():
    from ba2_common.core.models import TradingOrder

    column = TradingOrder.__table__.columns["expiry"]
    assert column.nullable is True, \
        "per-leg expiry must stay optional — the flatten path rebuilds legs with expiry=None"


def test_the_per_leg_column_was_added_by_an_earlier_revision():
    """Proves the storage was inherited, not quietly introduced by this task."""
    matches = list(VERSIONS_DIR.glob(f"{PER_LEG_COLUMN_REVISION}_*.py"))
    assert len(matches) == 1, f"revision {PER_LEG_COLUMN_REVISION} not found"

    text = matches[0].read_text(encoding="utf-8", errors="replace")
    assert 'op.add_column("tradingorder", sa.Column("expiry"' in text, \
        "the revision that was supposed to add tradingorder.expiry no longer does"

    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    ancestry = {rev.revision for rev in script.iterate_revisions(EXPECTED_HEAD, "base")}
    assert PER_LEG_COLUMN_REVISION in ancestry, \
        "the per-leg column's revision is not an ancestor of head — it may never be applied"


# ===========================================================================
# 5. the fixture DB: pre-existing single-expiry rows read IDENTICALLY
# ===========================================================================
@pytest.fixture
def legacy_db(tmp_path):
    """A scratch database holding one ordinary, pre-task, single-expiry structure.

    A bull call spread: the transaction stamped with the structure expiry, the parent order
    stamped with it too, and two legs both carrying it — precisely what the writer produced
    before Task 6-PRE existed, and the rows the requirement calls "existing single-expiry
    rows".
    """
    from ba2_common.core import db
    from ba2_common.core.models import Transaction, TradingOrder
    from ba2_common.core.types import (AssetClass, OptionRight, OrderDirection, OrderStatus,
                                       OrderType, TransactionStatus)

    db.configure_db(str(tmp_path / "legacy_single_expiry.sqlite"))
    db.init_db()

    txn_id = db.add_instance(Transaction(
        symbol="AAPL", quantity=1, side=OrderDirection.BUY, status=TransactionStatus.OPENED,
        open_date=SIM_AS_OF - timedelta(days=10), asset_class=AssetClass.OPTION,
        option_strategy="bull_call_spread", multiplier=100, expiry=LEGACY_EXPIRY))

    parent = TradingOrder(
        account_id=1, symbol="AAPL", quantity=1, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, transaction_id=txn_id,
        asset_class=AssetClass.OPTION, underlying_symbol="AAPL",
        option_strategy="bull_call_spread", multiplier=100, expiry=LEGACY_EXPIRY,
        created_at=datetime.now(timezone.utc))
    db.add_instance(parent, expunge_after_flush=True)

    for contract, side, price in (("AAPL_LONG", OrderDirection.BUY, 6.0),
                                  ("AAPL_SHORT", OrderDirection.SELL, 2.0)):
        db.add_instance(TradingOrder(
            account_id=1, symbol="AAPL", quantity=1, side=side, order_type=OrderType.MARKET,
            status=OrderStatus.FILLED, transaction_id=txn_id, asset_class=AssetClass.OPTION,
            contract_symbol=contract, underlying_symbol="AAPL", option_type=OptionRight.CALL,
            strike=100.0, multiplier=100, expiry=LEGACY_EXPIRY, open_price=price,
            filled_qty=1, parent_order_id=parent.id,
            created_at=datetime.now(timezone.utc)), expunge_after_flush=True)

    return SimpleNamespace(txn_id=txn_id, parent=parent, db=db)


def test_the_legacy_rows_still_hold_exactly_one_expiry(legacy_db):
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.trade_store import orders_where

    txn = get_instance(Transaction, legacy_db.txn_id)
    rows = orders_where(transaction_id=legacy_db.txn_id)

    assert txn.expiry == LEGACY_EXPIRY, "Transaction.expiry keeps its single-value meaning"
    assert {o.expiry for o in rows} == {LEGACY_EXPIRY}


def test_the_roll_window_reader_gives_the_pre_task_answer(legacy_db):
    """30 days — the number this structure reported before Task 6-PRE, unchanged."""
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.option_lifecycle import _dte
    from ba2_common.core.OptionRiskManagement import build_structure

    structure = build_structure(get_instance(Transaction, legacy_db.txn_id))
    assert _dte(structure, SIM_TODAY) == (30, "")


def test_the_structure_exit_reader_gives_the_pre_task_answer(legacy_db):
    from ba2_common.core.TradeConditions import DaysToExpiryCondition

    cond = DaysToExpiryCondition(
        account=SimpleNamespace(id=1), instrument_name="AAPL",
        expert_recommendation=SimpleNamespace(created_at=SIM_AS_OF, instance_id=1,
                                              symbol="AAPL"),
        operator_str="<=", value=21, existing_order=legacy_db.parent)

    assert cond.evaluate() is False              # 30 is not <= 21
    assert cond.get_calculated_value() == 30
    assert cond.unknown_reason is None


def test_no_leg_rule_is_exercised_on_a_legacy_single_expiry_structure(legacy_db):
    """THE byte-identical property, stated directly: the new machinery does not merely agree
    with the old answer, it never reaches the branch that could disagree."""
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.option_expiry import (EXPIRY_RULE_ROLL_WINDOW,
                                               EXPIRY_RULE_STRUCTURE_EXIT, ExpiryLeg,
                                               resolve_structure_expiry)
    from ba2_common.core.OptionRiskManagement import build_structure

    structure = build_structure(get_instance(Transaction, legacy_db.txn_id))
    legs = [ExpiryLeg(expiry=l.expiry, net_qty=l.net_qty) for l in structure.legs]

    for rule in (EXPIRY_RULE_ROLL_WINDOW, EXPIRY_RULE_STRUCTURE_EXIT):
        res = resolve_structure_expiry(legs, strategy=structure.strategy, rule=rule,
                                       declared_expiries=(structure.expiry,))
        assert res.expiry == LEGACY_EXPIRY
        assert res.rule_applied is None, \
            f"{rule} exercised a leg rule on a single-expiry structure"
        assert res.conflict == () and res.missing is False
