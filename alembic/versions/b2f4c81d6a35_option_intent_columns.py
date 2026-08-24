"""record the option INTENT on transaction: asset_class, option_strategy, expiry

Revision ID: b2f4c81d6a35
Revises: a3f1c07d9e21
Create Date: 2026-08-24

``Transaction`` becomes the INTENT of a position -- "a bull call spread on ACN
expiring 2026-08-21" -- while ``TradingOrder`` stays the EXECUTION, i.e. which
contracts actually filled. Before these three columns the only tell that a
transaction row was an option was ``multiplier = 100``: a coincidence of P&L
arithmetic standing in for a fact.

``transaction.symbol`` is NOT touched and keeps holding the UNDERLYING ticker.
``JobManager._execute_open_positions_analysis`` selects ``distinct
Transaction.symbol`` and submits one market analysis per value, so an OCC contract
string there would be analysed as a ticker.

``strike`` is deliberately NOT added: it is meaningful for a single leg and
misleading for a four-leg condor. Strikes stay on the legs.

THE ENUM IS STORED BY NAME
==========================
``asset_class`` is a SQLModel ``str`` enum, and SQLAlchemy persists ``.name``, so
the stored string is 'EQUITY' -- not the enum's value, 'equity'. The server default
below is therefore 'EQUITY'. Written as the value it would populate every existing
row with a string that is not one of the names SQLAlchemy maps back, and every read
of those rows would raise LookupError instead of returning a position.

VARCHAR(6) matches what ``sa.Enum(AssetClass)`` emits on SQLite today ('EQUITY' and
'OPTION' are both 6 characters). A longer member added later must widen this, or the
create_all path and this one stop building the same column;
tests/test_option_intent_migration.py runs alembic's own comparator with
compare_type=True and will say so.

FORWARD-ONLY: NOTHING IS BACK-FILLED
====================================
Every pre-existing row gets 'EQUITY' from the server default and keeps NULL
``option_strategy`` / ``expiry``. 23 of the 82 historical option orders have
unrecoverable contracts, so reconstructing the strategy or the expiry would be
guesswork wearing a fact's clothing.

The honest cost, measured read-only on ~/Documents/ba2/trade/db.sqlite on
2026-08-24: 20 transactions there DO have an option TradingOrder underneath (13 of
them still OPEN) and every one of them will read as EQUITY. That is recoverable
later and cheaply -- ``o.asset_class = 'OPTION'`` on the child order is a recorded
fact, not a guess -- but it is a separate revision and a separate decision, and it
is stated here rather than papered over.

IDEMPOTENT ON PURPOSE
=====================
Every add is guarded by a column check, because THIS REVISION RACES init_db().
Starting the app once on this branch runs ``SQLModel.metadata.create_all()``, which
materialises the three columns outside alembic on any database that is not brand new
(init_db only stamps head when the schema was ABSENT beforehand -- see
_schema_is_absent). An unguarded ``op.add_column`` then dies with "duplicate column
name: asset_class" and the revision can never be applied.

Unlike the allocation revision this one has NO ``create_table`` to fall back on: no
alembic revision has ever created ``transaction`` (see ca1825d61f7d -- "we don't drop
the transaction table since it was created by SQLModel"), which is also why
``alembic upgrade`` from an EMPTY file does not work on this repo at all. Every
database that reaches here already has the table. If it does not, this revision
raises rather than skipping: a silent skip would leave the model declaring three
columns the schema does not have, and every read would die on "no such column".

PRODUCTION RUNBOOK
==================
For ~/Documents/ba2/trade/db.sqlite. Read step 1 before touching anything.

1. WHICH SITUATION ARE YOU IN? Ask the database, not your memory:

       sqlite3 db.sqlite 'PRAGMA table_info("transaction");' | grep -E 'asset_class|option_strategy|expiry'
       venv/bin/python -m alembic current

   NOTHING listed -> the ordinary path, step 3.
   ALL THREE listed and current is behind -> the app already added them with
   create_all; step 2 applies and is the cheaper move.
   SOME of the three -> a previous half-finished run. Step 3 still works; that is
   the whole point of the guards.

2. IF create_all ALREADY ADDED THEM, `alembic stamp b2f4c81d6a35` IS LEGITIMATE:

       venv/bin/python -m alembic stamp b2f4c81d6a35

   It is legitimate because the columns this revision adds are PROVEN equal to the
   ones create_all builds: tests/test_option_intent_migration.py runs alembic's own
   autogenerate comparator over the migrated ``transaction`` table against
   SQLModel.metadata and asserts ZERO differences -- types, nullability, indexes.
   Stamping records a fact rather than skipping work.

   One difference is expected and is NOT drift: column ORDER. ``ALTER TABLE ADD
   COLUMN`` appends, so a migrated database has the three at the end while a
   create_all one has them after ``multiplier``. The live table is already out of
   declaration order for exactly this reason -- ``side``, ``close_reason``, both
   override flags and ``multiplier`` itself all arrived by ALTER.

3. THE ORDINARY PATH:

       venv/bin/python -m alembic upgrade b2f4c81d6a35

   MIND THE PARENTS. As of 2026-08-24 the live database is at d5e1b9a3c842, FIVE
   revisions behind, and the path here runs f1a7c2e9b4d0 -- the DESTRUCTIVE
   instrument merge -- and f1c8a24b7e05. Follow THEIR runbooks first; do not let
   them ride along unread behind an `upgrade head`.

4. VERIFY:

       sqlite3 db.sqlite 'PRAGMA table_info("transaction");' | grep -cE 'asset_class|option_strategy|expiry'   -> 3
       sqlite3 db.sqlite 'SELECT DISTINCT asset_class FROM "transaction";'                                      -> EQUITY
       venv/bin/python -m alembic current                                                                       -> b2f4c81d6a35

   'EQUITY' in capitals is the correct answer. Lowercase 'equity' means the default
   was written as the enum VALUE and every one of those rows is unreadable by the
   ORM -- see THE ENUM IS STORED BY NAME above.

5. IF IT FAILS, JUST RE-RUN IT. Nothing here deletes or rewrites a row; the worst a
   half-finished run can leave behind is some of the three columns, and the next run
   skips exactly those. Do NOT hand-type an ALTER TABLE -- create_all and this
   revision agree today (step 2 proves it) and a hand-typed one is how they would
   stop agreeing.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2f4c81d6a35'
down_revision: Union[str, Sequence[str], None] = 'a3f1c07d9e21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "transaction"


def _require_table(inspector, action: str) -> None:
    if not inspector.has_table(TABLE):
        raise RuntimeError(
            f"cannot {action}: table {TABLE!r} is missing. No alembic revision has "
            "ever created it (init_db()'s create_all does), so this is not a "
            "database this revision knows how to repair -- and skipping silently "
            "would leave the model declaring columns the schema does not have.")


def _add_column_if_absent(column: sa.Column) -> None:
    """``op.add_column`` unless the column is already there.

    A FRESH inspector every call, deliberately: an Inspector memoises its
    reflection, and this function changes the schema between calls, so a shared one
    would answer from a snapshot taken before the first ADD COLUMN and try to add a
    column it had just added -- a hard error on SQLite.

    ``asset_class`` is NOT NULL and carries a server default; SQLite accepts ADD
    COLUMN NOT NULL only BECAUSE of that default, and would refuse outright on a
    non-empty table without one. The other two are nullable, because NULL is how
    "this is not an option" is spelled and any sentinel would be an invention.
    """
    inspector = sa.inspect(op.get_bind())
    _require_table(inspector, f"add {TABLE}.{column.name}")
    names = {existing["name"] for existing in inspector.get_columns(TABLE)}
    if column.name in names:
        # print(), not logger: alembic's fileConfig can disable the app loggers,
        # and an operator following the runbook needs to see what was skipped.
        print(f"[option-intent] {TABLE}.{column.name} already exists -- skipped")
        return
    op.add_column(TABLE, column)
    print(f"[option-intent] added {TABLE}.{column.name}")


def _create_index_if_absent(index_name: str, columns) -> None:
    """``op.create_index`` unless that index is already on the table.

    Indexes need their own guard rather than riding on the columns': a create_all
    database has both, but one stranded by a half-finished run of THIS revision can
    have a column and not its index.

    The columns being indexed were added moments ago by _add_column_if_absent, so
    this is also where a silently-ineffective ADD COLUMN is caught -- alembic reports
    SQLite as non-transactional DDL, so without this the revision would leave a
    half-migrated table behind and still move alembic_version to head.

    A FRESH inspector every call, for the same reason _add_column_if_absent takes
    one: a shared Inspector memoises the column list from BEFORE the first ADD
    COLUMN, and would report a column that was just added as missing.
    """
    inspector = sa.inspect(op.get_bind())
    _require_table(inspector, f"create index {index_name}")
    present = {existing["name"] for existing in inspector.get_columns(TABLE)}
    missing = [name for name in columns if name not in present]
    if missing:
        raise RuntimeError(
            f"cannot create {index_name} on {TABLE}: column(s) {', '.join(missing)} "
            "are missing, which means the ADD COLUMN above silently did nothing")
    if inspector.has_index(TABLE, index_name):
        print(f"[option-intent] index {index_name} already exists -- skipped")
        return
    op.create_index(index_name, TABLE, columns)


def _drop_index_if_present(index_name: str) -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE) or not inspector.has_index(TABLE, index_name):
        print(f"[option-intent] index {index_name} is already gone -- skipped")
        return
    op.drop_index(index_name, table_name=TABLE)


def _drop_column_if_present(column_name: str) -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        print(f"[option-intent] {TABLE} is already gone -- skipped")
        return
    names = {existing["name"] for existing in inspector.get_columns(TABLE)}
    if column_name not in names:
        print(f"[option-intent] {TABLE}.{column_name} is already gone -- skipped")
        return
    op.drop_column(TABLE, column_name)


def upgrade() -> None:
    # EQUITY, in capitals: SQLAlchemy persists the enum's NAME. See the header.
    _add_column_if_absent(
        sa.Column("asset_class", sa.String(length=6), nullable=False,
                  server_default="EQUITY"))
    _add_column_if_absent(sa.Column("option_strategy", sa.String(), nullable=True))
    _add_column_if_absent(sa.Column("expiry", sa.Date(), nullable=True))

    # The names SQLAlchemy itself emits for these fields (``ix_<table>_<column>``),
    # so init_db()'s create_all on a fresh DB and Alembic on an existing one agree.
    # Both are query keys for the lifecycle pass: "every open option position" and
    # "everything expiring within N days" run on every open_positions trigger.
    _create_index_if_absent("ix_transaction_asset_class", ["asset_class"])
    _create_index_if_absent("ix_transaction_expiry", ["expiry"])


def downgrade() -> None:
    """Drop the three columns. Guarded for the same reason upgrade() is.

    Indexes first: SQLite refuses to drop a column that an index still mentions.
    """
    _drop_index_if_present("ix_transaction_expiry")
    _drop_index_if_present("ix_transaction_asset_class")
    for column_name in ("expiry", "option_strategy", "asset_class"):
        _drop_column_if_present(column_name)
