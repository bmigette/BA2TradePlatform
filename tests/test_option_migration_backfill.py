"""Regression test for the option-fields migration's asset_class backfill.

Bug: ``alembic/versions/08de6c7b6eed_add_option_fields_to_tradingorder.py``
originally backfilled ``tradingorder.asset_class`` with the lowercase enum
VALUE ``'equity'``. But SQLAlchemy/SQLModel persists this codebase's str-enums
by their NAME (uppercase), so the ORM stores/expects ``'EQUITY'``. A migrated DB
with ``'equity'`` therefore raises ``LookupError: 'equity' is not among the
defined enum values`` on any ORM load of a TradingOrder.

The plain unit suite never caught this because it builds the schema via
``SQLModel.metadata.create_all`` (which writes ``'EQUITY'``) and never runs the
migration backfill.

This test runs the REAL migration's ``upgrade()`` against a pre-option legacy
schema with a NULL-asset_class row, then asserts the backfilled value is the
ORM-loadable enum NAME and that the ORM can actually load the row. It FAILS if
the migration backfills ``'equity'`` and PASSES once it backfills ``'EQUITY'``.

Why importlib instead of a full ``alembic upgrade`` from an empty DB: in this
codebase the base schema is created by ``SQLModel.metadata.create_all``, not by
an initial Alembic migration. The earliest migrations (e.g. 2b4cf753ba81)
assume the tables already exist, so ``alembic upgrade`` against an EMPTY sqlite
DB fails before ever reaching the option migration. We therefore build a
realistic pre-option ``tradingorder`` table by hand and execute the real
migration module's ``upgrade()`` bound to a live Alembic Operations context —
which runs the exact same DDL + backfill SQL as a production migration.
"""
import importlib.util
import os
import sqlite3

import sqlalchemy
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlmodel import Session, SQLModel, create_engine, select


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_PATH = os.path.join(
    REPO,
    "alembic",
    "versions",
    "08de6c7b6eed_add_option_fields_to_tradingorder.py",
)

# The 9 columns the option migration is expected to add.
_OPTION_COLUMNS = {
    "asset_class", "contract_symbol", "option_type", "strike", "expiry",
    "underlying_symbol", "multiplier", "position_intent", "option_strategy",
}

# Indexes the option migration creates (so they must NOT pre-exist).
_OPTION_INDEXES = {
    "ix_tradingorder_asset_class",
    "ix_tradingorder_contract_symbol",
    "ix_tradingorder_underlying_symbol",
}


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("opt_fields_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_preoption_db(db_path):
    """Create a faithful pre-option tradingorder schema with one legacy row.

    We materialize the FULL current schema via SQLModel.metadata.create_all
    (which is exactly how the app/tests bootstrap the DB), then strip the 9
    option columns and their 3 indexes so the table looks like it did before the
    option-fields migration. This keeps every other (non-option) column present
    so the ORM can SELECT the row after the migration runs.
    """
    # Import models so all tables are registered on SQLModel.metadata.
    from ba2_trade_platform.core import models  # noqa: F401

    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()

    conn = sqlite3.connect(db_path)
    # Drop option indexes first (SQLite refuses to drop indexed columns).
    for idx in _OPTION_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {idx}")
    # Strip the option columns to recreate the pre-option shape.
    for col in _OPTION_COLUMNS:
        conn.execute(f"ALTER TABLE tradingorder DROP COLUMN {col}")
    cols_before = {row[1] for row in conn.execute("PRAGMA table_info(tradingorder)")}
    assert not (_OPTION_COLUMNS & cols_before), "fixture must be pre-option"
    # Insert a legacy row (no asset_class column exists yet).
    conn.execute(
        "INSERT INTO tradingorder "
        "(account_id, symbol, quantity, side, order_type, open_type, status) "
        "VALUES (1, 'AAPL', 10, 'BUY', 'MARKET', 'MANUAL', 'FILLED')"
    )
    conn.commit()
    conn.close()


def test_option_fields_migration_backfills_orm_loadable_asset_class(tmp_path):
    db_path = str(tmp_path / "legacy.sqlite")

    # 1. Build a realistic pre-option tradingorder table and insert a legacy row.
    #    The legacy row has NO asset_class column yet (it does not exist), so the
    #    migration's backfill (UPDATE ... WHERE asset_class IS NULL) must set it.
    _build_preoption_db(db_path)

    # 2. Run the REAL option-fields migration upgrade() against this DB. We bind
    #    the migration module's module-level `op`/`sa` to a live Alembic
    #    Operations context so the exact production DDL + backfill SQL executes.
    module = _load_migration_module()
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        ctx = MigrationContext.configure(connection, opts={"as_batch": True})
        module.op = Operations(ctx)
        module.sa = sqlalchemy
        module.upgrade()

    # 3. The migration must have added the option columns and backfilled the
    #    asset_class to the ORM-loadable enum NAME 'EQUITY' (not 'equity').
    conn = sqlite3.connect(db_path)
    cols_after = {row[1] for row in conn.execute("PRAGMA table_info(tradingorder)")}
    raw = conn.execute("SELECT asset_class FROM tradingorder").fetchone()[0]
    conn.close()
    assert _OPTION_COLUMNS.issubset(cols_after), (
        f"migration did not add all option columns; have {sorted(cols_after)}"
    )
    assert raw == "EQUITY", (
        f"migration backfilled {raw!r}; must be 'EQUITY' (enum NAME) so the ORM "
        f"can load it"
    )

    # 4. Load via the ORM — must NOT raise LookupError and must map to EQUITY.
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import AssetClass

    orm_engine = create_engine(f"sqlite:///{db_path}")
    with Session(orm_engine) as session:
        order = session.exec(select(TradingOrder)).first()
    assert order is not None
    assert order.asset_class == AssetClass.EQUITY
