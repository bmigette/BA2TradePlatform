"""``Transaction`` records the INTENT of an option position, not the contract.

Before these three columns the only tell that a Transaction row was an option was
``multiplier == 100`` -- a coincidence of P&L arithmetic standing in for a fact.
``asset_class`` states it, ``option_strategy`` says which structure was intended,
and ``expiry`` says when it ends.

``strike`` is deliberately NOT here: it is meaningful for a single leg and
misleading for a four-leg condor. Strikes live on the legs (``TradingOrder``).
"""
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session, SQLModel, select

from ba2_common.core.models import Transaction
from ba2_common.core.types import AssetClass, OrderDirection


# --- the intent itself ------------------------------------------------------

def test_transaction_records_the_option_intent():
    """Transaction is the INTENT: underlying + strategy + expiry. Not the contract."""
    txn = Transaction(symbol="ACN", quantity=1, side=OrderDirection.BUY,
                      asset_class=AssetClass.OPTION,
                      option_strategy="bull_call_spread",
                      expiry=date(2026, 8, 21))
    assert txn.asset_class is AssetClass.OPTION
    assert txn.option_strategy == "bull_call_spread"
    assert txn.expiry == date(2026, 8, 21)
    assert txn.symbol == "ACN", "symbol must stay the UNDERLYING, never an OCC string"


def test_an_equity_transaction_defaults_to_equity_and_no_option_fields():
    txn = Transaction(symbol="ACN", quantity=10, side=OrderDirection.BUY)
    assert txn.asset_class is AssetClass.EQUITY
    assert txn.option_strategy is None
    assert txn.expiry is None


def test_strike_is_deliberately_not_on_the_intent():
    """One strike is a fact about a LEG. An iron condor has four, and any single one
    of them recorded here would read as "the" strike of the position."""
    assert "strike" not in Transaction.__table__.columns, (
        "strike belongs on TradingOrder (the execution), not on the intent")


# --- the schema the model declares ------------------------------------------

def test_the_three_intent_columns_sit_immediately_after_multiplier():
    """Position in the class is the only thing that groups them with ``multiplier``.

    ``multiplier`` is the field they replace as the "is this an option?" tell, so
    they are declared next to it deliberately. Appending them at the end of the
    class would pass every name assertion below and quietly scatter the option
    fields across the model.
    """
    names = [column.name for column in Transaction.__table__.columns]
    start = names.index("multiplier")
    assert names[start + 1:start + 4] == ["asset_class", "option_strategy", "expiry"]


def test_the_intent_columns_carry_the_nullability_their_meaning_requires():
    """``asset_class`` is NOT NULL -- every row is either equity or option, there is
    no third state and no "unknown". The other two ARE nullable, because NULL is how
    "this is not an option" is spelled, and a sentinel string or a date far in the
    future would both be inventions.
    """
    columns = Transaction.__table__.columns
    assert columns["asset_class"].nullable is False
    assert columns["option_strategy"].nullable is True
    assert columns["expiry"].nullable is True


def test_asset_class_and_expiry_are_indexed():
    """Both are query keys for the lifecycle pass: "every open option position" and
    "everything expiring inside N days" run on every open_positions trigger."""
    indexed = {column.name for index in Transaction.__table__.indexes
               for column in index.columns}
    assert {"asset_class", "expiry"} <= indexed


# --- how the value actually lands in sqlite ---------------------------------

@pytest.fixture
def scratch_engine(tmp_path):
    """A throwaway sqlite holding only the ``transaction`` table.

    SQLite accepts a foreign key to a table that does not exist, so the parent
    ``expertinstance`` is deliberately not built: this is about one table's storage.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'intent.sqlite'}")
    SQLModel.metadata.create_all(engine, tables=[Transaction.__table__])
    return engine


def _raw_asset_class(engine, txn_id):
    with engine.connect() as connection:
        return connection.execute(
            text('SELECT asset_class FROM "transaction" WHERE id = :i'),
            {"i": txn_id}).scalar_one()


def test_asset_class_is_stored_as_the_enum_NAME_not_its_value(scratch_engine):
    """SQLAlchemy's Enum persists ``.name``, so the stored string is 'OPTION', not
    the enum's value 'option'.

    This is not a detail: a migration default, a backfill or a raw-SQL filter
    written as the *value* produces rows that no ORM query ever matches, and the
    mismatch is invisible until a position goes unmanaged.
    """
    with Session(scratch_engine) as session:
        txn = Transaction(symbol="ACN", quantity=1, side=OrderDirection.BUY,
                          asset_class=AssetClass.OPTION,
                          option_strategy="bull_call_spread",
                          expiry=date(2026, 8, 21))
        session.add(txn)
        session.commit()
        session.refresh(txn)
        txn_id = txn.id

    assert AssetClass.OPTION.value == "option", "the VALUE is lowercase..."
    assert _raw_asset_class(scratch_engine, txn_id) == "OPTION", "...the STORED form is the NAME"


def test_an_equity_row_stores_EQUITY_and_reads_back_as_the_enum(scratch_engine):
    with Session(scratch_engine) as session:
        txn = Transaction(symbol="ACN", quantity=10, side=OrderDirection.BUY)
        session.add(txn)
        session.commit()
        session.refresh(txn)
        txn_id = txn.id

    assert _raw_asset_class(scratch_engine, txn_id) == "EQUITY"
    with Session(scratch_engine) as session:
        reloaded = session.get(Transaction, txn_id)
        assert reloaded.asset_class is AssetClass.EQUITY
        assert reloaded.option_strategy is None
        assert reloaded.expiry is None


def test_the_option_intent_round_trips_through_the_database(scratch_engine):
    """The expiry must come back as a ``date``, not the string sqlite stores."""
    with Session(scratch_engine) as session:
        session.add(Transaction(symbol="ACN", quantity=1, side=OrderDirection.BUY,
                                asset_class=AssetClass.OPTION,
                                option_strategy="iron_condor",
                                expiry=date(2026, 8, 21)))
        session.commit()

    with Session(scratch_engine) as session:
        row = session.exec(select(Transaction)).one()
        assert row.asset_class is AssetClass.OPTION
        assert row.option_strategy == "iron_condor"
        assert row.expiry == date(2026, 8, 21)
        assert row.symbol == "ACN"
