"""merge duplicate instrument rows and make instrument.name unique

Revision ID: f1a7c2e9b4d0
Revises: 0a3e0bd24598
Create Date: 2026-08-20 12:00:00.000000

`instrument.name` had no unique constraint and no index, and production holds
duplicate names. `add_label_to_instruments` resolves a symbol with `.first()`
while `get_labels_by_symbol` keys by name, so on those symbols a label write
lands on an arbitrary row and can be invisible to the next read. Portfolio
allocation cannot be built on that.

The merge is unusually safe: NO table has a foreign key to `instrument` (verified
by grepping for `foreign_key="instrument` and by iterating pragma_foreign_key_list
over the live schema), so rows can be merged without repointing anything.

The merge itself lives in `ba2_common.core.instrument_merge` and is imported here
through the in-tree alias shim -- exactly how alembic/env.py imports models -- so
this migration and its tests execute the SAME code. It is idempotent: the plan is
recomputed from the current table state, so re-running writes nothing.

The import is INSIDE upgrade(), not at module scope. `alembic heads` / `history` /
`branches` load every revision module but never run env.py, and env.py is what puts
packages/* on sys.path (the venv's editable installs point at a path that does not
exist here). A module-scope import of the shim therefore breaks those commands with
ModuleNotFoundError: ba2_common, for every user, forever. Deferring it costs nothing:
upgrade() only ever runs under env.py.

INDEX NAME: `ix_instrument_name`, NOT `uix_instrument_name`. `Instrument.name` is
declared `Field(unique=True, index=True)`, which makes SQLModel's create_all emit
`CREATE UNIQUE INDEX ix_instrument_name ON instrument (name)` on a fresh database
(verified by probe). Any other name here and a migrated database would disagree
with a freshly created one forever.

DRY RUN -- inspect the affected names before committing to the merge. This prints
every group and aborts before writing anything, so it is read-only:

    BA2_INSTRUMENT_MERGE_DRY_RUN=1 BA2_DB_FILE=/path/to/db.sqlite \
        venv/bin/python -m alembic upgrade head

The merge is NOT reversible: downgrade only drops the index.
"""
import os
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a7c2e9b4d0'
down_revision: Union[str, Sequence[str], None] = '0a3e0bd24598'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = 'ix_instrument_name'


def _dry_run_requested() -> bool:
    """Whether BA2_INSTRUMENT_MERGE_DRY_RUN asks for a report-only run."""
    return os.environ.get('BA2_INSTRUMENT_MERGE_DRY_RUN', '').strip().lower() in ('1', 'true', 'yes')


def upgrade() -> None:
    """Upgrade schema."""
    # Deferred on purpose -- see the module docstring: `alembic heads`/`history`
    # import this file without ever running env.py, which is what makes ba2_common
    # importable.
    from ba2_trade_platform.core.instrument_merge import (
        merge_duplicate_instruments,
        report_duplicate_instruments,
    )

    connection = op.get_bind()

    if _dry_run_requested():
        # print(), not logger: alembic's fileConfig can disable the app loggers,
        # and this report is the whole point of the dry run.
        plan = report_duplicate_instruments(connection)
        print(f"[instrument-merge dry-run] {len(plan)} instrument group(s) would be rewritten")
        for group in plan:
            print(
                f"[instrument-merge dry-run] {group['name']}: keep id={group['keep_id']} "
                f"delete ids={group['delete_ids']} type={group['instrument_type']} "
                f"labels={group['labels']} categories={group['categories']}"
            )
        raise RuntimeError(
            f"BA2_INSTRUMENT_MERGE_DRY_RUN is set: reported {len(plan)} group(s) and aborted "
            "before writing anything. Unset the variable to run the merge for real."
        )

    stats = merge_duplicate_instruments(connection)
    print(
        f"[instrument-merge] merged {stats['duplicate_groups']} duplicate group(s), "
        f"deleted {stats['rows_deleted']} row(s), normalised {stats['rows_renamed']} name(s)"
    )

    existing = {ix['name'] for ix in sa.inspect(connection).get_indexes('instrument')}
    if INDEX_NAME not in existing:
        op.create_index(INDEX_NAME, 'instrument', ['name'], unique=True)


def downgrade() -> None:
    """Downgrade schema. The row merge cannot be undone; only the index is dropped."""
    connection = op.get_bind()
    existing = {ix['name'] for ix in sa.inspect(connection).get_indexes('instrument')}
    if INDEX_NAME in existing:
        op.drop_index(INDEX_NAME, table_name='instrument')
