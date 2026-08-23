"""add the five portfolio allocation tables

Revision ID: f1c8a24b7e05
Revises: f1a7c2e9b4d0
Create Date: 2026-08-20

Creates portfolio_allocation_config, portfolio_allocation_label,
portfolio_allocation_symbol, portfolio_income_event and portfolio_allocation_run.
Chained AFTER the instrument merge + unique index so the destructive data
migration can be run and inspected on its own, without the schema additions
riding along.

Index names are the ones SQLAlchemy itself emits for these models
(``ix_<table>_<column>``), so init_db()'s create_all on a fresh DB and Alembic on
an existing one agree. Foreign keys are declarative only -- the live DB runs with
PRAGMA foreign_keys = 0 -- so account deletion must clear these tables
explicitly (see portfolio_allocation_store.delete_account_allocation_data).

Amended before ever being applied (the live DB is still two revisions behind, at
d5e1b9a3c842, and no row of these tables exists anywhere) to add
portfolio_allocation_run.income_consumed_at / income_consumed_events: the
per-run idempotency guard for income consumption. Amending in place is only
legal because nothing has run this revision yet; once it ships, the same change
costs a second revision.

Amended a SECOND time, on the same grounds (still unapplied; nothing chains off
it), to rename portfolio_allocation_run.submitted_buy_value /
submitted_sell_value to filled_buy_value / filled_sell_value. The columns hold
what the broker FILLED, not what the platform submitted, and the income ledger is
spent against them. A developer database that `init_db()` built with create_all
on an older checkout has the OLD column names and would be skipped by
_create_table_if_absent, so _rename_column_if_present fixes it up in place.

Amended a THIRD time, on the same grounds, to add
portfolio_allocation_label.previous_target_pct and
portfolio_allocation_symbol.previous_weight_pct: one generation of "what did I
allocate with last time", which is what the wizard's Load-last button reads.
Re-verified read-only on the live DB on 2026-08-23 before amending --
alembic_version = d5e1b9a3c842, zero portfolio_% tables, f1c8a24b7e05 still the
only head -- so this is again free. BOTH ARE NULLABLE: NULL is how "there is no
last" is spelled, and a 0.0 default would be indistinguishable from "the last run
allocated nothing to this". _add_column_if_absent covers the developer database
whose create_all built these tables before the columns existed; the CREATEs below
cover everyone else.

IDEMPOTENT ON PURPOSE
=====================
Every create is guarded by a `has_table` / `has_index` check, because THIS
REVISION RACES init_db(). Starting the app once on this branch runs
SQLModel.metadata.create_all(), which materialises all five tables outside
alembic on any database that is not brand new (init_db only stamps head when the
schema was ABSENT beforehand -- see _schema_is_absent). An unguarded
`op.create_table` then dies with "table portfolio_allocation_config already
exists" and the revision can never be applied.

Partial pre-existence was the worse half. With only `portfolio_income_event`
present, the unguarded version created config, label and symbol, then hit the
duplicate and left all three behind while alembic_version stayed at
f1a7c2e9b4d0 -- verified on a throwaway copy. Alembic reports SQLite as
non-transactional DDL, so nothing rolled back, and the retry then failed one
table EARLIER, on config. That state has no exit. Guarding each create makes
every re-run converge instead.

PRODUCTION RUNBOOK
==================
For ~/Documents/ba2/trade/db.sqlite. Read step 1 before touching anything.

1. WHICH SITUATION ARE YOU IN? Ask the database, not your memory:

       sqlite3 db.sqlite "SELECT name FROM sqlite_master WHERE type='table'
                          AND name LIKE 'portfolio_%' ORDER BY name;"
       venv/bin/python -m alembic current

   NOTHING listed, and current is f1a7c2e9b4d0 -> the ordinary path, step 3.
   ALL FIVE listed, and current is behind -> the app already built them with
   create_all; step 2 applies and is the cheaper move.
   SOME of the five -> a previous unguarded run stranded them. Step 3 still
   works; that is the whole point of the guards.

2. IF create_all ALREADY BUILT THEM, `alembic stamp f1c8a24b7e05` IS LEGITIMATE:

       venv/bin/python -m alembic stamp f1c8a24b7e05

   It is legitimate because the schema this revision builds is PROVEN equal to
   the one create_all builds: tests/test_portfolio_allocation_migration.py runs
   alembic's own autogenerate comparator over the migrated database against
   SQLModel.metadata and asserts ZERO differences -- types, nullability,
   uniqueness, indexes -- not just matching names. Stamping is recording a fact,
   not skipping work. `upgrade` (step 3) reaches the same end state and stamps
   as part of it, so use whichever you prefer; stamp is simply the one that
   emits no DDL at all.

   Do not be alarmed by a raw `SELECT sql FROM sqlite_master` diff between a
   migrated and a create_all database: on three of the five tables the FOREIGN
   KEY and UNIQUE clauses come out in the opposite ORDER inside the CREATE TABLE
   text. Same columns, same constraints, same semantics -- SQLite stores the
   statement verbatim, so the text differs and the schema does not.

3. THE ORDINARY PATH. Safe from any of the three states above:

       venv/bin/python -m alembic upgrade f1c8a24b7e05

   Tables that exist are skipped with a printed line saying so; tables that do
   not are created. Note the parent revision, f1a7c2e9b4d0, is the DESTRUCTIVE
   instrument merge -- if you are behind it, follow ITS runbook first and do not
   let it ride along unread behind an `upgrade head`.

4. VERIFY:

       sqlite3 db.sqlite "SELECT count(*) FROM sqlite_master WHERE type='table'
                          AND name LIKE 'portfolio_%';"          -> 5
       venv/bin/python -m alembic current                        -> f1c8a24b7e05

5. IF IT FAILS, JUST RE-RUN IT. Nothing here deletes or rewrites a row; the
   worst a half-finished run can leave behind is some of the five tables, and
   the next run skips exactly those. Do NOT hand-create the missing tables --
   create_all and this revision agree today (step 2 proves it) and a hand-typed
   CREATE TABLE is how they would stop agreeing.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1c8a24b7e05'
down_revision: Union[str, Sequence[str], None] = 'f1a7c2e9b4d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_table_if_absent(table_name: str, *columns) -> None:
    """``op.create_table`` unless the table is already there.

    A FRESH inspector every call, deliberately: an Inspector memoises its
    reflection, and this function creates tables between calls, so a shared one
    would answer from a snapshot taken before the first CREATE and re-create a
    table it had just made.
    """
    if sa.inspect(op.get_bind()).has_table(table_name):
        # print(), not logger: alembic's fileConfig can disable the app loggers,
        # and an operator following the runbook needs to see what was skipped.
        print(f"[pf-allocation] {table_name} already exists -- skipped")
        return
    op.create_table(table_name, *columns)


def _create_index_if_absent(index_name: str, table_name: str, columns,
                            *, unique: bool = False) -> None:
    """``op.create_index`` unless that index is already on that table.

    Indexes need their own guard rather than riding on the table's: a database
    built by create_all has both, but one stranded by a previously failed run of
    THIS revision can have the table and only some of its indexes.
    """
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        raise RuntimeError(
            f"cannot index {table_name}.{index_name}: the table is missing, which "
            "means the CREATE TABLE above silently did nothing")
    if inspector.has_index(table_name, index_name):
        print(f"[pf-allocation] index {index_name} already exists -- skipped")
        return
    op.create_index(index_name, table_name, columns, unique=unique)


def _add_column_if_absent(table_name: str, column: sa.Column) -> None:
    """``op.add_column`` unless the column is already there.

    The sibling of _rename_column_if_present, for the other way an older create_all
    leaves a table behind: right name, missing column. _create_table_if_absent SKIPS
    a table that exists, so a column added to the CREATE above alone would never
    reach a developer database that init_db() built before the column existed --
    the model and the schema would then disagree forever and every read of it would
    die on "no such column".

    A FRESH inspector every call, for the same reason _create_table_if_absent takes
    one: an Inspector memoises its reflection and this function changes the schema
    between calls.

    Nothing to do when the table is absent -- the CREATE above already included the
    column. Nothing to do when the column is present -- which is what makes
    re-running this revision safe, since ALTER TABLE ADD COLUMN of an existing
    column is a hard error on SQLite.

    Only ever adds NULLABLE columns here. ADD COLUMN with NOT NULL and no server
    default is rejected outright by SQLite on a non-empty table, and back-filling a
    "previous target" nobody ever set would invent history.
    """
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return
    names = {existing["name"] for existing in inspector.get_columns(table_name)}
    if column.name in names:
        print(f"[pf-allocation] {table_name}.{column.name} already exists -- skipped")
        return
    op.add_column(table_name, column)
    print(f"[pf-allocation] added {table_name}.{column.name}")


def _rename_column_if_present(table_name: str, old_name: str, new_name: str) -> None:
    """Rename a column that an older create_all left behind under its old name.

    Converges the three states this revision can meet: table absent (nothing to
    do -- the CREATE above already used the new name), table present with the new
    name (nothing to do), table present with the OLD name (rename it). Anything
    else is a schema we do not recognise and must not guess at.

    batch_alter_table because SQLite has no ALTER COLUMN: alembic copies the table
    and its indexes. Only ever reached on a developer database.
    """
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return
    names = {column["name"] for column in inspector.get_columns(table_name)}
    if new_name in names:
        print(f"[pf-allocation] {table_name}.{new_name} already named correctly -- skipped")
        return
    if old_name not in names:
        raise RuntimeError(
            f"{table_name} has neither {old_name} nor {new_name}; refusing to guess")
    with op.batch_alter_table(table_name, schema=None) as batch_op:
        batch_op.alter_column(old_name, new_column_name=new_name,
                              existing_type=sa.Float(), existing_nullable=False)
    print(f"[pf-allocation] renamed {table_name}.{old_name} -> {new_name}")


def upgrade() -> None:
    _create_table_if_absent(
        "portfolio_allocation_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("valuation_mode", sa.String(), nullable=False),
        sa.Column("allow_fractional", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # unique=True + index=True on the model emits ONE unique index, not a
    # UniqueConstraint -- mirror that exactly so create_all and Alembic agree.
    _create_index_if_absent("ix_portfolio_allocation_config_account_id",
                            "portfolio_allocation_config", ["account_id"], unique=True)
    _create_index_if_absent("ix_portfolio_allocation_config_updated_at",
                            "portfolio_allocation_config", ["updated_at"])

    _create_table_if_absent(
        "portfolio_allocation_label",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("target_pct", sa.Float(), nullable=False),
        # NULLABLE and never back-filled: NULL is "there is no last", which is a
        # different answer from 0.0 ("the last run allocated nothing to this").
        sa.Column("previous_target_pct", sa.Float(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "label", name="uix_pf_alloc_label_account_label"),
    )
    _create_index_if_absent("ix_portfolio_allocation_label_account_id", "portfolio_allocation_label", ["account_id"])
    _create_index_if_absent("ix_portfolio_allocation_label_label", "portfolio_allocation_label", ["label"])
    _create_index_if_absent("ix_portfolio_allocation_label_created_at", "portfolio_allocation_label", ["created_at"])

    _create_table_if_absent(
        "portfolio_allocation_symbol",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("weight_pct", sa.Float(), nullable=False),
        sa.Column("previous_weight_pct", sa.Float(), nullable=True),
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "label", "symbol",
                            name="uix_pf_alloc_symbol_account_label_symbol"),
    )
    _create_index_if_absent("ix_portfolio_allocation_symbol_account_id", "portfolio_allocation_symbol", ["account_id"])
    _create_index_if_absent("ix_portfolio_allocation_symbol_label", "portfolio_allocation_symbol", ["label"])
    _create_index_if_absent("ix_portfolio_allocation_symbol_symbol", "portfolio_allocation_symbol", ["symbol"])
    _create_index_if_absent("ix_portfolio_allocation_symbol_created_at", "portfolio_allocation_symbol", ["created_at"])

    _create_table_if_absent(
        "portfolio_income_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=True),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("consumed_amount", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "external_id", name="uix_pf_income_account_externalid"),
    )
    _create_index_if_absent("ix_portfolio_income_event_account_id", "portfolio_income_event", ["account_id"])
    _create_index_if_absent("ix_portfolio_income_event_external_id", "portfolio_income_event", ["external_id"])
    _create_index_if_absent("ix_portfolio_income_event_event_date", "portfolio_income_event", ["event_date"])
    _create_index_if_absent("ix_portfolio_income_event_symbol", "portfolio_income_event", ["symbol"])
    _create_index_if_absent("ix_portfolio_income_event_created_at", "portfolio_income_event", ["created_at"])

    _create_table_if_absent(
        "portfolio_allocation_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("scope_label", sa.String(), nullable=True),
        sa.Column("base_notional", sa.Float(), nullable=False),
        sa.Column("available_buying_power", sa.Float(), nullable=False),
        sa.Column("allow_fractional", sa.Boolean(), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=True),
        sa.Column("filled_buy_value", sa.Float(), nullable=False),
        sa.Column("filled_sell_value", sa.Float(), nullable=False),
        sa.Column("order_ids", sa.JSON(), nullable=True),
        # Income-ledger replay guard. NULL = this run has never consumed income;
        # a timestamp = it has, exactly once. Written in the SAME transaction as
        # the portfolio_income_event updates (see finalise_allocation_run), so
        # the check and the spend cannot interleave.
        sa.Column("income_consumed_at", sa.DateTime(), nullable=True),
        sa.Column("income_consumed_events", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_index_if_absent("ix_portfolio_allocation_run_account_id", "portfolio_allocation_run", ["account_id"])
    _create_index_if_absent("ix_portfolio_allocation_run_mode", "portfolio_allocation_run", ["mode"])
    _create_index_if_absent("ix_portfolio_allocation_run_created_at", "portfolio_allocation_run", ["created_at"])

    _rename_column_if_present("portfolio_allocation_run",
                              "submitted_buy_value", "filled_buy_value")
    _rename_column_if_present("portfolio_allocation_run",
                              "submitted_sell_value", "filled_sell_value")

    # For the developer database whose create_all built these two tables before
    # the previous_* columns existed. A no-op everywhere else.
    _add_column_if_absent("portfolio_allocation_label",
                          sa.Column("previous_target_pct", sa.Float(), nullable=True))
    _add_column_if_absent("portfolio_allocation_symbol",
                          sa.Column("previous_weight_pct", sa.Float(), nullable=True))


def downgrade() -> None:
    """Drop the five tables. Guarded for the same reason upgrade() is.

    A downgrade run against a database where only some of them survive -- the
    exact wreckage the unguarded upgrade used to leave -- must clean up what is
    there instead of dying on the first one that is not.
    """
    for table_name in ("portfolio_allocation_run",
                       "portfolio_income_event",
                       "portfolio_allocation_symbol",
                       "portfolio_allocation_label",
                       "portfolio_allocation_config"):
        if sa.inspect(op.get_bind()).has_table(table_name):
            op.drop_table(table_name)
        else:
            print(f"[pf-allocation] {table_name} is already gone -- skipped")
