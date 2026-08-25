"""give a managed allocation label a colour swatch

Revision ID: c4d7e2b18a93
Revises: b2f4c81d6a35
Create Date: 2026-08-24

The Portfolio Allocation page draws one expansion per managed label and a picker
listing them all; every one of them carried the same grey ``label`` icon, so on an
account managing five or six baskets the only thing telling them apart was the text.
This adds the swatch.

ONE COLUMN, NULLABLE, AND IT IS NOT BACK-FILLED
===============================================
``portfolio_allocation_label.color`` holds one of the seven hexes in
``ba2_trade_platform.ui.utils.portfolio_allocation_view.LABEL_COLOR_PALETTE``
(Okabe & Ito's colour-universal-design set, minus their black, which is invisible on
this dark UI).

NULL means "the user has not chosen a colour". That is a DIFFERENT FACT from a
stored default, and the difference is visible in the product: a default -- whether
written as a server default or as an UPDATE here -- would make every label that
predates this revision claim a colour nobody picked, the picker could never show
"No colour" truthfully, and there would be no way to tell a deliberate grey from an
un-answered question. So there is no server default, no Python default in the
migration, and no UPDATE statement anywhere in this file. The render turns NULL into
a neutral grey at DRAW time (``resolve_label_icon_color``) and the column stays
empty.

The column is also whitelisted on the way OUT: the render looks the stored string up
in the palette and falls back to grey on a miss, so a hand-edited row can neither
break the page nor put anything into a CSS ``style`` attribute. Nothing about money
reads this column.

IDEMPOTENT ON PURPOSE
=====================
The add is guarded by a column check, because THIS REVISION RACES init_db().
Starting the app once on this branch runs ``SQLModel.metadata.create_all()``, which
materialises ``color`` outside alembic on any database that is not brand new
(init_db only stamps head when the schema was ABSENT beforehand). An unguarded
``op.add_column`` then dies with "duplicate column name: color" and the revision can
never be applied. This is exactly the trap b2f4c81d6a35 documents, and the guards
are its guards.

Unlike b2f4c81d6a35, this one DOES have a create_table ancestor:
``portfolio_allocation_label`` is created by f1c8a24b7e05. It is still an error
rather than a skip if the table is absent -- a silent skip would move
``alembic_version`` to head with the model declaring a column the schema does not
have, and every read of a managed label would then die on "no such column".

PRODUCTION RUNBOOK
==================
For ~/Documents/ba2/trade/db.sqlite.

1. WHICH SITUATION ARE YOU IN? Ask the database, not your memory:

       sqlite3 db.sqlite 'PRAGMA table_info(portfolio_allocation_label);' | grep color
       venv/bin/python -m alembic current

   NOTHING listed -> the ordinary path, step 2.
   'color' listed and current is behind -> the app already added it with create_all;
   `alembic stamp c4d7e2b18a93` is legitimate and complete. Unlike b2f4c81d6a35 there
   is no second half: this revision has no back-fill, so stamping skips nothing.

2. THE ORDINARY PATH:

       venv/bin/python -m alembic upgrade c4d7e2b18a93

   MIND THE PARENTS. Follow the runbooks of f1a7c2e9b4d0 (the DESTRUCTIVE instrument
   merge), f1c8a24b7e05 and b2f4c81d6a35 first if the database has not reached them;
   do not let them ride along unread behind an `upgrade head`.

3. VERIFY:

       sqlite3 db.sqlite 'PRAGMA table_info(portfolio_allocation_label);' | grep -c color
           -> 1
       sqlite3 db.sqlite 'SELECT COUNT(*) FROM portfolio_allocation_label WHERE color IS NOT NULL;'
           -> 0 (on a database whose app has not yet had a colour picked in the UI)
       venv/bin/python -m alembic current
           -> c4d7e2b18a93

   A non-zero third line on a database nobody has used the picker on means something
   back-filled a default. That is the one outcome this revision is written to avoid.

4. IF IT FAILS, JUST RE-RUN IT. There is no write of any kind here -- only ADD
   COLUMN, guarded -- so a second run either adds the column or reports that it is
   already there.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4d7e2b18a93'
down_revision: Union[str, Sequence[str], None] = 'b2f4c81d6a35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "portfolio_allocation_label"
COLUMN = "color"


def _require_table(inspector, action: str) -> None:
    if not inspector.has_table(TABLE):
        raise RuntimeError(
            f"cannot {action}: table {TABLE!r} is missing. It is created by revision "
            "f1c8a24b7e05 (and by init_db()'s create_all), so a database without it "
            "has not reached this point -- and skipping silently would leave the "
            "model declaring a column the schema does not have, so every read of a "
            "managed label would die on 'no such column'.")


def upgrade() -> None:
    """Add ``color``, unless it is already there. NOTHING is written to any row.

    Nullable and with NO server default, deliberately: see the header. SQLite is
    happy to ADD COLUMN NULL on a populated table precisely because there is nothing
    to fill in.
    """
    inspector = sa.inspect(op.get_bind())
    _require_table(inspector, f"add {TABLE}.{COLUMN}")
    if COLUMN in {c["name"] for c in inspector.get_columns(TABLE)}:
        # print(), not logger: alembic's fileConfig can disable the app loggers, and
        # an operator following the runbook needs to see what was skipped.
        print(f"[label-color] {TABLE}.{COLUMN} already exists -- skipped")
        return
    op.add_column(TABLE, sa.Column(COLUMN, sa.String(), nullable=True))
    print(f"[label-color] added {TABLE}.{COLUMN} (nullable, no default, no back-fill)")


def downgrade() -> None:
    """Drop the column if it is there.

    Guarded rather than raising on a missing table, unlike ``upgrade``: going
    FORWARD onto a schema that has no table is a state nobody can repair silently,
    while going BACK to a state that has already been reached is simply done.
    """
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        print(f"[label-color] {TABLE} is already gone -- skipped")
        return
    if COLUMN not in {c["name"] for c in inspector.get_columns(TABLE)}:
        print(f"[label-color] {TABLE}.{COLUMN} is already gone -- skipped")
        return
    op.drop_column(TABLE, COLUMN)
